"""M10：Saga / Compensation。

覆盖的不变量：

    S-1   补偿是一等 Execution —— 不新开执行通道，撤销动作走 Kernel
    S-2   一条 Execution 最多一条补偿记录（DB UNIQUE 兜底）
    S-3   逆序撤销（LIFO）
    S-4   认领必须原子（与 A-11 同源：两个 Coordinator 同时扫到同一条）
    S-5   撤销不掉必须可见（UNRESOLVED + reason + Trace），不许静默
    S-6   单条失败不阻断其余
    S-8   补偿必须随正向动作声明；缺参数就记 UNRESOLVED，绝不瞎撤销
    S-9   逆操作跟着正向动作一起过策略（批准动作 = 批准撤销）
    S-10  撤销不计步数预算
    S-11  EXTERNAL_UNKNOWN 的副作用存疑 → 不自动撤销，也不当没发生
    S-12  补偿挂独立 Step（挂原 Step 会把已完成状态倒推回去）
    S-13  只有会产生外部副作用的动作类型才允许声明补偿
    S-14  撤销状态不许倒流
    S-15  取消不自动撤销，但不静默

PG 部分跑在 sqlite 上的 PG 方言替身，schema 直接读 `infrastructure/postgres/` 原文。
"""
from __future__ import annotations

import unittest
from typing import Any, Mapping

from packages.agent_domain.business.compensation import (
    CompensationRecord,
    CompensationSpec,
    CompensationStatus,
)
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Goal
from packages.agent_domain.intelligence.state import State
from packages.agent_harness.cost import Budget
from packages.agent_harness.harness import Harness
from packages.agent_harness.policy import PolicyEngine, PolicyRule, Verdict
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    ToolRegistry,
    ToolRuntime,
    ToolSpec,
)
from packages.execution_kernel.inmemory import ManualClock
from packages.execution_kernel.kernel import ExecutionKernel

from .sqlite_shim import connect, load_schema_sql
from .test_agent_loop_full import Planner
from .test_control_plane_api import build_gateway

_SCHEMA = ("001_kernel.sql", "005_compensations.sql")


# ---------------------------------------------------------------- 道具
UNDONE: list[str] = []
CREATED: list[str] = []


def create_order(args: Mapping[str, Any]) -> dict[str, Any]:
    order_id = f"o-{len(CREATED) + 1}"
    CREATED.append(order_id)
    return {"order_id": order_id, "status": "created"}


def create_invoice(args: Mapping[str, Any]) -> dict[str, Any]:
    return {"invoice_id": f"i-{len(CREATED) + 1}"}


def cancel_order(args: Mapping[str, Any]) -> dict[str, Any]:
    UNDONE.append(str(args.get("order_id")))
    return {"cancelled": True}


def cancel_invoice(args: Mapping[str, Any]) -> dict[str, Any]:
    UNDONE.append(str(args.get("invoice_id")))
    return {"cancelled": True}


def exploding_undo(args: Mapping[str, Any]) -> dict[str, Any]:
    raise RuntimeError("undo endpoint is down")


def build_tool_runtime(*, undo=cancel_order) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="create_order", version="1.0.0"),
                      FunctionInvoker(create_order))
    registry.register(ToolSpec(name="create_invoice", version="1.0.0"),
                      FunctionInvoker(create_invoice))
    registry.register(ToolSpec(name="cancel_order", version="1.0.0"),
                      FunctionInvoker(undo))
    registry.register(ToolSpec(name="cancel_invoice", version="1.0.0"),
                      FunctionInvoker(cancel_invoice))
    return ToolRuntime(registry)


class Interpreter:
    """预算压到 1 —— 第二步就会耗尽，于是 Run 判 FAILED，触发补偿。"""

    def __init__(self, max_steps: int = 1) -> None:
        self.max_steps = max_steps

    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Goal:
        return Goal(
            run_id=str(context.get("run_id", "")),
            objective="create an order",
            success_criteria=("order created",),
            budget=Budget(max_steps=self.max_steps),
        )


class ScriptedActions:
    """按脚本依次给出 Action，用完就 FINISH。

    传进来的是 `run_id -> Action` 的**工厂**：run_id 只有 Loop 起来之后才知道，
    写死会让 `Decision.selected_action belongs to another run` 直接炸。
    """

    def __init__(self, *factories) -> None:
        self._factories = list(factories)
        self.actions: list[Action] = []

    def decide(self, state: State) -> Decision:
        if not self._factories:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            )
        action = self._factories.pop(0)(state.run_id)
        self.actions.append(action)
        return Decision(run_id=state.run_id, selected_action=action)


def order_action(run_id: str, *, risk: RiskLevel = RiskLevel.LOW) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "create_order", "args": {"sku": "x"}},
        risk_level=risk,
        rationale="create an order",
        compensation=CompensationSpec(
            tool="cancel_order",
            description="cancel the order",
            result_keys=("result.order_id",),
        ),
    )


def invoice_action(run_id: str) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "create_invoice", "args": {"amount": 10}},
        rationale="create an invoice",
        compensation=CompensationSpec(
            tool="cancel_invoice",
            description="void the invoice",
            result_keys=("result.invoice_id",),
        ),
    )


class SagaTestBase(unittest.TestCase):
    def setUp(self) -> None:
        UNDONE.clear()
        CREATED.clear()
        self.clock = ManualClock()
        self.gateway = build_gateway()

    def _stack(self, *, actions, undo=cancel_order, max_steps: int = 1, harness=None):
        self.tool_runtime = build_tool_runtime(undo=undo)
        stack = assemble_runtime_stack(
            agent_id="agent-saga",
            interpreter=Interpreter(max_steps=max_steps),
            planner=Planner(),
            decision_engine=ScriptedActions(*actions),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
            harness=harness,
        )
        return stack

    def _run_to_failure(self, *, actions, undo=cancel_order, max_steps: int = 1, harness=None):
        """跑起来：第一步成功（产生副作用），第二步预算耗尽 → FAILED → 补偿。"""
        stack = self._stack(actions=actions, undo=undo, max_steps=max_steps, harness=harness)
        stack.loop.start("create an order")
        stack.loop.run()
        return stack


# ---------------------------------------------------------------- S-1
class CompensationIsExecutionTest(SagaTestBase):
    def test_s1_compensation_is_a_real_execution_not_a_log_line(self) -> None:
        stack = self._run_to_failure(actions=[order_action])
        self.assertEqual(stack.loop.agent_run.status.value, "failed")

        # 撤销工具真的被调用了
        self.assertEqual(UNDONE, ["o-1"])
        # 而且它是 Kernel 里一条**真的 COMPLETED 的 Execution** ——
        # 不是一行日志：它有 Attempt、有 Lease、走的是同一条调度路径。
        comp_tasks = [
            t for t in _all_tasks(stack)
            if t.payload.get("tool") == "cancel_order"
        ]
        self.assertEqual(len(comp_tasks), 1)
        execs = _executions_for(stack, comp_tasks[0].task_id)
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0].status, ExecutionStatus.COMPLETED)

    def test_s1_control_without_a_declared_compensation_nothing_is_undone(self) -> None:
        """对照：没声明补偿 → 什么都不撤销。证明上面那条不是白测的。"""
        def plain(run_id: str) -> Action:
            return Action(
                run_id=run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "create_order", "args": {"sku": "x"}},
                rationale="create an order",
            )

        stack = self._run_to_failure(actions=[plain])
        self.assertEqual(stack.loop.agent_run.status.value, "failed")
        self.assertEqual(UNDONE, [])
        self.assertEqual(list(stack.loop.compensations.open_for(stack.loop.state.run_id)), [])


# ---------------------------------------------------------------- S-3
class OrderTest(SagaTestBase):
    def test_s3_compensation_runs_in_reverse_order(self) -> None:
        """先建订单、再开发票 → 撤销时必须先撤发票。"""
        stack = self._run_to_failure(
            actions=[order_action, invoice_action],
            max_steps=2,
        )
        self.assertEqual(CREATED, ["o-1"])
        # LIFO：i-2 先撤销，o-1 后撤销
        self.assertEqual(UNDONE, ["i-2", "o-1"])


# ---------------------------------------------------------------- S-2 / S-8 / S-14
class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryCompensationStore()
        self.saga = SagaCoordinator(store=self.store)

    def _record(self, action: Action, *, result=None, status=ExecutionStatus.COMPLETED,
                failure_class=None, execution_id="exec_1") -> CompensationRecord | None:
        return self.saga.record(
            run_id="run_1", step_id="step_1", task_id="task_1",
            execution_id=execution_id, action=action, result=result or {},
            execution_status=status, failure_class=failure_class,
        )

    def test_s2_one_record_per_execution(self) -> None:
        action = order_action("run_1")
        first = self._record(action, result={"result": {"order_id": "o-1"}})
        self.assertIsNotNone(first)
        # 第二次登记（重跑 / 重复回调）不能再造一条 —— 否则撤销两次
        self.assertIsNone(self._record(action, result={"result": {"order_id": "o-1"}}))
        self.assertEqual(len(self.store.open_for("run_1")), 1)

    def test_s8_missing_result_key_is_unresolved_not_a_guess(self) -> None:
        """撤销需要 order_id，但正向没返回 → 记 UNRESOLVED，绝不带着空参数去撤销。"""
        record = self._record(order_action("run_1"), result={"result": {"status": "created"}})
        assert record is not None
        self.assertEqual(record.status, CompensationStatus.UNRESOLVED)
        self.assertIn("order_id", record.reason)

    def test_s8_unresolved_must_carry_a_reason(self) -> None:
        with self.assertRaises(InvariantViolation):
            CompensationRecord(
                run_id="run_1", execution_id="exec_1", tool="cancel_order",
                description="d", status=CompensationStatus.UNRESOLVED,
            )

    def test_s11_external_unknown_is_not_auto_compensated_nor_ignored(self) -> None:
        """副作用存疑：自动撤销是猜，"当没发生"也是猜 —— 唯一诚实的是记下来交给人。"""
        record = self._record(
            order_action("run_1"),
            status=ExecutionStatus.FAILED,
            failure_class="external_unknown",
        )
        assert record is not None
        self.assertEqual(record.status, CompensationStatus.UNRESOLVED)
        self.assertIn("EXTERNAL_UNKNOWN", record.reason)
        self.assertEqual(len(self.store.unresolved_for("run_1")), 1)

    def test_failed_without_unknown_produces_no_record(self) -> None:
        """PERMANENT 失败：认为没产生副作用，不登记。"""
        self.assertIsNone(
            self._record(order_action("run_1"), status=ExecutionStatus.FAILED,
                         failure_class="permanent")
        )

    def test_s14_compensated_cannot_go_back_to_pending(self) -> None:
        record = self._record(order_action("run_1"), result={"result": {"order_id": "o-1"}})
        assert record is not None
        self.store.claim(record.compensation_id)
        record.transition(CompensationStatus.COMPENSATED)
        with self.assertRaises(InvariantViolation):
            record.transition(CompensationStatus.PENDING)

    def test_s14_reopen_is_the_only_allowed_backflow(self) -> None:
        """人工把 UNRESOLVED 拉回 PENDING 重试：唯一允许的倒流。"""
        record = self._record(order_action("run_1"), result={"result": {"status": "created"}})
        assert record is not None
        reopened = self.saga.reopen(record.compensation_id)
        self.assertEqual(reopened.status, CompensationStatus.PENDING)


# ---------------------------------------------------------------- S-5 / S-6
class FailureHandlingTest(SagaTestBase):
    def test_s5_unresolved_is_visible_not_silent(self) -> None:
        stack = self._run_to_failure(actions=[order_action], undo=exploding_undo)
        # Run 照样判 FAILED（否则永远结束不了）
        self.assertEqual(stack.loop.agent_run.status.value, "failed")
        # 但账本里留着一条 UNRESOLVED，且有原因
        run_id = stack.loop.state.run_id
        unresolved = list(stack.loop.compensations.unresolved_for(run_id))
        self.assertEqual(len(unresolved), 1)
        self.assertTrue(unresolved[0].reason)
        # 并且 Trace 里有痕迹 —— 静默失败是这个项目一直在猎的东西
        self.assertIn("compensation.unresolved", stack.loop.trace.kinds())

    def test_s6_one_broken_undo_does_not_block_the_rest(self) -> None:
        """后发生的（发票）撤销失败，先发生的（订单）**照样要撤销**。

        停下来等于用一个局部故障换一批永久性副作用。
        """
        def flaky(args: Mapping[str, Any]) -> dict[str, Any]:
            if args.get("invoice_id"):
                raise RuntimeError("invoice void endpoint is down")
            UNDONE.append(str(args.get("order_id")))
            return {"cancelled": True}

        # 两个撤销工具都注册：让 cancel_invoice 炸、cancel_order 好
        registry = ToolRegistry()
        registry.register(ToolSpec(name="create_order", version="1.0.0"),
                          FunctionInvoker(create_order))
        registry.register(ToolSpec(name="create_invoice", version="1.0.0"),
                          FunctionInvoker(create_invoice))
        registry.register(ToolSpec(name="cancel_order", version="1.0.0"),
                          FunctionInvoker(cancel_order))
        registry.register(ToolSpec(name="cancel_invoice", version="1.0.0"),
                          FunctionInvoker(exploding_undo))
        self.tool_runtime = ToolRuntime(registry)

        stack = assemble_runtime_stack(
            agent_id="agent-saga",
            interpreter=Interpreter(max_steps=2),
            planner=Planner(),
            decision_engine=ScriptedActions(order_action, invoice_action),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
        )
        stack.loop.start("create things")
        stack.loop.run()

        self.assertEqual(UNDONE, ["o-1"])                 # 订单还是被撤销了
        self.assertEqual(
            len(stack.loop.compensations.unresolved_for(stack.loop.state.run_id)), 1
        )


# ---------------------------------------------------------------- S-9 / S-10 / S-12
class WiringTest(SagaTestBase):
    def test_s10_compensation_does_not_consume_the_step_budget(self) -> None:
        """预算耗尽恰恰是最需要清理的时刻 —— 清理不能跟正向动作抢同一份预算。"""
        stack = self._run_to_failure(actions=[order_action], max_steps=1)
        self.assertEqual(stack.loop.steps, 1)             # 撤销没被计成一步
        self.assertEqual(UNDONE, ["o-1"])

    def test_s12_compensation_lives_on_its_own_step(self) -> None:
        """挂原 Step 会把"这一步已完成"倒推成"还在跑"（Step 状态是从 Execution 投影的）。"""
        stack = self._run_to_failure(actions=[order_action])
        names = [s.name for s in stack.loop.steps_of_run]
        self.assertIn("compensate", names)
        compensation_step = next(s for s in stack.loop.steps_of_run if s.name == "compensate")
        self.assertEqual(compensation_step.task_count, 1)

    def test_s12_the_original_step_is_not_polluted(self) -> None:
        stack = self._run_to_failure(actions=[order_action])
        original = next(s for s in stack.loop.steps_of_run if s.name != "compensate")
        self.assertNotIn(
            original.step_id,
            [s.step_id for s in stack.loop.steps_of_run if s.name == "compensate"],
        )


class SuccessClosesTheLedgerTest(SagaTestBase):
    def test_s16_a_successful_run_leaves_nothing_pending(self) -> None:
        """成功 = 副作用按预期保留。账本里要是还写着"待撤销"，那是指向错的。"""
        # max_steps=2：第一步建单，第二步 FINISH（预算检查在 decide 之前，1 会直接判 FAILED）
        stack = self._stack(actions=[order_action], max_steps=2)
        stack.loop.start("create an order")
        self.assertEqual(stack.loop.step().value, "executed")
        # 这一步产生了副作用，账本里有一条待撤销
        run_id = stack.loop.state.run_id
        self.assertEqual(len(stack.loop.compensations.open_for(run_id)), 1)

        # 目标达成 → Run COMPLETED
        stack.loop.run()
        self.assertEqual(stack.loop.agent_run.status.value, "completed")

        self.assertEqual(list(stack.loop.compensations.open_for(run_id)), [])
        self.assertIn("compensation.released", stack.loop.trace.kinds())
        # 而且没有被撤销 —— 成功运行的副作用是**保留**，不是"撤销过了"
        self.assertEqual(UNDONE, [])
        self.assertEqual(CREATED, ["o-1"])

    def test_s16_control_the_record_was_really_pending_first(self) -> None:
        """对照：证明上面那条不是"压根没登记"造成的假绿。

        执行完那一步之后，账本里确实有一条 **PENDING**；结案只发生在 COMPLETED 之后。
        """
        stack = self._stack(actions=[order_action], max_steps=2)
        stack.loop.start("create an order")
        stack.loop.step()
        run_id = stack.loop.state.run_id

        pending = list(stack.loop.compensations.open_for(run_id))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, CompensationStatus.PENDING)
        self.assertNotIn("compensation.released", stack.loop.trace.kinds())


class PolicyTest(SagaTestBase):
    def test_s9_the_inverse_is_screened_together_with_the_forward_action(self) -> None:
        """S-9 从断言变成事实：撤销在补偿阶段是绕过 Harness 的，
        所以必须在这里就把逆操作审掉 —— 否则撤销是一条完全没过策略的动作。
        """
        # 默认就是 ALLOW，所以只需要一条禁令（priority 高者优先）
        policy = PolicyEngine(rules=(
            PolicyRule(name="no-cancel", verdict=Verdict.DENY,
                       tools=frozenset({"cancel_order"}),
                       reason="cancelling is not permitted for this tenant",
                       priority=10),
        ))
        harness = Harness(policy=policy)
        verdict = harness.before_action(order_action("run_1"))
        self.assertEqual(verdict.verdict, Verdict.DENY)
        self.assertIn("S-9", "; ".join(verdict.reasons))

    def test_s9_control_without_the_rule_the_action_is_allowed(self) -> None:
        """对照：没有那条禁令，同一个动作是放行的 —— 证明上面不是白测的。"""
        harness = Harness.default()
        self.assertTrue(
            harness.before_action(order_action("run_1")).verdict is Verdict.ALLOW
        )

    def test_s9_undo_does_not_wait_for_a_human(self) -> None:
        """高风险动作要人批准；但**撤销**不再等一次批准 ——
        否则"人走了、Run 失败了"会让副作用永久留着，而且不报错（系统在"等批准"）。
        """
        stack = self._stack(actions=[lambda rid: order_action(rid, risk=RiskLevel.HIGH)])
        stack.loop.start("create an order")
        # 正向先被闸门挡住 —— 这一步确实要人批准
        self.assertEqual(stack.loop.step().value, "waiting_approval")
        stack.loop.approve(by="alice")
        stack.loop.run()

        self.assertEqual(stack.loop.agent_run.status.value, "failed")
        # 撤销**没有**再去等一次批准：否则"人走了、Run 失败了"会让副作用永久留着，
        # 而且不报错 —— 系统确实在"等批准"。
        self.assertEqual(UNDONE, ["o-1"])
        self.assertEqual(len(stack.loop.approvals.pending(stack.loop.state.run_id)), 0)


# ---------------------------------------------------------------- S-13
class DeclarationTest(unittest.TestCase):
    def test_s13_llm_call_cannot_declare_a_compensation(self) -> None:
        """模型调用没有可撤销的外部状态 —— 给它声明补偿是句谎话。"""
        with self.assertRaises(InvariantViolation):
            Action(
                run_id="run_1",
                action_type=ActionType.LLM_CALL,
                payload={"model": "gpt"},
                compensation=CompensationSpec(tool="undo", description="undo"),
            )

    def test_s13_tool_call_can(self) -> None:
        action = order_action("run_1")
        self.assertIsNotNone(action.compensation)

    def test_spec_requires_a_tool_and_a_description(self) -> None:
        with self.assertRaises(InvariantViolation):
            CompensationSpec(description="undo")
        with self.assertRaises(InvariantViolation):
            CompensationSpec(tool="undo")


# ---------------------------------------------------------------- PG
class PostgresCompensationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        self.store = PostgresCompensationStore(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _record(self, execution_id: str, **over) -> CompensationRecord:
        base = dict(
            run_id="run_1", step_id="step_1", task_id="task_1",
            execution_id=execution_id, action_type="tool_call",
            tool="cancel_order", description="cancel the order",
        )
        base.update(over)
        record = CompensationRecord(**base)
        self.store.add(record)
        return record

    def test_schema_is_loadable_and_queryable(self) -> None:
        self.assertEqual(list(self.store.open_for("run_1")), [])

    def test_s2_unique_execution_id_is_enforced_by_the_database(self) -> None:
        """约定守不住（两个 Coordinator 各记一条），约束守得住。"""
        self._record("exec_1")
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self._record("exec_1")

    def test_s4_claim_is_atomic_the_loser_gets_nothing(self) -> None:
        """两个进程同时扫到同一条记录：只有一个能认领成功。"""
        record = self._record("exec_1")

        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        other = PostgresCompensationStore(self.conn)
        # 第二个进程先抢（它读到的是同一条 PENDING）
        winner = other.claim(record.compensation_id)
        self.assertIsNotNone(winner)
        # 第一个进程再抢 → 拿不到，而不是"撤销两次"
        self.assertIsNone(self.store.claim(record.compensation_id))

    def test_s4_the_loser_does_not_silently_compensate_again(self) -> None:
        """对照：证明上面那条不是白测的 —— 用两个**独立**的读视图模拟真并发。"""
        record = self._record("exec_1")
        a = self.store.get(record.compensation_id)
        b = self.store.get(record.compensation_id)
        assert a is not None and b is not None
        self.assertEqual(a.status, CompensationStatus.PENDING)
        self.assertEqual(b.status, CompensationStatus.PENDING)

        self.assertIsNotNone(self.store.claim(a.compensation_id))
        # b 手上那份还是 PENDING（它不知道），但认领会被数据库拒绝
        self.assertIsNone(self.store.claim(b.compensation_id))
        self.assertEqual(
            self.store.get(record.compensation_id).status, CompensationStatus.RUNNING
        )

    def test_unresolved_has_reason_is_enforced_by_the_database(self) -> None:
        import sqlite3

        cur = self.conn.cursor()
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO compensations (compensation_id, run_id, step_id, task_id,"
                " execution_id, action_type, tool, description, status, reason)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("cmp_x", "run_1", "s", "t", "exec_x", "tool_call", "cancel_order",
                 "d", "unresolved", ""),
            )

    def test_pending_returns_in_reverse_order(self) -> None:
        self._record("exec_1")
        self._record("exec_2")
        ids = [r.execution_id for r in self.store.open_for("run_1")]
        self.assertEqual(ids, ["exec_2", "exec_1"])

    def test_save_detects_a_stale_writer(self) -> None:
        record = self._record("exec_1")
        stale = self.store.get(record.compensation_id)
        fresh = self.store.get(record.compensation_id)
        assert stale is not None and fresh is not None

        fresh.transition(CompensationStatus.RUNNING)
        self.store.save(fresh)

        stale.transition(CompensationStatus.RUNNING)
        from packages.agent_domain.errors import ConcurrentStateError

        with self.assertRaises(ConcurrentStateError):
            self.store.save(stale)


# ---------------------------------------------------------------- 工具
def _all_tasks(stack) -> list:
    """从 Kernel 里把所有 Task 翻出来（Executor 层没有反查接口，走 repository）。"""
    kernel: ExecutionKernel = stack.kernel
    return list(getattr(kernel, "_tasks", {}).values())


def _executions_for(stack, task_id: str) -> list:
    kernel: ExecutionKernel = stack.kernel
    execution = kernel.repository.get_by_task(task_id)
    return [execution] if execution is not None else []


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
