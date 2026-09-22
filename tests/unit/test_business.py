"""Business Domain：AgentRun / Step / 状态派生（基线 §3.1 / §4 / §9.2 / §10）。

`AgentRun` 和 `Step` 之前在代码里**完全没有落点** —— Loop 只有一个 `run_id` 字符串，
于是整条 `AgentRun : Step = 1 : N : Task = 1 : N` 的基数链（§4，P0）在最上面一环是断的。

这里验证的重点是**"状态不是被设置的"**这件事是否真的守住了：

    B-1  AgentRun 必须带 goal + agent_id
    B-2  AgentRun.status 是派生值，不可直接赋值
    B-3  Run 终态不可变
    B-4  SUSPENDED 必须说清楚在等什么
    B-5  Step.status 由所属 Task 派生（且 Step 必须挂在某个 Plan Node 上）
    B-6  Step : Task = 1 : N（扇出的支撑点）
    B-7  Run 的终态只能由 Runtime 声明，不能从 Step 派生
"""
from __future__ import annotations

import unittest
from dataclasses import fields

from packages.agent_domain.business import (
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    AgentRun,
    AgentRunStateMachine,
    AgentRunStatus,
    Step,
    StepStatus,
    derive_run_status,
    derive_step_status,
)
from packages.agent_domain.errors import IllegalTransition, InvariantViolation
from packages.agent_domain.execution import ExecutionStatus
from packages.agent_domain.execution.execution import SuspensionReason
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_harness import Harness
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.task_factory import TaskFactory

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


def goal(run_id: str = "run_b", objective: str = "answer it") -> Goal:
    return Goal(
        run_id=run_id,
        objective=objective,
        success_criteria=("answer is produced",),
        budget=Budget(max_steps=6),
    )


def make_run(**kw) -> AgentRun:
    kw.setdefault("agent_id", "agent-1")
    kw.setdefault("goal", goal(kw.get("run_id", "run_b")))
    return AgentRun(**kw)


def make_step(**kw) -> Step:
    kw.setdefault("run_id", "run_b")
    kw.setdefault("plan_node_id", "n0")
    return Step(**kw)


# ============================================================ B-1 / B-2


class AgentRunTest(unittest.TestCase):
    def test_b1_goal_is_required(self) -> None:
        with self.assertRaises(InvariantViolation):
            AgentRun(run_id="run_x", agent_id="agent-1")

    def test_b1_agent_id_is_required(self) -> None:
        with self.assertRaises(InvariantViolation):
            AgentRun(run_id="run_x", goal=goal("run_x"))

    def test_b2_status_cannot_be_assigned(self) -> None:
        run = make_run()
        with self.assertRaises(InvariantViolation):
            run.status = AgentRunStatus.RUNNING

    def test_b2_suspension_reason_cannot_be_assigned(self) -> None:
        """连"为什么挂起"都不能手填 —— 它和 status 一样是派生出来的。"""
        run = make_run()
        with self.assertRaises(InvariantViolation):
            run.suspension_reason = SuspensionReason.HUMAN_APPROVAL

    def test_b3_terminal_is_immutable(self) -> None:
        run = make_run()
        run.sync([], runtime_terminal=AgentRunStatus.COMPLETED)
        self.assertIs(run.status, AgentRunStatus.COMPLETED)
        # M51：抛的是 `IllegalTransition`，不是 `InvariantViolation`。
        #
        # 两者在 API 层映射成不同的码（409 vs 422）：
        # B-3 是"状态明确但转换不允许"，不是"请求本身不合法"。
        # 报成 422 会让并发推进拿到与单线程推进不同的错误码（见 §89）。
        with self.assertRaises(IllegalTransition) as ctx:
            run.sync([], runtime_terminal=AgentRunStatus.FAILED)
        self.assertIn("B-3", str(ctx.exception))

    def test_b3_terminal_statuses_are_the_three_expected(self) -> None:
        self.assertEqual(
            TERMINAL_RUN_STATUSES,
            frozenset(
                {
                    AgentRunStatus.COMPLETED,
                    AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED,
                }
            ),
        )

    def test_b4_suspended_always_carries_a_reason(self) -> None:
        step = make_step()
        step.sync([ExecutionStatus.SUSPENDED])
        run = make_run()
        run.sync([step])
        self.assertIs(run.status, AgentRunStatus.SUSPENDED)
        self.assertIsNotNone(run.suspension_reason)

    def test_b4_leaving_suspended_clears_the_reason(self) -> None:
        step = make_step()
        step.sync([ExecutionStatus.SUSPENDED])
        run = make_run()
        run.sync([step])
        self.assertIsNotNone(run.suspension_reason)

        step.sync([ExecutionStatus.COMPLETED])
        run.sync([step])
        self.assertIs(run.status, AgentRunStatus.RUNNING)     # B-7
        self.assertIsNone(run.suspension_reason)


# ============================================================ B-5 / B-6


class StepTest(unittest.TestCase):
    def test_b5_step_must_be_a_plan_node_instance(self) -> None:
        """Step 是 Plan Node 的**运行实例**，所以 plan_node_id 不是可选装饰。"""
        with self.assertRaises(InvariantViolation):
            Step(run_id="run_b")
        with self.assertRaises(InvariantViolation):
            Step(plan_node_id="n0")

    def test_b5_status_cannot_be_assigned(self) -> None:
        step = make_step()
        with self.assertRaises(InvariantViolation):
            step.status = StepStatus.COMPLETED

    def test_b5_no_stale_in_business_view(self) -> None:
        """STALE 是 Lease 过期的产物，属于 Kernel；Step 只看"做完没有"。"""
        self.assertFalse(hasattr(StepStatus, "STALE"))
        self.assertTrue(hasattr(ExecutionStatus, "STALE"))
        # STALE 在派生里被折叠成 PENDING（这一步还没做完）
        step = make_step()
        self.assertIs(step.sync([ExecutionStatus.STALE]), StepStatus.PENDING)

    def test_b6_one_step_many_tasks(self) -> None:
        step = make_step()
        for i in range(3):
            step.add_task(f"task_{i}")
        self.assertEqual(step.task_count, 3)
        self.assertEqual(step.task_ids, ("task_0", "task_1", "task_2"))

    def test_b6_add_task_is_idempotent(self) -> None:
        step = make_step()
        step.add_task("task_0")
        step.add_task("task_0")
        self.assertEqual(step.task_count, 1)

    def test_step_status_aggregates_its_tasks(self) -> None:
        step = make_step()
        self.assertIs(step.sync([]), StepStatus.PENDING)              # 还没派活
        self.assertIs(step.sync([ExecutionStatus.RUNNING]), StepStatus.RUNNING)
        self.assertIs(
            step.sync([ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED]),
            StepStatus.RUNNING,                                       # 活跃态优先
        )
        self.assertIs(
            step.sync([ExecutionStatus.FAILED, ExecutionStatus.RUNNING]),
            StepStatus.RUNNING,                                       # 同上
        )
        self.assertIs(step.sync([ExecutionStatus.COMPLETED]), StepStatus.COMPLETED)
        self.assertIs(step.sync([ExecutionStatus.FAILED]), StepStatus.FAILED)
        self.assertIs(step.sync([ExecutionStatus.CANCELLED]), StepStatus.CANCELLED)

    def test_retryable_failure_returns_step_to_pending(self) -> None:
        """可重试失败让 Execution 回到 PENDING，Step 必须跟着回去，不能判 FAILED。

        否则"曾经 RUNNING 过 + 现在 PENDING"会被误判成失败，
        Scheduler 重新拉起来的时候 Step 已经是终态了。
        """
        step = make_step()
        step.sync([ExecutionStatus.RUNNING])
        self.assertIs(step.sync([ExecutionStatus.PENDING]), StepStatus.PENDING)

    def test_terminal_step_statuses(self) -> None:
        self.assertEqual(
            TERMINAL_STEP_STATUSES,
            frozenset({StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED}),
        )


# ============================================================ 派生表


class DeriveTest(unittest.TestCase):
    def test_step_derivation_table(self) -> None:
        cases = [
            ((), StepStatus.PENDING),
            ((ExecutionStatus.PENDING,), StepStatus.PENDING),
            ((ExecutionStatus.STALE,), StepStatus.PENDING),
            ((ExecutionStatus.RUNNING,), StepStatus.RUNNING),
            ((ExecutionStatus.SUSPENDED,), StepStatus.SUSPENDED),
            ((ExecutionStatus.PENDING, ExecutionStatus.SUSPENDED), StepStatus.SUSPENDED),
            ((ExecutionStatus.CANCELLED,), StepStatus.CANCELLED),
            ((ExecutionStatus.FAILED,), StepStatus.FAILED),
            ((ExecutionStatus.COMPLETED,), StepStatus.COMPLETED),
            ((ExecutionStatus.COMPLETED, ExecutionStatus.FAILED), StepStatus.FAILED),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self.assertIs(derive_step_status(list(statuses)), expected)

    def test_run_derivation_table(self) -> None:
        cases = [
            ((), AgentRunStatus.CREATED),
            ((StepStatus.PENDING,), AgentRunStatus.QUEUED),
            ((StepStatus.RUNNING,), AgentRunStatus.RUNNING),
            ((StepStatus.SUSPENDED,), AgentRunStatus.SUSPENDED),
            ((StepStatus.PENDING, StepStatus.RUNNING), AgentRunStatus.RUNNING),
            ((StepStatus.PENDING, StepStatus.SUSPENDED), AgentRunStatus.SUSPENDED),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self.assertIs(derive_run_status(list(statuses)), expected)

    def test_b7_all_steps_done_does_not_mean_run_is_done(self) -> None:
        """B-7 的核心用例：Step 全绿，但 Runtime 还没宣布终态 → Run 仍然 RUNNING。

        反例就是"Agent 还在思考"：思考不产生 Task，在派生里是看不见的。
        如果这里派生出 COMPLETED，后面 Runtime 想标 FAILED（放弃 / 预算耗尽）
        会被 B-3 顶回来 —— 等于 Kernel 替 Runtime 决定了什么时候收工。
        """
        for terminal in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED):
            with self.subTest(terminal=terminal):
                self.assertIs(
                    derive_run_status([terminal, terminal]), AgentRunStatus.RUNNING
                )

    def test_b7_runtime_terminal_wins(self) -> None:
        self.assertIs(
            derive_run_status(
                [StepStatus.RUNNING], runtime_terminal=AgentRunStatus.CANCELLED
            ),
            AgentRunStatus.CANCELLED,
        )

    def test_run_is_created_before_any_step_exists(self) -> None:
        self.assertIs(derive_run_status([]), AgentRunStatus.CREATED)


# ============================================================ 状态机只校验


class StateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sm = AgentRunStateMachine()

    def test_can_transition_never_initiates(self) -> None:
        """§9.2：Kernel/Runtime 提供机制与策略的分离 ——
        这里只有 `can_transition`，没有任何 `transition()`。"""
        self.assertFalse(hasattr(self.sm, "transition"))
        self.assertFalse(hasattr(self.sm, "to"))

    def test_first_step_gated_before_anything_runs(self) -> None:
        """基线 §10 的生命周期图没画 CREATED → SUSPENDED，但它是真实的一条边：
        Run 创建后的第一个动作就被判成 REQUIRE_APPROVAL。

        之所以不能靠"先采样一次 RUNNING"绕开：投影必须是**顺序无关的纯函数**，
        从 Checkpoint 恢复时采不到中间那一帧，同一个状态会投影出两种结果。
        """
        self.assertTrue(
            self.sm.can_transition(AgentRunStatus.CREATED, AgentRunStatus.SUSPENDED)
        )

    def test_illegal_jump_is_rejected(self) -> None:
        with self.assertRaises(IllegalTransition):
            self.sm.can_transition(AgentRunStatus.COMPLETED, AgentRunStatus.RUNNING)

    def test_terminal_cannot_leave(self) -> None:
        for st in TERMINAL_RUN_STATUSES:
            with self.subTest(status=st):
                self.assertTrue(self.sm.is_terminal(st))
                with self.assertRaises(IllegalTransition):
                    self.sm.can_transition(st, AgentRunStatus.RUNNING)

    def test_same_status_is_always_allowed(self) -> None:
        """投影是幂等的 —— 同一个值再投影一次不能炸。"""
        for st in AgentRunStatus:
            self.assertTrue(self.sm.can_transition(st, st))


# ============================================================ 接进 Loop


class RunProjectionTest(LoopTestBase):
    def _loop(self, script, *, max_steps: int = 6, harness=None, **config):  # type: ignore[override]
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=max_steps),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine(script),
            config=AgentLoopConfig(max_steps=max_steps, **config),
            harness=harness,
        )

    def _tool(self, run_id: str, expr: str = "1+1") -> Action:
        return Action(
            run_id=run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": expr}},
        )

    def test_start_creates_a_real_run(self) -> None:
        loop = self._loop([])
        state = loop.start("2+3=?")
        run = loop.agent_run
        assert run is not None
        self.assertEqual(run.run_id, state.run_id)
        self.assertIs(run.status, AgentRunStatus.CREATED)
        self.assertIs(run.goal, state.goal)

    def test_run_becomes_running_after_a_task(self) -> None:
        loop = self._loop([])
        state = loop.start("2+3=?")
        loop.decision_engine = ScriptedDecisionEngine([self._tool(state.run_id)])
        self.assertIs(loop.step(), StepOutcome.EXECUTED)
        self.assertIs(loop.agent_run.status, AgentRunStatus.RUNNING)

    def test_b6_tasks_of_one_action_share_the_step(self) -> None:
        """B-6：同一个 Step 上挂了闸门 Task 和被放行的 Task（1:N，不是 1:1）。

        如果 `_execute` 不把 Task 挂到当前 Step 上，`TaskFactory` 默认会
        `new_step_id()` —— 那等于每个 Task 开一个 Step，扇出能力悄悄消失。
        """
        loop = self._loop([], harness=Harness.default())
        state = loop.start("do something risky")
        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = ScriptedDecisionEngine([risky])

        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertIs(loop.approve(by="alice"), StepOutcome.EXECUTED)

        self.assertEqual(len(loop.steps_of_run), 1)
        self.assertEqual(loop.steps_of_run[0].task_count, 2)   # 闸门 + 真动作

    def test_finish_declares_the_terminal(self) -> None:
        """Goal 达成是 Runtime 的判断 —— 下层没有任何记录会说"Run 完成了"。"""
        loop = self._loop([])
        loop.start("2+3=?")
        self.assertIs(loop.step(), StepOutcome.FINISHED)
        self.assertIs(loop.agent_run.status, AgentRunStatus.COMPLETED)

    def test_budget_exhaustion_declares_failed(self) -> None:
        loop = self._loop([], max_steps=2)
        state = loop.start("loop forever")
        actions = [self._tool(state.run_id, "1+1") for _ in range(4)]
        loop.decision_engine = ScriptedDecisionEngine(actions)

        self.assertIs(loop.step(), StepOutcome.EXECUTED)
        self.assertIs(loop.step(), StepOutcome.EXECUTED)
        # B-7：此刻两个 Step 都 COMPLETED，但 Run 仍然是 RUNNING ——
        # 如果这里已经被派生成 COMPLETED，下一步就会被 B-3 顶回来。
        self.assertIs(loop.agent_run.status, AgentRunStatus.RUNNING)
        self.assertIs(loop.step(), StepOutcome.BUDGET_EXHAUSTED)
        self.assertIs(loop.agent_run.status, AgentRunStatus.FAILED)

    def test_suspension_is_projected_up_to_the_run(self) -> None:
        loop = self._loop([], harness=Harness.default())
        state = loop.start("do something risky")
        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = ScriptedDecisionEngine([risky])

        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)
        run = loop.agent_run
        assert run is not None
        self.assertIs(run.status, AgentRunStatus.SUSPENDED)
        self.assertIsNotNone(run.suspension_reason)
        self.assertIs(loop.steps_of_run[0].status, StepStatus.SUSPENDED)

    def test_run_checkpoint_is_written_before_suspending(self) -> None:
        """§14：进入 SUSPENDED 前**必须**落 Run Checkpoint。

        漏了它，唤醒后只能重跑整个 Run —— 对已经产生过外部副作用的 Task 是灾难。
        """
        loop = self._loop([], harness=Harness.default())
        state = loop.start("do something risky")
        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = ScriptedDecisionEngine([risky])

        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)
        cp = loop.checkpoints.latest(state.run_id)
        assert cp is not None
        self.assertEqual(cp.current_step, loop.steps_of_run[0].step_id)
        self.assertEqual(cp.variables["reason"], "suspending for approval")

    def test_e24_kernel_does_not_know_what_a_step_is(self) -> None:
        """E-24：Kernel Checkpoint 里不能有 current_step / completed_tasks。

        这是"Kernel 不知道什么是 Step"这条边界的可断言形式。
        """
        from packages.agent_domain.execution import KernelCheckpoint

        names = {f.name for f in fields(KernelCheckpoint)}
        self.assertNotIn("current_step", names)
        self.assertNotIn("completed_tasks", names)

    def test_step_has_no_scheduling_semantics(self) -> None:
        """Step 只是逻辑分组 —— Lease / Retry / Worker / Cancellation 全在 Kernel。"""
        names = {f.name for f in fields(Step)}
        for forbidden in ("lease", "attempt", "worker", "retry", "fencing_token"):
            self.assertNotIn(forbidden, names)


# ============================================================ TaskFactory 侧


class StepTaskCardinalityTest(unittest.TestCase):
    def test_default_factory_would_break_1_to_n(self) -> None:
        """反证：不传 step_id 时每个 Task 一个新 Step —— 1:N 被降成 1:1。

        这条测试把那个"悄悄退化"钉住，让人一眼看出为什么 `_execute` 必须传 step_id。
        """
        factory = TaskFactory()
        action = Action(
            run_id="run_c",
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "1+1"}},
        )
        t1 = factory.from_action(action)
        t2 = factory.from_action(action)
        self.assertNotEqual(t1.step_id, t2.step_id)

        with_step = factory.from_action(action, step_id="step_fixed")
        self.assertEqual(with_step.step_id, "step_fixed")


if __name__ == "__main__":
    unittest.main()
