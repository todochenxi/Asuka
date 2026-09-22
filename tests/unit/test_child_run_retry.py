"""M31 / 空洞 218：委派失败与取消的**重试语义**。

--------------------------------------------------------------------------
起因：一个探针跑出来的事实链

    子 Run 进终态（failed / cancelled）
      → `child_failed()` → `_close_child_gate(completed=False)`
      → `kernel.fail(TRANSIENT)` → **父 Execution 回到 PENDING（attempt=2）**
      → Worker 领走 → `AgentDelegationExecutor` 拒绝
      → FAILED（attempt=3），终态 error = `DELEGATION_NOT_WORKER_EXECUTABLE`

三重损害：

  1. **真因被抹掉**。排障第一入口（父 Execution 的终态 error）读到的是
     "拥有你的 Loop 不在了"，真实原因"子 Run 没做成"消失了。
     与 PR-19（`HUMAN_APPROVAL` 被 `ToolCallExecutor` 以 `BAD_PAYLOAD` 拒掉）
     是同一类错：一句既不对又误导的话。
  2. **重试必然失败**。委派 Execution 在 Worker 侧只有一种归宿
     （`DeferringExecutor` 拒绝），所以那个 TRANSIENT 重试 100% 以 PERMANENT
     收场 —— 纯浪费一次 Lease、一次 Attempt、两条事件。
  3. **取消被当成可重试失败**（S-15）。取消是"到此为止"，
     把它塞进 TRANSIENT 重试等于把一个被叫停的动作再跑一遍。

--------------------------------------------------------------------------
    D-9  委派动作的失败在父侧**不可重试**。重试的判据归子 Run 自己的 Kernel
         （它有自己的 Attempt 与 RetryPolicy）；子 Run 进终态意味着那套预算
         已经判过了，而 D-1 保证父侧重试拿回的是**同一条**子 Run
    D-10 取消不是失败，尤其不可重试（S-15）。
         failed → PERMANENT（预算判过了）；cancelled → POLICY_DENIED（有人决定到此为止）。
         两者刻意不同：排障方向不一样
    D-11 不得挂起在一条**已经有结果**的子 Run 上（兜底，不可达）

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from packages.agent_domain.business.snapshot import state_from_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_domain.execution.retry import (
    RETRYABLE_FAILURE_CLASSES,
    FailureClass,
    RetryPolicy,
)
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import (
    ChildRunRegistry,
    ChildRunRequest,
    InProcessChildRunSpawner,
)
from packages.agent_runtime.executors import (
    AgentDelegationExecutor,
    LLMCallExecutor,
    SkillExecutor,
    TaskTypeRouter,
    ToolCallExecutor,
)
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import CHILD_OUTCOME_FAILURE, AgentLoop, StepOutcome
from packages.agent_runtime.recovery import InMemoryRunSnapshotStore, RunRecovery
from packages.agent_runtime.saga import (
    InMemoryCompensationStore,
    SagaCoordinator,
)
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
    Scheduler,
    Worker,
    WorkerConfig,
)

from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)


def _delegation(target: str = "researcher") -> Action:
    return Action(
        run_id="run_parent",
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": target},
    )


@dataclass
class _TerminalSpawner:
    """`spawn()` 一律交回一条**已经终态**的子 Run —— D-11 用。

    生产里不可达（D-9 已经关掉重试），所以只能靠测试双把它造出来。
    这不是"造一个方便的对象"，而是把"D-1 把重试和同一条子 Run 绑死"
    这个耦合本身推到台面上。
    """

    inner: Any
    registry: Any
    status: str = "failed"

    def spawn(self, request: ChildRunRequest) -> Any:
        handle = self.inner.spawn(request)
        return self.registry.mark_finished(
            handle.child_run_id, self.status, {"summary": "already over"}
        )


class RetryWorld(unittest.TestCase):
    """一个**真跑起来**的父子世界，且带一个真的 Worker。

    必须带 Worker：空洞 218 的后果（真因被 `DELEGATION_NOT_WORKER_EXECUTABLE`
    盖掉）**只有 Worker 真正把那条重试 Attempt 领走才会发生**。
    没有 Worker 的世界里，"重试"只是停留在 PENDING 上看不出后果 ——
    那正是它一直没被发现的原因（PR-28 同款理由）。
    """

    def setUp(self) -> None:
        self.registry = ChildRunRegistry()
        self.snapshots = InMemoryRunSnapshotStore()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=ManualClock(),
        )
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors={
                "native": TaskTypeRouter(
                    {
                        TaskType.TOOL_CALL: ToolCallExecutor(_tool_runtime()),
                        TaskType.SKILL: SkillExecutor(),
                    },
                    executor_type="native",
                ),
                "http": TaskTypeRouter(
                    {TaskType.LLM_CALL: LLMCallExecutor(_gateway())},
                    executor_type="http",
                ),
                "agent_runtime": TaskTypeRouter(
                    {TaskType.AGENT_DELEGATION: AgentDelegationExecutor()},
                    executor_type="agent_runtime",
                ),
            },
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )
        self.recovery = RunRecovery(
            snapshots=self.snapshots, factory=self._factory, approvals=None
        )
        self.compensations = InMemoryCompensationStore()
        self.waker = ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )
        self.decisions = ScriptedDecisionEngine([_delegation()])

    # ------------------------------------------------------------ 装配

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        from packages.agent_runtime.assembly import assemble_runtime_stack

        # 唤醒路径是靠 `rebuild()` 拿回父 Run 的，而 `rebuild()` 会**调这个工厂**
        # 造一条全新的栈。所以父 Run 的决策脚本必须由**共享的**那个 engine 供，
        # 否则重建出来的父 Run 拿到一份空脚本，下一步就直接 FINISH ——
        # 于是"失败之后还能换条路"永远测不到（把测试写成"它果然走不下去了"）。
        engine = (
            self.decisions if agent_id == "parent" else ScriptedDecisionEngine([])
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=engine,
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
        )

    def _parent(self, script: list[Action] | None = None) -> AgentLoop:
        from packages.agent_runtime.assembly import assemble_runtime_stack

        self.decisions = ScriptedDecisionEngine(
            script if script is not None else [_delegation()]
        )
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=self.decisions,
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id="run_parent")
        return stack.loop

    def _spawned(self, script: list[Action] | None = None) -> tuple[AgentLoop, str]:
        loop = self._parent(script)
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        return loop, loop.pending_child.child_run_id

    def _rebuilt(self) -> AgentLoop:
        """交付发生在**重建出来**的那条 Run 上（D-5：父 Run 是被唤醒的，不是原地等着的）。

        所以凡是要看"交付之后父 Run 变成什么样"的断言，
        都必须读重建后的对象 —— 手上那个 `loop` 是挂起时的那一份，
        它的 `pending_child` 还在（闸门没被关），读它会得到一个
        "什么都没发生"的假象。

        ----------------------------------------------------------
        D-27 之后这条只在"父 Run 还没被推到终态"时可用

        唤醒路径现在会**自己把父 Run 推走**（空洞 217）。推到终态之后，
        R-3 禁止恢复终态 Run —— `rebuild()` 会抛 `IllegalTransition`。
        要读"它现在什么样"时改用 `_state_after()`。
        """
        return self.recovery.rebuild("run_parent").loop

    def _state_after(self) -> Any:
        """推进之后父 Run 的 State —— 从**最新快照**读，不走 `rebuild()`。

        D-27 让交回结果的人继续把父 Run 推走，推到终态之后 R-3 就挡住了
        `rebuild()`。但"它现在相信什么"这件事**本来就该问存储**：
        快照是事实，`rebuild()` 是"再来一次"的能力 ——
        问事实的那一路不该被"能不能重来"挡住。
        """
        snapshot = self.snapshots.latest("run_parent")
        assert snapshot is not None
        return state_from_dict(snapshot.state)

    def _child_outcome(self) -> Any:
        """那一条"子 Run 完事了"的 Observation（D-10：failed / cancelled 不同）。

        刻意**不**取 `observations[-1]`：父 Run 被推走之后末尾那一条是
        `run.finished`，不是它。按 `content["outcome"]` 找，找的是
        **那一件事**，不是"最后一件事"。
        """
        matches = [
            o
            for o in self._state_after().observations
            if "outcome" in (o.content or {})
        ]
        self.assertTrue(matches, "State 里没有留下子 Run 的结局")
        return matches[-1]

    # ------------------------------------------------------------ 断言助手

    def _pending(self) -> list[str]:
        return [
            e.execution_id
            for e in self.kernel.repository.all()
            if e.status is ExecutionStatus.PENDING
        ]

    def _terminal_error(self, execution_id: str) -> Any:
        """父 Execution **最后**一次 Attempt 的 ErrorInfo —— 排障的第一入口。"""
        ex = self.kernel.repository.get(execution_id)
        assert ex is not None
        attempt = self.kernel.attempts.get(execution_id, ex.current_attempt_no)
        assert attempt is not None
        return attempt.error


# ---------------------------------------------------------------- D-9


class DelegationFailureIsNotRetryableTest(RetryWorld):
    def test_a_failed_child_does_not_send_the_parent_back_to_pending(self) -> None:
        """D-9 的主断言：父 Execution 不再回到 PENDING 等重试。

        修之前：`PENDING after child terminal: 1`，Worker 领走后
        `FAILED attempt=3`。修之后：直接 `FAILED attempt=2`。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "failed", {"summary": "budget out"})
        self.waker.wake(child_id)

        self.assertEqual(self._pending(), [], "委派失败不得产生新的可调度 Attempt")

    def test_the_real_cause_survives_to_the_terminal_error(self) -> None:
        """PR-19 同款：终态 error 必须是**子 Run 失败**，不是 Worker 的拒绝。

        这是空洞 218 最要紧的一条。修之前这里读到的
        `DELEGATION_NOT_WORKER_EXECUTABLE`（"拥有你的 Loop 不在了"）
        会让人去查"父 Run 的 Loop 怎么没了"，
        而真相只是"子 Run 没做成" —— 排障方向整个错了。
        """
        loop, child_id = self._spawned()
        execution_id = loop.pending_child.parent_execution_id
        self.registry.mark_finished(child_id, "failed", {"summary": "budget out"})
        self.waker.wake(child_id)

        error = self._terminal_error(execution_id)
        assert error is not None
        self.assertEqual(error.code, "CHILD_RUN_FAILED")
        self.assertIs(error.failure_class, FailureClass.PERMANENT)

    def test_the_worker_never_gets_a_second_chance_at_it(self) -> None:
        """控制组方向：Worker 真的领不到那条 Execution。

        只断言"没回到 PENDING"还不够 —— 那是**状态**层面的。
        这条走的是**调度**层面：让 Worker 真去捞一次，捞不到东西。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)

        self.assertEqual(self.worker.run_once(limit=10), {})

    def test_the_control_retry_is_not_turned_off_globally(self) -> None:
        """控制组：D-9 关的是**委派**的重试，不是整个系统的重试。

        没有这条，"委派不可重试"和"我把 TRANSIENT 从可重试集合里删了"
         在测试上无法区分 —— 后者会让所有瞬时故障都变成终态。
        """
        self.assertIn(FailureClass.TRANSIENT, RETRYABLE_FAILURE_CLASSES)
        self.assertTrue(RetryPolicy().is_retryable(FailureClass.TRANSIENT))

    def test_the_control_both_delegation_outcomes_are_non_retryable(self) -> None:
        """两个不成功终态都不可重试；`completed` 不在表里（它走 complete）。"""
        policy = RetryPolicy()
        for outcome, (_code, cls) in CHILD_OUTCOME_FAILURE.items():
            self.assertFalse(
                policy.is_retryable(cls), f"{outcome} 竟然可重试: {cls}"
            )
        self.assertNotIn("completed", CHILD_OUTCOME_FAILURE)


# ---------------------------------------------------------------- D-10


class CancellationIsNotFailureTest(RetryWorld):
    def test_a_cancelled_child_gets_its_own_error_code(self) -> None:
        """D-10：取消不是失败（S-15），所以它有**自己的** error code。"""
        loop, child_id = self._spawned()
        execution_id = loop.pending_child.parent_execution_id
        self.registry.mark_finished(child_id, "cancelled", {})
        self.waker.wake(child_id)

        error = self._terminal_error(execution_id)
        assert error is not None
        self.assertEqual(error.code, "CHILD_RUN_CANCELLED")
        self.assertIs(error.failure_class, FailureClass.POLICY_DENIED)

    def test_cancelled_and_failed_do_not_share_a_failure_class(self) -> None:
        """D-10 的那一半：两个终态刻意不同。

        并成一个值之后，排障只能去 payload 里找区别，而没有人会去找。
        """
        failed_cls = CHILD_OUTCOME_FAILURE["failed"][1]
        cancelled_cls = CHILD_OUTCOME_FAILURE["cancelled"][1]
        self.assertIs(failed_cls, FailureClass.PERMANENT)
        self.assertIs(cancelled_cls, FailureClass.POLICY_DENIED)
        self.assertNotEqual(failed_cls, cancelled_cls)

    def test_the_waker_routes_cancellation_to_its_own_gate(self) -> None:
        """唤醒路径的三个终态三个入口 —— 取消不能走 `child_failed`。

        判据取 **Observation**：它是进父 Run 的 State、给模型看的东西。
        模型读到 "child agent run X failed" 会以为该换个 Agent 再试，
        而真相是"有人把它叫停了" —— 那是完全不同的决策。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "cancelled", {})
        self.waker.wake(child_id)

        last = self._child_outcome()
        self.assertIn("cancelled", last.summary)
        self.assertEqual(last.content.get("outcome"), "cancelled")

    def test_the_control_a_failure_still_reads_as_a_failure(self) -> None:
        """控制组方向：`failed` 的 Observation 说的是 failed，不是 cancelled。"""
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)

        last = self._child_outcome()
        self.assertIn("failed", last.summary)
        self.assertEqual(last.content.get("outcome"), "failed")


# ---------------------------------------------------------------- D-9 的边界


class ReplanningIsStillAllowedTest(RetryWorld):
    """D-9 关的是"同一个 Execution 再来一次"，不是"父 Agent 换条路"。

    `retry.py` 开头把 Retry / Recovery / **Agent Replanning** 分成了三件事。
    这组测试钉住第三条还活着 —— 否则"不可重试"会被误读成"父 Run 到此为止"。
    """

    def test_the_parent_can_delegate_to_a_different_target(self) -> None:
        """失败之后换目标再派：新 Task → 新 execution_id → D-1 派生**新**子 Run。"""
        loop, child_id = self._spawned(
            [_delegation("researcher"), _delegation("writer")]
        )
        first_execution = loop.pending_child.parent_execution_id
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)

        # 换目标必须由**被唤醒的那条**父 Run 发起 —— 见 `_rebuilt()` 的注释。
        parent = self._rebuilt()
        self.assertIs(parent.step(), StepOutcome.WAITING_CHILD)
        second = parent.pending_child
        assert second is not None
        self.assertNotEqual(second.child_run_id, child_id, "换目标必须派出新的一条")
        self.assertEqual(second.target, "writer")
        self.assertNotEqual(second.parent_execution_id, first_execution)

    def test_the_control_the_retry_bound_is_not_consumed_by_replanning(self) -> None:
        """控制组：新委派有自己的 Attempt 计数，没继承失败那条。"""
        loop, child_id = self._spawned(
            [_delegation("researcher"), _delegation("writer")]
        )
        first_execution = loop.pending_child.parent_execution_id
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)
        parent = self._rebuilt()
        parent.step()

        second_execution = parent.pending_child.parent_execution_id
        self.assertNotEqual(second_execution, first_execution)
        ex = self.kernel.repository.get(second_execution)
        assert ex is not None
        self.assertEqual(ex.current_attempt_no, 1)


# ---------------------------------------------------------------- D-11


class D11NoSuspensionOnAFinishedChildTest(RetryWorld):
    def test_spawn_handing_back_a_finished_child_is_refused(self) -> None:
        """D-11：不得挂起在一条已经有结果的子 Run 上。

        它**不可达**（D-9 已关重试），所以是响亮的断言而不是恢复路径：
        哪天有人把 FailureClass 改回可重试，这里立刻变红，
        而不是悄悄长出一个"父 Run 永远在等子 Agent"的故障。
        """
        from packages.agent_runtime.assembly import assemble_runtime_stack

        inner = InProcessChildRunSpawner(factory=self._factory, registry=self.registry)
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_delegation()]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            spawner=_TerminalSpawner(inner=inner, registry=self.registry),
            max_steps=6,
        )
        stack.loop.start("go", run_id="run_parent")

        with self.assertRaises(InvariantViolation) as cm:
            stack.loop.step()
        self.assertIn("D-11", str(cm.exception))

    def test_the_control_a_fresh_child_may_be_waited_on(self) -> None:
        """控制组方向：正常的（未终态）子 Run 照样挂起等它。

        没有这条，"spawn 一律拒绝"也能让上一条变绿。
        """
        self.assertIs(self._spawned()[0].pending_child.is_finished, False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
