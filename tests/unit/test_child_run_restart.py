"""M26：让"等子 Run"这条挂起路径**活过重启**。

--------------------------------------------------------------------------
先说这一轮是怎么开始的

本来的任务是"补 `008_child_runs.sql` + `PostgresChildRunRegistry`，让 D-1 活过重启"。
动手前先写了个探针去量这个洞到底有多大，结果它比预想的严重得多 ——
M25 那条路径上有四个洞，而且**前三个让第四个变得根本修不好**：

    A  `step()` / `run()` 不认识 `WAITING_CHILD`
       探针实测：派生之后下一次 `step()` 完全无视 `pending_child` 继续推进，
       一路走到 FINISH，把父 Run 判成 **COMPLETED** ——
       子 Run 还在跑，父 Run 已经宣布成功，委派的结果永远不会回到 State。

    B  快照里的 `status` 落后一步
       快照在 `_sync_after_execution()` **之前**拍，于是记的是 `created`，
       而 Run 实际已经是 SUSPENDED。快照是给人看的，说谎的快照是假证据。

    C  R-1 只认审批
       `RunSnapshot.__post_init__` 写着 `is_gated → pending_approval_id`。
       M25 引入 `CHILD_AGENT` / `CHILD_SKILL` 之后，这句话开始**误伤**：
       为子 Run 而挂起的 Run 一旦被快照就直接 InvariantViolation ——
       **它连一份快照都落不下来**，遑论恢复。

    D  registry 在内存 + `pending_child` 不在快照里
       重启后 `restore()` 接不回 `pending_child` → 再走一次派生 →
       开出**第二条**子 Run。而且第二条更藏得住：重走 `step()` 会 `submit`
       一个**新的** Task，于是派生键 `parent_execution_id` 本身都换了，
       连 `UNIQUE(parent_execution_id)` 这种物理约束也拦不住它。

所以 D-1 真正的保证**不是**"先查再派"，而是：

    D-5 派生过子 Run 之后，这一步就到此为止（step() 认识 WAITING_CHILD）
    R-6 挂起必须带着"在等谁"，且不能只认审批
    D-6 登记处必须持久 —— 这是**兜底**，挡的是两个进程同时派生

层次不能颠倒：把 D-6 当主保证，等于用唯一键去拦一个键会变的重派。

--------------------------------------------------------------------------
每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.business.compensation import (
    CompensationSpec,
)
from packages.agent_domain.business.run import AgentRunStatus
from packages.agent_domain.business.snapshot import RunSnapshot
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import ExecutorType
from packages.agent_domain.execution.execution import (
    ExecutionStatus,
    SuspensionReason,
)
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunKind,
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
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.saga import InMemoryCompensationStore
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
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
)

from .sqlite_shim import connect, load_schema_sql
from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

#: 一个"真重启"需要的全部表：内核（001）+ 快照（004 + 009 + 018）+ 派生（008）
RESTART_SCHEMA = (
    "001_kernel.sql",
    "004_run_snapshots.sql",
    "009_snapshot_pending_child.sql",
    "018_snapshot_port_progress.sql",
    "008_child_runs.sql",
    "010_child_run_result.sql",
    "012_child_run_cancel_request.sql",
    "015_child_wait_deadline.sql",
)


def _delegation(run_id: str = "run_1", **payload: Any) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher", "instruction": "go find out", **payload},
    )


def _delegation_with_undo(run_id: str = "run_1") -> Action:
    """带逆操作声明的委派 —— S-13 允许（委派会在外部世界留下子 Run）。"""
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher", "instruction": "go find out"},
        compensation=CompensationSpec(
            tool="cancel_delegation",
            args={"target": "researcher"},
            description="撤销这次委派（子 Run 可能已经建了工单）",
        ),
    )


def _executors() -> dict[str, Any]:
    return {
        ExecutorType.NATIVE.value: TaskTypeRouter(
            {
                TaskType.TOOL_CALL: ToolCallExecutor(_tool_runtime()),
                TaskType.SKILL: SkillExecutor(),
                TaskType.HUMAN_APPROVAL: SkillExecutor(),
            },
            executor_type=ExecutorType.NATIVE.value,
        ),
        ExecutorType.HTTP.value: TaskTypeRouter(
            {TaskType.LLM_CALL: LLMCallExecutor(_gateway())},
            executor_type=ExecutorType.HTTP.value,
        ),
        ExecutorType.AGENT_RUNTIME.value: TaskTypeRouter(
            {TaskType.AGENT_DELEGATION: AgentDelegationExecutor()},
            executor_type=ExecutorType.AGENT_RUNTIME.value,
        ),
    }


# ---------------------------------------------------------------- 内存基底


class MemoryBase(unittest.TestCase):
    """内存版：用来测 Loop / 快照这一层的行为（不测存储）。"""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=self.clock,
        )
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors=_executors(),
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )

    def _spawner(self) -> tuple[InProcessChildRunSpawner, list[str]]:
        seen: list[str] = []

        def factory(agent_id: str, approvals: Any = None) -> Any:
            seen.append(agent_id)
            return assemble_runtime_stack(
                agent_id=agent_id,
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=ScriptedDecisionEngine([]),
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=ExecutionKernel(
                    repository=InMemoryExecutionRepository(),
                    attempts=InMemoryAttemptRepository(),
                    outbox=InMemoryOutbox(),
                    clock=ManualClock(),
                ),
            )

        spawner = InProcessChildRunSpawner(factory=factory)
        return spawner, seen

    def _loop(
        self,
        action: Action,
        *,
        spawner: Any = None,
        compensations: Any = None,
    ) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([action]),
            config=AgentLoopConfig(max_steps=6),
            spawner=spawner,
            compensations=compensations or InMemoryCompensationStore(),
        )

    def _suspended(self) -> list[Any]:
        return [
            e
            for e in self.kernel.repository.all()
            if e.status is ExecutionStatus.SUSPENDED
        ]


# ---------------------------------------------------------------- D-5


class D5ParentWaitsForItsChildTest(MemoryBase):
    def test_d5_the_parent_does_not_declare_success_while_its_child_runs(self) -> None:
        """D-5：派生之后，父 Run 必须**停在那儿**。

        M25 漏了这一句。后果是父 Run 在自己的子 Run 还没跑完时就宣布
        COMPLETED —— 委派的结果永远不会回到 State，而且没有任何报错。
        """
        spawner, seen = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")

        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        # 关键：再走一步**不许**继续推进
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.SUSPENDED)
        self.assertEqual(seen, ["researcher"])

    def test_d5_run_stops_at_waiting_child_instead_of_running_on(self) -> None:
        """D-5 的另一半：`run()` 的退出集合里必须有 `WAITING_CHILD`。

        M25 只改了 `step()`，没改 `run()` —— 于是 `run()` 会一路走到 FINISH。
        """
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        loop.run()
        self.assertEqual(loop.agent_run.status, AgentRunStatus.SUSPENDED)
        self.assertEqual(len(self._suspended()), 1)

    def test_the_control_once_the_child_completes_the_parent_moves_on(self) -> None:
        """控制组：不是"永远停住" —— 子 Run 收口之后父 Run 继续往前走。

        上两条不是因为 `step()` 被写成了无条件返回 WAITING_CHILD。
        """
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)

        child_id = loop.pending_child.child_run_id
        loop.child_completed(child_id, {"answer": "42"})
        self.assertIs(loop.step(), StepOutcome.FINISHED)

    def test_d4_the_control_the_suspension_reason_is_still_the_child(self) -> None:
        """控制组：D-5 没有把挂起原因改坏（D-4 仍然成立）。"""
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        loop.step()
        execution = self._suspended()[0]
        self.assertIs(execution.suspension.reason, SuspensionReason.CHILD_AGENT)


# ---------------------------------------------------------------- R-6


class R6SnapshotSaysWhoItWaitsForTest(MemoryBase):
    def test_r6_the_snapshot_names_the_child_it_is_waiting_for(self) -> None:
        """R-6：挂起必须带着"在等谁" —— 子 Run 与审批同等。"""
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        loop.step()

        snapshot = loop.snapshots.latest(loop.agent_run.run_id)
        assert snapshot is not None
        self.assertEqual(snapshot.pending_child_id, loop.pending_child.child_run_id)
        self.assertEqual(snapshot.waiting_for, loop.pending_child.child_run_id)

    def test_r6_the_snapshot_status_is_the_true_status(self) -> None:
        """R-6 的另一半：快照的 `status` 不许落后一步。

        M26 之前快照在 `_sync_after_execution()` **之前**拍，
        于是记的是 `created`，而 Run 实际已经 SUSPENDED ——
        一份 status 说谎的快照比没有快照更糟（PR-23 说的那种假证据）。
        """
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        loop.step()

        self.assertEqual(loop.agent_run.status, AgentRunStatus.SUSPENDED)
        snapshot = loop.snapshots.latest(loop.agent_run.run_id)
        assert snapshot is not None
        self.assertEqual(snapshot.status, AgentRunStatus.SUSPENDED.value)

    def test_r6_a_suspended_run_can_be_snapshotted_again(self) -> None:
        """R-6 的落点：为子 Run 而挂起的 Run **造得出**快照。

        M26 之前这条直接 `InvariantViolation: a SUSPENDED snapshot must carry
        pending_approval_id` —— R-1 把"等谁"写死成了"等人"，
        于是第二种挂起原因被它自己的断言挡在门外。
        """
        spawner, _ = self._spawner()
        loop = self._loop(_delegation(), spawner=spawner)
        loop.start("go")
        loop.step()
        # 不抛异常就是这条的全部意义
        self.assertIsNotNone(loop.capture(reason="still waiting"))

    def test_the_control_a_suspended_snapshot_with_nobody_to_wait_for_is_refused(
        self,
    ) -> None:
        """控制组：R-1 的**意图**没有放宽 —— 挂着却说不出在等谁，照样拒绝。

        上一条不是因为把这条断言删掉了。
        """
        with self.assertRaises(InvariantViolation) as cm:
            RunSnapshot(
                run_id="run_1",
                state={"run_id": "run_1"},
                status=AgentRunStatus.SUSPENDED.value,
            )
        self.assertIn("what it is waiting for", str(cm.exception))

    def test_r6_the_control_an_approval_suspension_still_names_the_approval(
        self,
    ) -> None:
        """控制组：审批那条路没被改坏 —— 它依然可以只带 `pending_approval_id`。"""
        snapshot = RunSnapshot(
            run_id="run_1",
            state={"run_id": "run_1"},
            status=AgentRunStatus.SUSPENDED.value,
            pending_approval_id="apr_1",
        )
        self.assertEqual(snapshot.waiting_for, "apr_1")
        self.assertIsNone(snapshot.pending_child_id)


# ---------------------------------------------------------------- D-6（PG）


class D6RegistryIsDurableTest(unittest.TestCase):
    """D-1 的物理保证：`UNIQUE(parent_execution_id)` + 原子绑定。"""

    def setUp(self) -> None:
        self.conn = connect(
            schema_sql=load_schema_sql(
                "008_child_runs.sql",
                "010_child_run_result.sql",
                "012_child_run_cancel_request.sql",
                "015_child_wait_deadline.sql",
            )
        )
        self.addCleanup(self.conn.close)
        self.registry = PostgresChildRunRegistry(self.conn)

    def _handle(
        self,
        child_run_id: str = "child_1",
        parent_execution_id: str = "exec_1",
        **kwargs: Any,
    ) -> ChildRunHandle:
        kwargs.setdefault(
            "action",
            Action(run_id="run_1", action_type=ActionType.AGENT_DELEGATION),
        )
        kwargs.setdefault("parent_task_id", "task_1")
        return ChildRunHandle(
            child_run_id=child_run_id,
            kind=ChildRunKind.AGENT,
            parent_run_id="run_1",
            parent_execution_id=parent_execution_id,
            target="researcher",
            **kwargs,
        )

    def test_d1_a_second_bind_returns_the_first(self) -> None:
        first = self.registry.bind(self._handle("child_1"))
        second = self.registry.bind(self._handle("child_2"))
        self.assertEqual(second.child_run_id, "child_1")
        self.assertEqual(first.child_run_id, "child_1")

    def test_d1_a_fresh_registry_over_the_same_database_sees_the_first(self) -> None:
        """D-6 的核心：**换一个进程**也拿回同一条。

        内存版在这一条上是红的 —— 那正是 M25 的洞。
        """
        self.registry.bind(self._handle("child_1"))
        other_process = PostgresChildRunRegistry(self.conn)
        self.assertEqual(
            other_process.for_execution("exec_1").child_run_id, "child_1"
        )

    def test_d1_the_control_different_executions_each_get_one(self) -> None:
        """控制组：不是"只肯存一条" —— 不同的父 Execution 各派生一条。"""
        self.registry.bind(self._handle("child_1", "exec_1"))
        self.registry.bind(self._handle("child_2", "exec_2"))
        self.assertEqual(len(self.registry.children_of("run_1")), 2)

    def test_d1_the_derivation_record_keeps_the_action_and_its_undo(self) -> None:
        """S-1：落库的 Action 必须带着 `compensation`。

        M26 之前 `CompensationSpec.to_dict` 因为缩进错误**根本不存在**
        （它写在模块级 `_dig` 的体内、`return` 之后），
        于是 S-8 的撤销声明从来没有序列化通道。
        """
        handle = self._handle(action=_delegation_with_undo())
        self.registry.bind(handle)
        revived = self.registry.for_execution("exec_1")
        assert revived is not None
        self.assertIsNotNone(revived.action)
        assert revived.action is not None
        self.assertIsNotNone(revived.action.compensation)
        self.assertEqual(revived.action.compensation.tool, "cancel_delegation")
        self.assertEqual(revived.parent_task_id, "task_1")

    def test_bind_refuses_a_handle_without_an_action(self) -> None:
        """没有 Action 的派生 = 一条说不清为什么要撤销的记录，直接拒绝。"""
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.bind(
                ChildRunHandle(
                    child_run_id="child_x",
                    kind=ChildRunKind.AGENT,
                    parent_run_id="run_1",
                    parent_execution_id="exec_9",
                    target="researcher",
                    parent_task_id="task_9",
                    action=None,
                )
            )
        self.assertIn("compensation declaration", str(cm.exception))

    def test_the_control_the_memory_registry_is_not_durable(self) -> None:
        """控制组：内存版**确实**活不过重启 —— 上几条不是因为存储层无差别。

        这条存在的意义是把"内存版不能用"变成一句有测试撑着的话，
        而不是一句注释。
        """
        memory = ChildRunRegistry()
        memory.bind(self._handle("child_1"))
        self.assertIsNone(ChildRunRegistry().for_execution("exec_1"))


# ---------------------------------------------------------------- 真重启


class RestartDoesNotSpawnASecondChildTest(unittest.TestCase):
    """端到端：父 Run 派生 → 落快照 → **换一个进程** → 恢复 → 不再派生。

    这里的内核、快照、派生登记全部落 PG，于是"重启"是真的：
    新 loop 拿到的是全新对象，唯一共享的东西是那一个数据库。
    """

    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql(*RESTART_SCHEMA))
        self.addCleanup(self.conn.close)
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = InMemoryCompensationStore()
        self.seen: list[str] = []

    def _kernel(self) -> ExecutionKernel:
        return ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            clock=ManualClock(),
        )

    def _spawner(self) -> InProcessChildRunSpawner:
        seen = self.seen

        def factory(agent_id: str, approvals: Any = None) -> Any:
            seen.append(agent_id)
            return assemble_runtime_stack(
                agent_id=agent_id,
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=ScriptedDecisionEngine([]),
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=ExecutionKernel(
                    repository=InMemoryExecutionRepository(),
                    attempts=InMemoryAttemptRepository(),
                    outbox=InMemoryOutbox(),
                    clock=ManualClock(),
                ),
            )

        return InProcessChildRunSpawner(factory=factory, registry=self.registry)

    def _loop(self, action: Action, *, kernel: ExecutionKernel) -> AgentLoop:
        return AgentLoop(
            kernel=kernel,
            worker=Worker(
                kernel=kernel,
                scheduler=Scheduler(kernel),
                executors=_executors(),
                config=WorkerConfig(
                    worker_id="w1",
                    lease_ttl=timedelta(seconds=30),
                    heartbeat_interval=timedelta(seconds=10),
                ),
            ),
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            # 每个 loop 一份：恢复之后它**依然**会吐出那条委派，
            # 于是"会不会再派生一次"取决于 D-5 / R-6，不取决于脚本。
            decision_engine=ScriptedDecisionEngine([action]),
            config=AgentLoopConfig(max_steps=6),
            spawner=self._spawner(),
            snapshots=self.snapshots,
            compensations=self.compensations,
        )

    def _snapshot_of(self, run_id: str) -> RunSnapshot:
        snapshot = self.snapshots.latest(run_id)
        assert snapshot is not None
        return snapshot

    def _suspended_for_child(self, run_id: str = "run_parent") -> RunSnapshot:
        """一条**真的**为子 Run 而挂起的快照。

        为什么要真的跑一遍而不是手搓：手搓的 `state` 是空的，
        `restore()` 会先在 `I-1`（Goal 必填）上炸掉，根本走不到本轮要验的那一步 ——
        那种红是脚手架的红，不是不变量的红（PR-23：换掉测试替身它还红吗）。
        """
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id=run_id)
        loop.step()
        assert loop.pending_child is not None
        return self._snapshot_of(run_id)

    # -------------------------------------------------------------- 终局
    def test_d1_a_restart_does_not_spawn_a_second_child(self) -> None:
        """D-1 的终局：重启之后**不再派生**，工厂只被调用过一次。

        M25 在这里是红的，而且红得比"多一条记录"更难看：
        重走 `step()` 会 `submit` 一个新 Task，派生键本身就换了。
        """
        kernel = self._kernel()
        loop = self._loop(_delegation(), kernel=kernel)
        loop.start("go", run_id="run_parent")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        first_child = loop.pending_child.child_run_id
        snapshot = self._snapshot_of("run_parent")

        # ---- 重启：全新内核、全新 loop、全新 spawner，只共享那一个数据库 ----
        fresh = self._kernel()
        restored = self._loop(_delegation(), kernel=fresh)
        restored.restore(snapshot)

        self.assertIsNotNone(restored.pending_child)
        assert restored.pending_child is not None
        self.assertEqual(restored.pending_child.child_run_id, first_child)
        self.assertIs(restored.step(), StepOutcome.WAITING_CHILD)
        self.assertEqual(self.seen, ["researcher"])

    def test_r6_the_restored_parent_knows_who_it_is_waiting_for(self) -> None:
        """R-6：恢复出来的父 Run 必须**接得上**它的子 Run。

        接不上的话，子 Run 跑完了、事件来了，
        `_must_pending_child` 却说"你没有在等" —— 谁也叫不醒它。
        """
        kernel = self._kernel()
        loop = self._loop(_delegation(), kernel=kernel)
        loop.start("go", run_id="run_parent")
        loop.step()
        snapshot = self._snapshot_of("run_parent")
        self.assertEqual(snapshot.pending_child_id, loop.pending_child.child_run_id)

        fresh = self._kernel()
        restored = self._loop(_delegation(), kernel=fresh)
        restored.restore(snapshot)
        assert restored.pending_child is not None
        # 不抛 D-3 就是"接上了"
        self.assertEqual(
            restored.pending_child.child_run_id, snapshot.pending_child_id
        )

    def test_s1_the_restored_parent_still_records_the_undo(self) -> None:
        """S-1：恢复之后等到子 Run 结果时，**照样**登记得了撤销。

        这是"派生记录要带 Action"这条要求的落点：没有 Action 就没有
        `compensation`，`SagaCoordinator` 会静默跳过，账本缺一条 ——
        缺的正是子 Run 留在外部世界的副作用。
        """
        action = _delegation_with_undo()
        kernel = self._kernel()
        loop = self._loop(action, kernel=kernel)
        loop.start("go", run_id="run_parent")
        loop.step()
        child_id = loop.pending_child.child_run_id
        snapshot = self._snapshot_of("run_parent")

        fresh = self._kernel()
        restored = self._loop(action, kernel=fresh)
        restored.restore(snapshot)
        assert restored.pending_child is not None
        # 派生键要在 `child_completed()` **之前**取：那一步会清空 pending_child。
        parent_execution_id = restored.pending_child.parent_execution_id
        restored.child_completed(child_id, {"answer": "42"})

        # 走到这里 pending_child 已经清空了，这正是"接上了"的证明。
        self.assertIsNone(restored.pending_child)
        record = self.compensations.get_by_execution(parent_execution_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.tool, "cancel_delegation")
        self.assertTrue(record.task_id)

    def test_the_control_a_restored_run_without_a_child_has_nothing_pending(
        self,
    ) -> None:
        """控制组：快照里没有子 Run 时，恢复出来也确实没有 ——

        上一条不是因为 `restore()` 无条件塞了一个 `pending_child`。
        """
        kernel = self._kernel()
        loop = self._loop(_delegation(), kernel=kernel)
        loop.start("go", run_id="run_plain")
        # 一条"没在等任何人"的快照：拍下来、存进去，但没派生过。
        self.snapshots.save(loop.capture(reason="control"))
        snapshot = self._snapshot_of("run_plain")
        self.assertIsNone(snapshot.pending_child_id)

        fresh = self._kernel()
        restored = self._loop(_delegation(), kernel=fresh)
        restored.restore(snapshot)
        self.assertIsNone(restored.pending_child)

    def test_a_snapshot_pointing_at_a_child_that_is_gone_is_refused(self) -> None:
        """快照指向一条不存在的派生记录 —— 拒绝恢复，而不是"当作没在等"。

        静默当没在等会让它**再派生一次**（D-1），
        而那次派生用的是新的 execution_id，谁也连不上前一条。
        """
        snapshot = replace(
            self._suspended_for_child("run_ghost"),
            pending_child_id="child_that_never_was",
        )
        restored = self._loop(_delegation(), kernel=self._kernel())
        with self.assertRaises(InvariantViolation) as cm:
            restored.restore(snapshot)
        self.assertIn("not registered", str(cm.exception))

    def test_a_loop_without_a_spawner_cannot_restore_a_child_wait(self) -> None:
        """没有派生器的 loop 恢复不出"在等子 Run" —— 早说，别静默。"""
        kernel = self._kernel()
        loop = AgentLoop(
            kernel=kernel,
            worker=Worker(
                kernel=kernel,
                scheduler=Scheduler(kernel),
                executors=_executors(),
                config=WorkerConfig(
                    worker_id="w1",
                    lease_ttl=timedelta(seconds=30),
                    heartbeat_interval=timedelta(seconds=10),
                ),
            ),
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_delegation()]),
            config=AgentLoopConfig(max_steps=6),
            spawner=None,
            snapshots=self.snapshots,
            compensations=self.compensations,
        )
        snapshot = self._suspended_for_child("run_nospawn")
        with self.assertRaises(InvariantViolation) as cm:
            loop.restore(snapshot)
        self.assertIn("no spawner/registry", str(cm.exception))


# ---------------------------------------------------------------- 迁移卫生


class MigrationHygieneTest(unittest.TestCase):
    def test_every_migration_is_exercised_by_some_test(self) -> None:
        """每一份迁移都必须被至少一条测试真的跑过。

        一份没人跑过的迁移，等于一份**可能根本执行不了**的迁移：
        009 就是这么被发现的 —— 加上它之前，快照表的列数对不上，
        而唯一能发现这件事的方式是有一条测试真的去建这张表。
        """
        from pathlib import Path

        migrations = sorted(
            p.name
            for p in (
                Path(__file__).resolve().parents[2] / "infrastructure" / "postgres"
            ).glob("*.sql")
        )
        self.assertTrue(migrations)
        tests_dir = Path(__file__).resolve().parent
        referenced = "\n".join(
            p.read_text(encoding="utf-8")
            for p in tests_dir.glob("*.py")
            if p.name != "sqlite_shim.py"
        )
        unexercised = [name for name in migrations if name not in referenced]
        self.assertEqual(
            unexercised,
            [],
            f"migrations never run by any test: {unexercised}",
        )
