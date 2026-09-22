"""真 PostgreSQL 上的 Run 级取消（M33 / 空洞 221）。

--------------------------------------------------------------------------
为什么这一层不能只在内存里验

取消这一条路径上，**三个**判据都是 SQL 谓词而不是 Python 判断：

1. `undelivered()`：
   `WHERE completed_at IS NOT NULL AND delivered_at IS NULL`
   —— B-9 的第三半（叫停之后必须结掉那条子 Run）全靠它。
   内存版 `ChildRunRegistry` 是用 Python 的 `is_finished` / `is_delivered`
   过滤的，**这个谓词一行都没被执行过**。

2. `child_runs_outcome_consistent`（D-6）：终态 ⟺ `completed_at` 非空。
   级联取消往里写的是 `cancelled` —— 与 `failed` / `completed` 同一个 CHECK。

3. `run_snapshots` 的最新一条（R-1）：`latest()` 按
   `created_at DESC, snapshot_id DESC` 排序。
   取消之后落的那份终态快照必须真的排在挂起那一帧**后面** ——
   否则"取消"在库里看不见，重启后恢复出来的是一条 RUNNING 的 Run。

三条全是"只有真库才执行得到"的东西。内存版什么都不校验，它永远看不见。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.recovery import RunRecovery
from packages.agent_runtime.saga import SagaCoordinator
from packages.execution_kernel import ExecutionKernel, ManualClock
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)

from tests.unit.test_delegation_compensation import _delegation

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import RealPostgresCase


class RunCancellationOnRealPostgresTest(RealPostgresCase):
    """取消在真库上：停得住、停得干净、留得下。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn)
        self.saga = SagaCoordinator(store=self.compensations)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )
        self.decisions = ScriptedDecisionEngine([_delegation()])

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
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
        )

    def _spawned(self) -> Any:
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
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        return stack

    def _row(self, sql: str, params: tuple = ()) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
        return dict(rows[0])

    def test_the_cancelled_run_leaves_a_terminal_snapshot_in_postgres(self) -> None:
        """R-1 在真库上：取消之后**最新**那一帧必须是 `cancelled`。

        `latest()` 的排序是 `created_at DESC, snapshot_id DESC`。
        排序错了，取消就只存在于内存里 —— 重启后恢复出来的是一条
        还在 RUNNING 的 Run，它会继续往前走，而且不报错。
        """
        stack = self._spawned()
        stack.loop.cancel(reason="user asked", by="alice")

        row = self._row(
            "SELECT status FROM run_snapshots WHERE run_id = %s "
            "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
            ("run_parent",),
        )
        self.assertEqual(row["status"], "cancelled")

    def test_the_cascade_lands_in_postgres(self) -> None:
        """B-9 在真库上：子 Run 被判 `cancelled`，且 `completed_at` 非空（D-6）。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.cancel(reason="user asked", by="alice")

        row = self._row(
            "SELECT status, completed_at, delivered_at FROM child_runs "
            "WHERE child_run_id = %s",
            (child_id,),
        )
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["completed_at"], "D-6：终态必须有完成时刻")
        # B-9 的第三半：结掉了。不结的话它永远待在 `undelivered()` 里，
        # 兜底扫每轮捞它一次，而父 Run 已终态 ⟹ 每轮记一条 D-13 孤儿。
        self.assertIsNotNone(row["delivered_at"])

    def test_the_sweep_finds_nothing_after_a_cancellation(self) -> None:
        """`undelivered()` 的 SQL 谓词第一次被真正执行。

        内存版是用 `is_finished` / `is_delivered` 过滤的 ——
        这条 `WHERE completed_at IS NOT NULL AND delivered_at IS NULL`
        在本条之前从未被跑过。谓词写反了内存版一样绿。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.cancel(reason="user asked", by="alice")

        # 先钉住它确实被叫停且已交付，再钉住"扫不出来"。
        # 少了前一句，"压根没级联"时这条也绿 —— 那是空过，不是通过。
        row = self._row(
            "SELECT completed_at, delivered_at FROM child_runs WHERE child_run_id = %s",
            (child_id,),
        )
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNotNone(row["delivered_at"])

        self.assertEqual(list(self.registry.undelivered()), [])

        waker = ChildRunWaker(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            saga=self.saga,

            driver=InProcessRunDriver(
                recovery=RunRecovery(
                    snapshots=self.snapshots,
                    factory=self._factory,
                    approvals=None,
                ),
            ),
        )
        self.assertEqual(waker.sweep().total, 0)

    def test_the_delegation_ledger_lands_in_postgres(self) -> None:
        """D-12 + B-9：级联取消之后，那笔"可能留下的副作用"在真库里也在。"""
        stack = self._spawned()
        stack.loop.cancel(reason="user asked", by="alice")

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, reason FROM compensations WHERE run_id = %s",
                ("run_parent",),
            )
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unresolved")
        self.assertIn("D-12", rows[0]["reason"])

    def test_the_gated_execution_is_cancelled_not_left_suspended(self) -> None:
        """Kernel 里那条 SUSPENDED 必须真的判死 —— 它等的人不会来了。"""
        stack = self._spawned()
        execution_id = stack.loop.pending_child.parent_execution_id

        stack.loop.cancel(reason="user asked", by="alice")

        row = self._row(
            "SELECT status FROM executions WHERE execution_id = %s",
            (execution_id,),
        )
        self.assertEqual(row["status"], "CANCELLED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
