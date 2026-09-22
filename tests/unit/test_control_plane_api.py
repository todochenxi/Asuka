"""M18：Control Plane 契约层 + HITL 审批回调（基线 §44 第一行 / §23 HITL）。

    POST /agents/{agent_id}/runs        ← §44 的第一行，此前在代码里没有落点
    GET  /runs/{run_id}
    GET  /approvals
    POST /runs/{run_id}/approvals/{id}/decision    ← HITL 回调（此前人根本没法批准）

覆盖的不变量：

    A-1  API 不做业务判断
    A-2  领域异常 → 明确 HTTP 语义；500 只留给真的没预料到的
    A-3  POST /runs 幂等
    A-4  审批回调是唯一入口，且必须经 AgentLoop.approve()
    A-5  归属校验（404）+ 已决定再回调（409）
    A-6  响应不暴露 Kernel 内部对象
    A-7  handler 与框架无关
    A-8  审批必须带 by
    A-9  过期 ≠ 已决定（410 而不是 409 ALREADY_DECIDED）
    A-10 待审批列表查存储，不查内存 Loop
"""
from __future__ import annotations

import pathlib
import unittest
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_api import (
    ApiResponse,
    InProcessControlPlane,
    decide_approval,
    get_run,
    list_approvals,
    map_domain_error,
    start_run,
)
from packages.agent_api.handlers import cancel_run, step_run
from packages.agent_domain.errors import (
    ConcurrentStateError,
    IllegalTransition,
    InvariantViolation,
    LeaseRequired,
    RetryBudgetExhausted,
    StaleWriteError,
)
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.model_gateway import (
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    ToolRegistry,
    ToolRuntime,
    ToolSpec,
)
from packages.execution_kernel.inmemory import ManualClock

from .test_agent_loop_full import Interpreter, Planner


# ---------------------------------------------------------------- 替身
def calculator(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"value": eval(args["expr"], {"__builtins__": {}}, {})}  # noqa: S307 仅测试用


@dataclass
class RiskyThenFinish:
    """第一个动作是高风险工具调用（会被闸门挡住），之后 FINISH。"""

    expr: str = "6*7"
    used: bool = False

    def decide(self, state: State) -> Decision:
        if self.used:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            )
        self.used = True
        return Decision(
            run_id=state.run_id,
            selected_action=Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "calculator", "args": {"expr": self.expr}},
                risk_level=RiskLevel.HIGH,
            ),
            confidence_signal=0.99,      # I-9：再高也换不来自动放行
            rationale="scripted high risk",
        )


def build_gateway() -> ModelGateway:
    def _call(dep, request):
        return ok_response(dep, request, text="ok")

    model = Model(model_id="gpt-api", name="api model")
    dep = Deployment(deployment_id="gpt-api@p", model_id="gpt-api", provider="scripted")
    return ModelGateway(
        ModelRouter([model], [dep]),
        {"scripted": FunctionProvider("scripted", _call)},
        max_fallbacks=0,
        default_model_id="gpt-api",
    )


def build_tool_runtime() -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="calculator", version="1.0.0"),
        FunctionInvoker(calculator),
        make_default=True,
    )
    return ToolRuntime(registry)


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = build_gateway()
        self.tool_runtime = build_tool_runtime()
        self.cp = InProcessControlPlane(factory=self._factory)

    def _factory(self, agent_id: str, approvals):
        # A-10：审批存储由 ControlPlane 持有并**共享**给所有 Run ——
        # 它不是某个 Run 的私有状态。
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=Interpreter(),
            planner=Planner(),
            decision_engine=RiskyThenFinish(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=ManualClock(),
            approval_store=approvals,
        )

    def _start(self, **extra) -> dict[str, Any]:
        body = {"agent_id": "agent-api", "user_request": "compute 6*7"}
        body.update(extra)
        resp = start_run(self.cp, body, idempotency_key=str(extra.get("_idem") or ""))
        self.assertIn(resp.status, (200, 201), resp.body)
        return dict(resp.body)

    def _pending(self, run_id: str) -> dict[str, Any]:
        resp = list_approvals(self.cp, run_id)
        self.assertEqual(resp.status, 200)
        items = resp.body["items"]
        self.assertEqual(len(items), 1, items)
        return dict(items[0])


# ---------------------------------------------------------------- §44 第一行
class StartRunTest(ApiTestBase):
    def test_post_runs_returns_201_and_a_run_view(self) -> None:
        resp = start_run(self.cp, {"agent_id": "agent-api", "user_request": "hi"})
        self.assertEqual(resp.status, 201)
        self.assertTrue(resp.body["run_id"])
        self.assertEqual(resp.body["agent_id"], "agent-api")

    def test_a6_run_view_exposes_no_kernel_objects(self) -> None:
        """A-6：RunView 里不该出现 Execution / Attempt / Lease / fencing_token。"""
        view = self._start()
        for forbidden in ("execution", "executions", "attempt", "attempts",
                          "lease", "fencing_token", "lease_index"):
            self.assertNotIn(forbidden, view, forbidden)

    def test_missing_fields_are_400_not_500(self) -> None:
        resp = start_run(self.cp, {"agent_id": "agent-api"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["error"]["code"], "BAD_REQUEST")

    def test_get_run(self) -> None:
        created = self._start()
        resp = get_run(self.cp, created["run_id"])
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["run_id"], created["run_id"])

    def test_unknown_run_is_404(self) -> None:
        resp = get_run(self.cp, "run_nope")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body["error"]["code"], "RUN_NOT_FOUND")


class IdempotencyTest(ApiTestBase):
    def test_a3_same_key_does_not_create_a_second_run(self) -> None:
        first = start_run(self.cp, {"agent_id": "a", "user_request": "x"},
                          idempotency_key="k1")
        second = start_run(self.cp, {"agent_id": "a", "user_request": "x"},
                           idempotency_key="k1")
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)              # 早就有了
        self.assertTrue(second.body["replayed"])
        self.assertEqual(first.body["run_id"], second.body["run_id"])

    def test_a3_different_key_creates_a_new_run(self) -> None:
        first = start_run(self.cp, {"agent_id": "a", "user_request": "x"},
                          idempotency_key="k1")
        second = start_run(self.cp, {"agent_id": "a", "user_request": "x"},
                           idempotency_key="k2")
        self.assertNotEqual(first.body["run_id"], second.body["run_id"])

    def test_a3_no_key_means_no_idempotency(self) -> None:
        first = start_run(self.cp, {"agent_id": "a", "user_request": "x"})
        second = start_run(self.cp, {"agent_id": "a", "user_request": "x"})
        self.assertNotEqual(first.body["run_id"], second.body["run_id"])


class ErrorMappingTest(unittest.TestCase):
    """A-2：领域异常必须翻成明确的 HTTP 语义。"""

    def test_invariant_violation_is_422_not_500(self) -> None:
        err = map_domain_error(InvariantViolation("B-3: already completed"))
        self.assertEqual(err.http_status, 422)
        self.assertEqual(err.code, "INVARIANT_VIOLATION")

    def test_illegal_transition_is_409(self) -> None:
        self.assertEqual(map_domain_error(IllegalTransition("x")).http_status, 409)

    def test_concurrent_state_is_409_and_hints_re_read(self) -> None:
        err = map_domain_error(ConcurrentStateError("version conflict"))
        self.assertEqual(err.http_status, 409)
        self.assertIn("retry_hint", err.details)

    def test_stale_write_and_lease_required_are_409(self) -> None:
        self.assertEqual(map_domain_error(StaleWriteError("x")).http_status, 409)
        self.assertEqual(map_domain_error(LeaseRequired("x")).http_status, 409)

    def test_retry_budget_exhausted_is_429(self) -> None:
        self.assertEqual(map_domain_error(RetryBudgetExhausted("x")).http_status, 429)

    def test_only_unexpected_errors_are_500(self) -> None:
        """500 只能是 bug —— 否则监控会被一堆假 500 淹没。"""
        err = map_domain_error(RuntimeError("boom"))
        self.assertEqual(err.http_status, 500)
        self.assertEqual(err.code, "INTERNAL")


# ---------------------------------------------------------------- HITL 回调
class HitlCallbackTest(ApiTestBase):
    def test_a4_human_can_actually_approve(self) -> None:
        """此前 `loop.approve()` 只是个进程内方法 —— 人根本没法批准。"""
        created = self._start()
        run_id = created["run_id"]
        stack = self.cp.runs[run_id]

        self.assertEqual(stack.loop.step().value, "waiting_approval")
        approval = self._pending(run_id)
        self.assertEqual(approval["status"], "pending")
        self.assertTrue(approval["execution_id"])      # 能追溯到挂起的那条 Execution

        resp = decide_approval(
            self.cp, run_id, approval["approval_id"],
            {"decision": "approve", "by": "alice", "comment": "looks fine"},
        )
        self.assertEqual(resp.status, 200)
        # A-4：Kernel 里那条 SUSPENDED 真的被唤醒并走完了
        self.assertIn("executed", [h.value for h in stack.loop.history])

    def test_reject_does_not_execute(self) -> None:
        created = self._start()
        run_id = created["run_id"]
        stack = self.cp.runs[run_id]
        stack.loop.step()
        approval = self._pending(run_id)

        resp = decide_approval(
            self.cp, run_id, approval["approval_id"],
            {"decision": "reject", "by": "bob"},
        )
        self.assertEqual(resp.status, 200)
        self.assertNotIn("executed", [h.value for h in stack.loop.history])

    def test_a5_deciding_twice_is_409_not_silent_success(self) -> None:
        """静默成功 = 审计记录被第二次调用覆盖，"谁批的"就说不清了。"""
        created = self._start()
        run_id = created["run_id"]
        stack = self.cp.runs[run_id]
        stack.loop.step()
        approval = self._pending(run_id)

        decide_approval(self.cp, run_id, approval["approval_id"],
                        {"decision": "approve", "by": "alice"})
        again = decide_approval(self.cp, run_id, approval["approval_id"],
                                {"decision": "reject", "by": "bob"})
        self.assertEqual(again.status, 409)
        self.assertEqual(again.body["error"]["code"], "APPROVAL_ALREADY_DECIDED")
        self.assertEqual(again.body["error"]["details"]["decided_by"], "alice")

    def test_a5_approval_from_another_run_is_404(self) -> None:
        """用 404 而不是 403 —— 不泄漏"另一个 Run 里有这条审批"。"""
        run_a = self._start()["run_id"]
        run_b = self._start()["run_id"]
        self.cp.runs[run_a].loop.step()
        approval = self._pending(run_a)

        resp = decide_approval(self.cp, run_b, approval["approval_id"],
                               {"decision": "approve", "by": "alice"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body["error"]["code"], "APPROVAL_NOT_FOUND")

    def test_a8_anonymous_approval_is_rejected(self) -> None:
        created = self._start()
        run_id = created["run_id"]
        self.cp.runs[run_id].loop.step()
        approval = self._pending(run_id)

        resp = decide_approval(self.cp, run_id, approval["approval_id"],
                               {"decision": "approve"})
        self.assertEqual(resp.status, 400)

    def test_bad_decision_value_is_400(self) -> None:
        created = self._start()
        run_id = created["run_id"]
        self.cp.runs[run_id].loop.step()
        approval = self._pending(run_id)

        resp = decide_approval(self.cp, run_id, approval["approval_id"],
                               {"decision": "maybe", "by": "alice"})
        self.assertEqual(resp.status, 400)

    def test_approvals_are_scoped_by_run(self) -> None:
        run_a = self._start()["run_id"]
        self._start()                                  # 第二个 Run，还没走 step()
        self.cp.runs[run_a].loop.step()
        resp = list_approvals(self.cp, run_a)
        self.assertEqual(len(resp.body["items"]), 1)


class ApprovalLifecycleTest(ApiTestBase):
    """A-9 / A-10：审批不是"待办事项"，它有生命周期，而且必须活过内存。"""

    def _start_pending(self):
        created = self._start()
        run_id = created["run_id"]
        stack = self.cp.runs[run_id]
        stack.loop.step()
        approval = self._pending(run_id)
        return run_id, stack, approval

    def test_a9_expired_is_410_not_already_decided(self) -> None:
        """最容易写错的一版：把超时也报成 409 `APPROVAL_ALREADY_DECIDED`。

        语法上没错，语义上是在告诉调用方"有人批过了，去等结果吧"——
        而那个结果永远不会来。审计要回答的恰恰是"到底有没有人批过"。
        """
        run_id, stack, approval = self._start_pending()
        stack.clock.advance(timedelta(minutes=31))          # 过了默认 30min TTL
        stack.loop.harness.approvals.expire_due()

        resp = decide_approval(self.cp, run_id, approval["approval_id"],
                               {"decision": "approve", "by": "alice"})
        self.assertEqual(resp.status, 410)
        self.assertEqual(resp.body["error"]["code"], "APPROVAL_EXPIRED")
        self.assertNotEqual(resp.body["error"]["code"], "APPROVAL_ALREADY_DECIDED")

    def test_a9_expiry_before_the_sweeper_runs_is_also_410(self) -> None:
        """存储里还写着 PENDING，但时间已经过了 —— 只差一次 `expire_due()`。

        这里钉住两件事：
          1) 依然是 410（不能因为"状态字段还没改"就放行 —— 那是 H-8）
          2) API **没有顺手把状态推进成 EXPIRED**：推进是 sweeper 的事，
             否则"被人判死的"和"被时间判死的"会混在一起，审计就废了。
        """
        run_id, stack, approval = self._start_pending()
        stack.clock.advance(timedelta(minutes=31))

        resp = decide_approval(self.cp, run_id, approval["approval_id"],
                               {"decision": "approve", "by": "alice"})
        self.assertEqual(resp.status, 410)
        self.assertEqual(resp.body["error"]["code"], "APPROVAL_EXPIRED")
        self.assertTrue(resp.body["error"]["details"]["pending"])

        stored = stack.loop.harness.approvals.get(approval["approval_id"])
        self.assertEqual(stored.status.value, "pending")    # API 没改它

    def test_a9_cancelled_has_its_own_code(self) -> None:
        run_id, stack, approval = self._start_pending()
        stack.loop.harness.approvals.cancel(approval["approval_id"], by="system")

        resp = decide_approval(self.cp, run_id, approval["approval_id"],
                               {"decision": "approve", "by": "alice"})
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.body["error"]["code"], "APPROVAL_CANCELLED")

    def test_a10_list_comes_from_the_store_not_the_loop(self) -> None:
        """直接往存储里写一条 Loop 没在等的审批 —— 它必须出现在列表里。

        如果列表读的是 `loop.pending_approval`，这条就看不见：
        服务一重启（Loop 没了、存储还在）列表就空了，而 Run 还挂着等人批。
        界面上什么都没有，系统里全在等 —— 这是最难排查的一类故障。
        """
        run_id, stack, approval = self._start_pending()
        extra = stack.loop.harness.approvals.request(
            Action(
                run_id=run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "calculator", "args": {"expr": "1+1"}},
                risk_level=RiskLevel.HIGH,
            ),
            reason="second gate",
        )
        self.assertNotEqual(extra.approval_id, approval["approval_id"])

        resp = list_approvals(self.cp, run_id)
        ids = {item["approval_id"] for item in resp.body["items"]}
        self.assertEqual(len(resp.body["items"]), 2, resp.body["items"])
        self.assertIn(extra.approval_id, ids)
        # Loop 明明只在等一条 —— 列表却给了两条，所以来源确实是存储
        self.assertIsNotNone(stack.loop.pending_approval)


class FrameworkIndependenceTest(unittest.TestCase):
    def _api_import_lines(self):
        root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "agent_api"
        for path in sorted(root.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    yield path.name, stripped

    def test_a7_handlers_do_not_import_a_web_framework(self) -> None:
        for name, line in self._api_import_lines():
            for framework in ("fastapi", "flask", "starlette"):
                self.assertNotIn(framework, line, f"{name}: {line}")

    def test_a1_api_cannot_even_see_the_business_rules(self) -> None:
        """A-1 最强的形式不是"约定不做业务判断"，而是**它看不见规则**。

        `agent_api` 连 policy / guardrail / cost 都 import 不到，
        于是"API 自己判断这个动作该不该做"在结构上就不可能发生 ——
        不用靠自觉，也不用靠 code review 抓。
        """
        for name, line in self._api_import_lines():
            for module in ("agent_harness.policy", "agent_harness.guardrail",
                           "agent_harness.cost", "agent_runtime.loop"):
                self.assertNotIn(module, line, f"{name}: {line}")


# ---------------------------------------------------------------- 叫停（B-8）
class CancelRunTest(ApiTestBase):
    """空洞 221：M33 之前契约层只有"推"，没有"停"。

    少一个推进行不行得通看得出来（Run 不动），
    少一个叫停**看不出来** —— Run 只是继续跑，而谁也没有办法让它停。
    """

    def test_a_run_can_be_stopped(self) -> None:
        created = self._start()
        resp = cancel_run(
            self.cp, created["run_id"], {"reason": "user asked", "by": "alice"}
        )
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["status"], "cancelled")
        self.assertEqual(resp.body["last_outcome"], "cancelled")

    def test_reason_is_required(self) -> None:
        """说不出为什么的取消，事后没人回答得了这一条为什么跑了一半。"""
        created = self._start()
        resp = cancel_run(self.cp, created["run_id"], {"by": "alice"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["error"]["code"], "BAD_REQUEST")

    def test_by_is_required(self) -> None:
        """A-8 同款：匿名取消进不了审计。"""
        created = self._start()
        resp = cancel_run(self.cp, created["run_id"], {"reason": "because"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["error"]["code"], "BAD_REQUEST")

    def test_a_terminal_run_cannot_be_cancelled(self) -> None:
        """B-10：终态不可取消。静默成功会让人以为自己按停了一条早就停了的 Run。"""
        created = self._start()
        cancel_run(self.cp, created["run_id"], {"reason": "because", "by": "alice"})
        resp = cancel_run(self.cp, created["run_id"], {"reason": "again", "by": "bob"})
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.body["error"]["code"], "RUN_TERMINAL")

    def test_unknown_run_is_404(self) -> None:
        resp = cancel_run(self.cp, "run_nope", {"reason": "because", "by": "alice"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body["error"]["code"], "RUN_NOT_FOUND")

    def test_cancelling_a_gated_run_does_not_leave_a_pending_approval(self) -> None:
        """A-9：撤销 ≠ 驳回。叫停之后那条审批不该还挂在"等人"里 ——
        它等的人不会来了，而界面上它还在，于是没人知道该去点哪里。"""
        created = self._start()
        step_run(self.cp, created["run_id"], {})
        self.assertEqual(len(list_approvals(self.cp, created["run_id"]).body["items"]), 1)

        cancel_run(self.cp, created["run_id"], {"reason": "because", "by": "alice"})

        self.assertEqual(
            list_approvals(self.cp, created["run_id"]).body["items"],
            [],
            "被叫停的 Run 不该还挂着一条待审批",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
