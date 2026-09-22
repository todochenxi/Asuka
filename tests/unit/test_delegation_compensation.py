"""M32 / 空洞 219 + 220：委派**没做成**的时候，账本怎么说。

--------------------------------------------------------------------------
起因：S-1 有一扇没关的门

`SagaCoordinator.record()` 里有一条判据：

    非 COMPLETED 且非 EXTERNAL_UNKNOWN 的失败 → 认为没产生副作用 → 不登记

对**单次工具调用**这条判据大致成立：一次调用没做成，多半真的没改到东西。
但委派派出的是一条**完整的 Run** —— 它在进终态之前可能已经跑了任意多步：
建了工单、发了邮件、甚至派生了它自己的子 Run。
于是"失败 = 没副作用"对委派**根本不成立**。

而它到底留没留下东西，父 Run 无从得知 ——
子 Run 自己的账本挂在它自己的 `run_id` 下，这里看不到。

--------------------------------------------------------------------------
    D-12 委派以 failed / cancelled 收尾时，父 Run 必须登记一条 UNRESOLVED
         （S-1 的覆盖缺口）。记的是 UNRESOLVED 不是 PENDING：
          UNRESOLVED 不会被 `claim()` 领走（S-15：取消不自动回滚），
          但留在 `unresolved_for()` 里 —— 自动撤销与"当没发生"都是猜
    D-13 父 Run 已终态时，子 Run 的副作用不得被静默丢弃
         （R-3 让 `rebuild()` 抛异常 ⟹ 没有任何一步会去管它）

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.compensation import (
    CompensationSpec,
    CompensationStatus,
)
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.cancellation import InMemoryRunCancellationStore
from packages.agent_runtime.child_wake import ChildRunWaker, ChildWakeOutcome
from packages.agent_runtime.delegation import (
    ChildRunRegistry,
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
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.agent_runtime.recovery import InMemoryRunSnapshotStore, RunRecovery
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    ToolRegistry,
    ToolSpec,
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


def _spec() -> CompensationSpec:
    return CompensationSpec(
        tool="cancel_ticket",
        args={"ticket_id": "t-1"},
        # 刻意不给 `result_keys`：这条测试关心的是"有没有记、记成什么状态"，
        # 不关心撤销参数怎么取（`materialize` 的行为归 test_saga.py 管）。
        result_keys=(),
        description="撤销子 Agent 建的那张工单",
    )


def _delegation(target: str = "researcher", *, compensable: bool = True) -> Action:
    return Action(
        run_id="run_parent",
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": target},
        compensation=_spec() if compensable else None,
    )


def _tool_call() -> Action:
    return Action(
        run_id="run_parent",
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "destroy"},
        compensation=_spec(),
    )


def _exploding_tool_runtime() -> Any:
    def destroy(args: Any) -> Any:
        raise RuntimeError("the external system is on fire")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="destroy", version="1"), FunctionInvoker(destroy))
    from packages.agent_runtime.tool_runtime import ToolRuntime

    return ToolRuntime(registry)


class LedgerWorld(unittest.TestCase):
    """一个**真跑起来**的父子世界，且账本只有一本。

    `compensations` 是显式共享的：栈里那条 `SagaCoordinator` 与唤醒路径
    那条必须是同一个 store（A-12）。分叉之后"子 Run 的副作用"记在一处、
    "父 Run 的撤销"读另一处，两条都成立，拼起来是假的。
    """

    def setUp(self) -> None:
        self.registry = ChildRunRegistry()
        self.snapshots = InMemoryRunSnapshotStore()
        self.compensations = InMemoryCompensationStore()
        # M34 / 空洞 222：Run 级取消意图。与 snapshots / compensations 同款 ——
        # 父子**共享**同一份，否则父写在一处、子读另一处，等于没有通道。
        self.cancellations = InMemoryRunCancellationStore()
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
        self.waker = ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )
        self.decisions = ScriptedDecisionEngine([_delegation()])

    # ------------------------------------------------------------ 装配

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
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
            cancellations=self.cancellations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
        )

    def _parent(
        self,
        script: list[Action] | None = None,
        *,
        tool_runtime: Any = None,
    ) -> AgentLoop:
        self.decisions = ScriptedDecisionEngine(
            script if script is not None else [_delegation()]
        )
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=self.decisions,
            gateway=_gateway(),
            tool_runtime=tool_runtime if tool_runtime is not None else _tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            cancellations=self.cancellations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id="run_parent")
        return stack.loop

    def _spawned(self) -> tuple[AgentLoop, str]:
        loop = self._parent()
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        return loop, loop.pending_child.child_run_id

    def _rebuilt(self) -> AgentLoop:
        """交付发生在**重建出来**的那条父 Run 上（D-5），不是原地那个 loop。"""
        return self.recovery.rebuild("run_parent").loop

    # ------------------------------------------------------------ 断言助手

    def _all(self) -> list[Any]:
        """账本里属于这条父 Run 的全部记录（不管状态）。"""
        out: list[Any] = []
        for cid in list(getattr(self.compensations, "_by_id", {})):
            r = self.compensations.get(cid)
            if r is not None and r.run_id == "run_parent":
                out.append(r)
        return sorted(out, key=lambda r: r.compensation_id)


# ---------------------------------------------------------------- D-12


class D12DelegationLedgerTest(LedgerWorld):
    def test_a_failed_delegation_is_recorded(self) -> None:
        """D-12 的主断言：委派失败了，账本照样要有一条。

        修之前这里 `len == 0` —— 一次失败的委派在账本上**像从未发生过**，
        而子 Run 可能已经建了工单。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "failed", {"summary": "budget out"})
        self.waker.wake(child_id)

        records = self._all()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].status, CompensationStatus.UNRESOLVED)
        self.assertIn("D-12", records[0].reason)
        self.assertIn("failed", records[0].reason)

    def test_a_cancelled_delegation_is_recorded(self) -> None:
        """D-12 + S-15：取消不是失败，但同样"留在外部世界的东西没人管"。"""
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "cancelled", {})
        self.waker.wake(child_id)

        records = self._all()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].status, CompensationStatus.UNRESOLVED)
        self.assertIn("cancelled", records[0].reason)
        self.assertIn("S-15", records[0].reason)

    def test_the_two_outcomes_are_told_apart_in_the_reason(self) -> None:
        """理由必须能区分 failed 与 cancelled —— 排障方向不一样（D-10 同款）。"""
        # 脚本里放两次委派：失败一次、取消一次，各自一条记录（S-2 不冲突）
        loop = self._parent([_delegation("researcher"), _delegation("writer")])
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        a = loop.pending_child.child_run_id  # type: ignore[union-attr]
        self.registry.mark_finished(a, "failed", {})
        self.waker.wake(a)
        failed_reason = self._all()[0].reason

        parent = self._rebuilt()
        self.assertIs(parent.step(), StepOutcome.WAITING_CHILD)
        assert parent.pending_child is not None
        b = parent.pending_child.child_run_id
        self.registry.mark_finished(b, "cancelled", {})
        self.waker.wake(b)

        cancelled_reason = [r for r in self._all() if r.reason != failed_reason]
        self.assertEqual(len(cancelled_reason), 1)
        self.assertNotEqual(failed_reason, cancelled_reason[0].reason)

    def test_the_control_a_successful_delegation_is_still_pending(self) -> None:
        """控制组：成功路径没被 D-12 改成 UNRESOLVED。

        没有这条，"一律记 UNRESOLVED"也能让上面两条变绿 ——
        而那会把"副作用确实发生了、等撤销"说成"不知道有没有发生"。

        ------------------------------------------------------------------
        D-27 之后这一行**不再停在 PENDING**，而这恰好是更强的证据

        交回结果的人现在会把父 Run 推到 COMPLETED（空洞 217），
        于是 S-16 把账本结案成 `NOT_NEEDED`。而 `NOT_NEEDED`
        在迁移表里**只能从 PENDING 来**（S-14）：UNRESOLVED 那条路
        通不到这里。所以断言 `NOT_NEEDED` 同时证明了
        "D-12 没有把它改成 UNRESOLVED" —— 改过的话它会停在那儿，
        `release()` 一眼都不会看它。
        """
        loop, child_id = self._spawned()
        child_stack = loop.spawner.stack_for(child_id)  # type: ignore[union-attr]
        child_stack.loop.run()
        self.waker.wake(child_id)

        records = self._all()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].status, CompensationStatus.NOT_NEEDED)
        # args 必须真的取到了（撤销参数没丢）
        self.assertEqual(dict(records[0].args).get("ticket_id"), "t-1")

    def test_the_control_a_plain_failed_action_is_still_not_recorded(self) -> None:
        """控制组：D-12 只改**委派**，没推翻"普通动作失败 = 没副作用"这条判据。

        走的是真路径：一个会炸的工具调用，带 compensation，失败后账本为空。
        """
        loop = self._parent([_tool_call()], tool_runtime=_exploding_tool_runtime())
        self.assertIs(loop.step(), StepOutcome.FAILED)
        self.assertEqual(self._all(), [], "普通动作失败仍不登记")

    def test_the_control_a_delegation_without_compensation_records_nothing(self) -> None:
        """控制组：没声明逆操作 → 账本不凭空造一条。"""
        loop = self._parent([_delegation(compensable=False)])
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        child_id = loop.pending_child.child_run_id
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)

        self.assertEqual(self._all(), [])

    def test_s2_a_repeated_wake_does_not_add_a_second_record(self) -> None:
        """S-2：一条 Execution 最多一条。重复投递不得把账本记成两笔。"""
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "failed", {})
        self.waker.wake(child_id)
        self.waker.wake(child_id)

        self.assertEqual(len(self._all()), 1)


# ---------------------------------------------------------------- D-13


class D13OrphanSideEffectTest(LedgerWorld):
    """父 Run 已终态：结果无处可交，但副作用不能跟着消失。"""

    def _make_parent_terminal(self) -> None:
        """把父 Run 的最新快照置成终态。

        AgentOS 目前**没有 Run 级取消入口**（见 §70 登记的空洞 221），
        所以这个状态只能直接造出来。它对应的是真实会发生的那件事：
        父 Run 已经被判终态，而它还在等的那条子 Run 跑完了。
        """
        snap = self.snapshots.latest("run_parent")
        assert snap is not None
        self.snapshots.save(
            replace(snap, status=AgentRunStatus.CANCELLED.value)
        )

    def test_a_terminal_parent_leaves_an_unresolved_orphan(self) -> None:
        """D-13 的主断言：结果交不回去，但账本必须知道有这么一笔。"""
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {"summary": "done"})
        self._make_parent_terminal()

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.PARENT_TERMINAL)

        records = self._all()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].status, CompensationStatus.UNRESOLVED)
        self.assertIn("D-13", records[0].reason)

    def test_the_orphan_stays_visible(self) -> None:
        """它必须留在 `unresolved_for()` 里 —— 否则看板上它还是不存在。"""
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {})
        self._make_parent_terminal()
        self.waker.wake(child_id)

        self.assertEqual(len(self.compensations.unresolved_for("run_parent")), 1)

    def test_the_orphan_names_the_task_but_not_a_fake_step(self) -> None:
        """S-1 要求说得清"哪条 Task 产生的"，这个必须精确；
        而 step 说不出来就**留空**，不填一个假的（PR-19 同款）。"""
        loop, child_id = self._spawned()
        task_id = loop.pending_child.parent_task_id
        execution_id = loop.pending_child.parent_execution_id
        self.registry.mark_finished(child_id, "completed", {})
        self._make_parent_terminal()
        self.waker.wake(child_id)

        record = self._all()[0]
        self.assertEqual(record.task_id, task_id)
        self.assertEqual(record.execution_id, execution_id)
        self.assertEqual(record.step_id, "")
        self.assertIn("step_id is empty", record.reason)

    def test_the_control_a_live_parent_records_no_orphan(self) -> None:
        """控制组方向：父 Run 还活着 → 走正常路径，**没有**孤儿记录。

        没有这条，"一律记孤儿"也能让上面三条变绿。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {})
        self.waker.wake(child_id)

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(
            [r for r in self._all() if "D-13" in r.reason],
            [],
            "父 Run 活着时不该有孤儿记录",
        )

    def test_the_control_an_orphan_without_compensation_records_nothing(self) -> None:
        """控制组：没声明逆操作的子 Run，父终态时账本不凭空造一条。"""
        loop = self._parent([_delegation(compensable=False)])
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        child_id = loop.pending_child.child_run_id
        self.registry.mark_finished(child_id, "completed", {})
        self._make_parent_terminal()

        self.waker.wake(child_id)
        self.assertEqual(self._all(), [])

    def test_a_successful_run_does_not_wipe_the_orphan(self) -> None:
        """S-16：`release()` 只结 PENDING，UNRESOLVED 必须留下。

        没有这条，一次成功的收尾会把孤儿一起"结案"，
        看板上干净了，而那笔副作用还在外部世界里没人管。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {})
        self._make_parent_terminal()
        self.waker.wake(child_id)

        SagaCoordinator(store=self.compensations).release("run_parent")
        self.assertEqual(
            len(self.compensations.unresolved_for("run_parent")),
            1,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
