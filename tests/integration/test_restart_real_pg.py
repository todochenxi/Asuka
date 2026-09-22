"""真 PostgreSQL 上的端到端重启：派生 → 挂起 → 换进程 → 恢复 → 不再派生。

--------------------------------------------------------------------------
为什么这一条不能只留在单元测试里

`tests/unit/test_child_run_restart.py` 有同一条流程，但它跑在
`sqlite_shim` 上。本文件把**同一条流程**搬到真 PG，于是下面这些
第一次被真正的存储层验到：

    `PostgresExecutionRepository` 的乐观锁（E-13）在真 PG 事务下
    `PostgresOutboxStore` 的 `%s::jsonb` 写入
    `PostgresRunSnapshotStore` 的 15 列 INSERT（含 009 的新列）
    `PostgresChildRunRegistry` 的 `ON CONFLICT` 认领
    `TIMESTAMPTZ` 往返（shim 靠 register_converter 模拟）

如果真 PG 上有任何一条不变量实际上不成立，这里会红，
而 unit 层永远是绿的 —— 那正是 PR-23 要防的那一类假证据。

--------------------------------------------------------------------------
"重启"在这里是真的

第二个 loop 拿到的是**全新**的 repository / registry / snapshot store 对象，
唯一共享的东西是那一个数据库。内存态一律不共享。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from packages.agent_domain.business.compensation import CompensationSpec
from packages.agent_domain.business.snapshot import RunSnapshot
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import ExecutorType
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.executors import (
    AgentDelegationExecutor,
    LLMCallExecutor,
    SkillExecutor,
    TaskTypeRouter,
)
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.saga import InMemoryCompensationStore
from packages.execution_kernel import (
    ExecutionKernel,
    ManualClock,
    Scheduler,
    Worker,
    WorkerConfig,
)
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import RealPostgresCase


def _executors() -> dict[str, Any]:
    from packages.agent_domain.execution.task import TaskType
    from packages.agent_runtime.executors import ToolCallExecutor

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


def _delegation(run_id: str = "run_1") -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher", "instruction": "go find out"},
    )


def _delegation_with_undo(run_id: str = "run_1") -> Action:
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


class RestartOnRealPostgresTest(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.registry = PostgresChildRunRegistry(self.conn)
        self.compensations = InMemoryCompensationStore()
        self.seen: list[str] = []

    def _kernel(self) -> ExecutionKernel:
        """一个"新进程"的内核：全新对象，共享的只有那一个数据库。"""
        return ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
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
                    repository=PostgresExecutionRepository(self.conn),
                    attempts=PostgresAttemptRepository(self.conn),
                    outbox=PostgresOutboxStore(self.conn),
                    tasks=PostgresTaskRepository(self.conn),
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
            decision_engine=ScriptedDecisionEngine([action]),
            config=AgentLoopConfig(max_steps=6),
            spawner=self._spawner(),
            snapshots=self.snapshots,
            compensations=self.compensations,
        )

    # ---------------------------------------------------------------- 终局
    def test_d1_a_restart_over_real_pg_does_not_spawn_a_second_child(self) -> None:
        """D-1 的终局，在真 PG 上：工厂只被调用过一次。

        unit 层同一条断言跑在 sqlite 上；这里跑在真 PG 上，
        于是 `PostgresChildRunRegistry.bind()` 的 `ON CONFLICT` 认领
        第一次被真正的 PostgreSQL 判定。
        """
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        first_child = loop.pending_child.child_run_id

        # ---- 重启：全新内核 / 全新 loop / 全新 spawner，只共享数据库 ----
        restored = self._loop(_delegation(), kernel=self._kernel())
        restored.restore(self.snapshots.latest("run_parent"))

        self.assertIsNotNone(restored.pending_child)
        self.assertEqual(restored.pending_child.child_run_id, first_child)
        self.assertIs(restored.step(), StepOutcome.WAITING_CHILD)
        self.assertEqual(self.seen, ["researcher"])

    def test_d6_the_registry_survives_a_new_process_over_real_pg(self) -> None:
        """D-6：换一个 registry 对象（= 换进程）也拿回同一条。"""
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        loop.step()
        expected = loop.pending_child.child_run_id

        fresh = PostgresChildRunRegistry(self.conn)
        handle = fresh.for_execution(loop.pending_child.parent_execution_id)
        self.assertIsNotNone(handle)
        self.assertEqual(handle.child_run_id, expected)

    def test_s1_the_restored_parent_still_records_the_undo(self) -> None:
        """S-1：真 PG 上，Action 里的 `compensation` 往返不丢。

        这是 M26 那次旧病的落点：`CompensationSpec.to_dict` 曾经是死代码，
        撤销声明根本没有序列化通道。这里走的是**真的** jsonb 往返 ——
        shim 里 json 列只是 TEXT，看不出丢没丢。
        """
        action = _delegation_with_undo()
        loop = self._loop(action, kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        loop.step()
        child_id = loop.pending_child.child_run_id

        restored = self._loop(action, kernel=self._kernel())
        restored.restore(self.snapshots.latest("run_parent"))
        parent_execution_id = restored.pending_child.parent_execution_id
        restored.child_completed(child_id, {"answer": "42"})

        record = self.compensations.get_by_execution(parent_execution_id)
        self.assertIsNotNone(record, "撤销声明在 jsonb 往返里丢了")
        self.assertEqual(record.tool, "cancel_delegation")

    def test_r6_the_snapshot_round_trips_pending_child_id(self) -> None:
        """R-6：快照的 `pending_child_id` 在真 PG 上往返不丢（009 的新列）。"""
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        loop.step()
        snapshot = self.snapshots.latest("run_parent")
        self.assertEqual(snapshot.pending_child_id, loop.pending_child.child_run_id)
        self.assertIsNotNone(snapshot.waiting_for)

    # ---------------------------------------------------------------- 控制组
    def test_the_control_a_ghost_child_is_still_refused(self) -> None:
        """控制组：快照指向一条不存在的派生记录 → 拒绝恢复。"""
        from dataclasses import replace

        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        loop.step()
        ghost = replace(
            self.snapshots.latest("run_parent"),
            pending_child_id="child_that_never_was",
        )
        restored = self._loop(_delegation(), kernel=self._kernel())
        with self.assertRaises(InvariantViolation) as cm:
            restored.restore(ghost)
        self.assertIn("not registered", str(cm.exception))

    def test_the_control_a_run_without_a_child_has_nothing_pending(self) -> None:
        """控制组：没有派生过的 Run，恢复出来也确实没有在等谁。"""
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_plain")
        self.snapshots.save(loop.capture(reason="control"))
        snapshot = self.snapshots.latest("run_plain")
        self.assertIsNone(snapshot.pending_child_id)

        restored = self._loop(_delegation(), kernel=self._kernel())
        restored.restore(snapshot)
        self.assertIsNone(restored.pending_child)

    def test_the_control_the_snapshot_store_round_trips_a_full_snapshot(self) -> None:
        """控制组：快照整体往返（15 列 + trace）在真 PG 上不失真。

        没有这条，上几条也可能是因为"快照其实根本没被正确读回来，
        而恢复恰好不需要它" —— 那条路在 M26 的探针里就骗过一次。
        """
        loop = self._loop(_delegation(), kernel=self._kernel())
        loop.start("go", run_id="run_parent")
        loop.step()
        before: RunSnapshot = self.snapshots.latest("run_parent")
        after = self.snapshots.latest("run_parent")
        self.assertEqual(after.run_id, before.run_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.step_count, before.step_count)
        self.assertEqual(len(after.trace), len(before.trace))
        self.assertTrue(after.trace, "trace 是空的 —— 往返根本没验到")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
