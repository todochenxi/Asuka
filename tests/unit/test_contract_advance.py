"""契约层必须能**推进**一条 Run，且账本必须**看得见**（M28 / F-1~F-4）。

--------------------------------------------------------------------------
为什么要有这个文件

M28 之前，契约层（`packages.agent_api`）只有四种操作：

    start_run / get_run / list_approvals / decide

而 `start_run` 调的是 `stack.start()` —— 那只做初始化
（interpret → goal → state），**不跑**。于是通过 API 开出来的 Run：

    status = created,  step_count = 0,  trace = 0 条

不产生审批、不做工具调用、永不完成。六个 HTTP 端点里没有任何一个能让它
往前走一步。

这是比"缺功能"更糟的一类缺口 —— **它看起来完全是通的**：
装得起来、开得出 Run、查得到状态、审批列表也 Working。
探针实测的时候，`POST /runs` 返回 201，一切正常。
只有把 Run 一路点下去才会发现它一动不动。

**一条不变量，如果没有任何测试要求它成立，它就不成立。**
（这是 `default_model_id`（G-8）、`CHILD_AGENT`（D-4）、
compensation 序列化（PR-27）之后的第四次。）

--------------------------------------------------------------------------
每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from typing import Any

from examples.demo_stack import build_stack_factory
from packages.agent_api.dto import DecisionRequest, StartRunRequest
from packages.agent_api.errors import Conflict
from packages.agent_api.handlers import drive_run, get_trace, step_run
from packages.agent_api.service import InProcessControlPlane
from packages.agent_harness.approval import InMemoryApprovalStore

#: 每条 Run 都是新的 DecisionEngine，所以这些测试之间不共享状态
APPROVAL_AT = 2


def _plane(*, approval_at_step: int = 0) -> InProcessControlPlane:
    return InProcessControlPlane(
        factory=build_stack_factory(None, approval_at_step=approval_at_step),
        approvals=InMemoryApprovalStore(),
    )


class StartDoesNotAdvanceTest(unittest.TestCase):
    """先把 M28 之前的行为钉死，防止有人再以为 `start` 就是"跑"。"""

    def test_starting_a_run_leaves_it_at_created(self) -> None:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        self.assertEqual(view.status, "created")
        self.assertEqual(view.step_count, 0)
        self.assertEqual(
            view.last_outcome, "", "刚 start 的 Run 还没有走过任何一步"
        )

    def test_the_control_it_is_not_stuck_there(self) -> None:
        """控制组：它停在 created 是因为 start 只初始化 —— 推一下就动了。

        少了这条，上一条可以被"整个系统压根跑不动"满足。
        """
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        view = cp.step_run(view.run_id)
        self.assertNotEqual(view.status, "created")
        self.assertEqual(view.step_count, 1)


class StepAdvancesTest(unittest.TestCase):
    """F-1：契约层能把一条 Run 往前推。"""

    def test_stepping_produces_a_real_model_call(self) -> None:
        """推进不是"计数器 +1"——账本里真的有一次模型调用。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.step_run(view.run_id)
        entries = cp.get_trace(view.run_id).entries
        kinds = [e["kind"] for e in entries]
        self.assertIn("task.submitted", kinds)
        self.assertIn("execution.observed", kinds)

        observed = [e for e in entries if e["kind"] == "execution.observed"]
        self.assertTrue(
            any(e["served"].get("model") for e in observed),
            f"模型调用没真的落到某个 Deployment 上：{observed}",
        )

    def test_a_second_step_reaches_a_terminal_state(self) -> None:
        """两步之内必须能走到终态 —— 否则"跑得动"又变成一句空话。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.step_run(view.run_id)
        view = cp.step_run(view.run_id)
        self.assertEqual(view.status, "completed")
        self.assertEqual(view.last_outcome, "finished")

    def test_drive_runs_until_it_stops(self) -> None:
        """`drive_run` 一次走到停，不用调用方自己循环。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        view = cp.drive_run(view.run_id)
        self.assertEqual(view.status, "completed")

    def test_drive_stops_at_the_gate_not_past_it(self) -> None:
        """控制组：`drive_run` 遇到闸门会**自己停**，不是一路撞过去。"""
        cp = _plane(approval_at_step=APPROVAL_AT)
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="写一条笔记")
        )
        view = cp.drive_run(view.run_id)
        self.assertEqual(view.status, "suspended")
        self.assertEqual(view.last_outcome, "waiting_approval")


class TerminalRefusesToAdvanceTest(unittest.TestCase):
    """F-2：终态之后再推进要**明说**，不能静默成功。"""

    def _completed(self) -> Any:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        return cp, cp.drive_run(view.run_id)

    def test_stepping_a_terminal_run_raises_run_terminal(self) -> None:
        cp, view = self._completed()
        with self.assertRaises(Conflict) as cm:
            cp.step_run(view.run_id)
        self.assertEqual(cm.exception.code, "RUN_TERMINAL")
        self.assertEqual(cm.exception.http_status, 409)

    def test_driving_a_terminal_run_raises_run_terminal(self) -> None:
        cp, view = self._completed()
        with self.assertRaises(Conflict) as cm:
            cp.drive_run(view.run_id)
        self.assertEqual(cm.exception.code, "RUN_TERMINAL")

    def test_the_control_refusing_does_not_grow_the_ledger(self) -> None:
        """控制组：被拒绝的那一次**什么都没记**。

        这是 M28 探针实测到的第二个问题：终态之后重复 `step()`
        会在账本里再记一条 `run.finished`（实测 3 条重复），
        于是"这个 Run 完成了几次"变成一个没有答案的问题。

        上一条只证明了"会拒绝"；这一条才证明"拒绝得干净"。
        """
        cp, view = self._completed()
        before = len(cp.get_trace(view.run_id).entries)
        for _ in range(3):
            with self.assertRaises(Conflict):
                cp.step_run(view.run_id)
        after = len(cp.get_trace(view.run_id).entries)
        self.assertEqual(after, before, f"账本在被拒绝的推进中变长了：{before} → {after}")

    def test_the_control_refusing_is_not_because_everything_is_refused(self) -> None:
        """控制组：非终态照样推得动 —— 上一条不是因为"什么都拒绝"。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        self.assertNotEqual(cp.step_run(view.run_id).status, "created")


class SuspendedSaysWhatItWaitsForTest(unittest.TestCase):
    """F-3 / R-6：挂起的 Run 必须说清楚在等谁。"""

    def test_a_suspended_run_names_the_approval(self) -> None:
        cp = _plane(approval_at_step=APPROVAL_AT)
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="写一条笔记")
        )
        view = cp.drive_run(view.run_id)
        self.assertEqual(view.status, "suspended")
        self.assertTrue(
            view.waiting_for,
            "挂起的 Run 没说在等谁 —— 界面上它只是「卡住了」，"
            "没人知道该去点哪一个按钮",
        )
        pending = [a.approval_id for a in cp.list_approvals(view.run_id)]
        self.assertIn(view.waiting_for, pending)

    def test_the_control_a_running_run_waits_for_nothing(self) -> None:
        """控制组：不在等待的时候 `waiting_for` 是 None，不是空串也不是上次的值。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        self.assertIsNone(cp.step_run(view.run_id).waiting_for)

    def test_approving_clears_what_it_waits_for(self) -> None:
        """批完之后必须**不再等待** —— 否则界面上会一直挂着那个按钮。"""
        cp = _plane(approval_at_step=APPROVAL_AT)
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="写一条笔记")
        )
        view = cp.drive_run(view.run_id)
        approval_id = view.waiting_for
        assert approval_id is not None
        view = cp.decide(
            view.run_id,
            DecisionRequest(approval_id=approval_id, decision="approve", by="alice"),
        )
        self.assertIsNone(view.waiting_for)


class TraceIsVisibleTest(unittest.TestCase):
    """F-4：账本必须能被单独读出来。"""

    def test_the_trace_is_readable_through_the_contract(self) -> None:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.drive_run(view.run_id)
        trace = cp.get_trace(view.run_id)
        self.assertEqual(trace.run_id, view.run_id)
        self.assertGreater(len(trace.entries), 0)
        self.assertGreaterEqual(trace.step_count, 1)

    def test_the_ledger_is_append_only(self) -> None:
        """账本只增：推进之后旧的一条都不能变。"""
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.step_run(view.run_id)
        before = list(cp.get_trace(view.run_id).entries)
        cp.drive_run(view.run_id)
        after = list(cp.get_trace(view.run_id).entries)
        self.assertEqual(after[: len(before)], before, "账本的前缀被改写了")

    def test_the_control_a_trace_of_an_unknown_run_is_not_empty_success(self) -> None:
        """控制组：查一个不存在的 Run 会 404，不是返回一本空账。

        空账和"没有这本账"在界面上是同一副样子，而后者是错误。
        """
        from packages.agent_api.errors import NotFound

        cp = _plane()
        with self.assertRaises(NotFound):
            cp.get_trace("run_does_not_exist")


class HandlersWorkWithoutAFrameworkTest(unittest.TestCase):
    """A-7：新增的三个 handler 与框架无关，且 409 语义正确。"""

    def test_step_handler_returns_the_run(self) -> None:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        response = step_run(cp, view.run_id, {})
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.body["status"], "created")

    def test_step_handler_maps_run_terminal_to_409(self) -> None:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.drive_run(view.run_id)
        response = step_run(cp, view.run_id, {})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "RUN_TERMINAL")

    def test_drive_handler_reports_a_missing_run_as_404(self) -> None:
        """查一个不存在的 Run 是 404，不是 500 —— 形状校验/编排在 handler（A-1）。"""
        cp = _plane()
        response = drive_run(cp, "run_does_not_exist", {})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "RUN_NOT_FOUND")

    def test_trace_handler_returns_entries(self) -> None:
        cp = _plane()
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="帮我算一下 1+1")
        )
        cp.drive_run(view.run_id)
        response = get_trace(cp, view.run_id)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["entries"])


class TheWholeFlowIncludingAnApprovalTest(unittest.TestCase):
    """把"含审批的完整流程"整条跑一遍 —— 这是页面要演示的那条路。

    审批不是引擎自己请示（`ActionType.HUMAN_APPROVAL`），而是
    **治理层拦下来的**：一个 HIGH 风险的 WRITE 工具调用被 Harness 判
    `REQUIRE_APPROVAL`（I-9）。演示的正是"审批不靠智能体自觉"。
    """

    def test_start_step_suspend_approve_finish(self) -> None:
        cp = _plane(approval_at_step=APPROVAL_AT)
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="写一条笔记")
        )
        self.assertEqual(view.status, "created")

        view = cp.step_run(view.run_id)                    # 1. 问模型
        self.assertEqual(view.status, "running")

        view = cp.step_run(view.run_id)                    # 2. 被治理层拦下
        self.assertEqual(view.status, "suspended")
        assert view.waiting_for is not None

        # 3. 这一步不该被"偷偷放过去"：账本里要有一次"请求了审批"
        kinds = [e["kind"] for e in cp.get_trace(view.run_id).entries]
        self.assertIn("approval.requested", kinds)

        # 4. 人批准 → 被挂起的动作**真的被执行了**（不是只改个状态）
        view = cp.decide(
            view.run_id,
            DecisionRequest(
                approval_id=view.waiting_for, decision="approve", by="alice"
            ),
        )
        self.assertIsNone(view.waiting_for)
        self.assertTrue(view.last_outcome, "批准后没报出执行结果")

        kinds = [e["kind"] for e in cp.get_trace(view.run_id).entries]
        self.assertIn("approval.decided", kinds)
        self.assertTrue(
            any("tool_call" in str(e.get("payload", {}).get("action_type", ""))
                for e in cp.get_trace(view.run_id).entries),
            "批准之后被挂起的那个工具调用没有真的执行",
        )

        # 5. 走到终态，且终态之后不再接受推进
        view = cp.step_run(view.run_id)
        self.assertEqual(view.status, "completed")
        with self.assertRaises(Conflict):
            cp.step_run(view.run_id)

    def test_the_control_rejecting_does_not_execute_the_action(self) -> None:
        """控制组：驳回的动作**不产生 Task** —— 上一条不是因为"批不批都执行"。"""
        cp = _plane(approval_at_step=APPROVAL_AT)
        view = cp.start_run(
            StartRunRequest(agent_id="demo", user_request="写一条笔记")
        )
        view = cp.drive_run(view.run_id)
        assert view.waiting_for is not None
        cp.decide(
            view.run_id,
            DecisionRequest(
                approval_id=view.waiting_for, decision="reject", by="bob"
            ),
        )
        entries = cp.get_trace(view.run_id).entries
        tool_calls = [
            e for e in entries
            if str(e.get("payload", {}).get("action_type", "")) == "tool_call"
        ]
        self.assertEqual(
            tool_calls, [], "驳回之后那个 HIGH 风险的工具调用仍然被派出去了"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
