"""真 PostgreSQL 上的**迟到结果**（M39 / 空洞 231）。

--------------------------------------------------------------------------
这一层要验什么

单测（`tests/unit/test_child_late_result.py`）跑的是内存账本，而内存版
"只改 UNRESOLVED"是自己 `if` 出来的。真库上那一句必须是 SQL 的

    UPDATE ... WHERE execution_id = %s AND status = 'unresolved'

—— 判据在 WHERE 里，于是"先读再写"那个两进程竞态（S-4 / A-11 同款）
根本没有立足之地。这件事只有真库验得了。

第二件事：账本那一行要能被**另一条连接**原样读出来。
同一个连接读自己刚写的，读到的是自己的内存，那条断言什么也证明不了。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker, ChildWakeOutcome
from packages.agent_runtime.child_wait import ChildRunWaitExpirer
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

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import RealPostgresCase
from .test_child_wait_deadline_real_pg import _compensable

UNKNOWN_MARKER = "WE DO NOT KNOW"


class AmendmentOnRealPostgresTest(RealPostgresCase):
    """D-23 的落点：判据在 SQL 的 WHERE 里，不在 Python 的 `if` 里。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresCompensationStore(self.conn)
        self.saga = SagaCoordinator(store=self.store)

    def _book(self, execution_id: str, reason: str = "we do not know yet") -> Any:
        return self.saga.record_unresolved(
            run_id="run_late",
            step_id="step_1",
            task_id="task_1",
            execution_id=execution_id,
            action=_compensable("run_late"),
            reason=reason,
        )

    def test_an_unresolved_row_is_amended_and_visible_to_another_connection(
        self,
    ) -> None:
        from ._pg import real_pg

        self._book("exec_unknown")

        amended = self.saga.amend_unresolved(
            execution_id="exec_unknown", reason="D-23: it ended 'completed'"
        )
        self.assertIsNotNone(amended)
        assert amended is not None
        self.assertIs(amended.status, CompensationStatus.UNRESOLVED)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT status, reason, version FROM compensations "
            "WHERE execution_id = 'exec_unknown'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "unresolved")
        self.assertIn("D-23", row["reason"])
        self.assertEqual(row["version"], 2, "补真相必须留版本号线索（E-25）")

    def test_a_row_that_has_already_been_disposed_of_is_left_alone(self) -> None:
        """已处置过的是历史。真 PG 上挡住它的是 WHERE，不是调用方。"""
        record = self._book("exec_handled")
        assert record is not None
        record.transition(CompensationStatus.PENDING)
        self.store.save(record)

        self.assertIsNone(
            self.saga.amend_unresolved(
                execution_id="exec_handled", reason="D-23: now we know"
            )
        )
        after = self.store.get_by_execution("exec_handled")
        assert after is not None
        self.assertIs(after.status, CompensationStatus.PENDING)
        self.assertNotIn("now we know", after.reason)

    def test_amending_a_row_that_does_not_exist_creates_nothing(self) -> None:
        self.assertIsNone(
            self.saga.amend_unresolved(execution_id="exec_never", reason="truth")
        )
        rows = self.conn.execute(
            "SELECT count(*) AS n FROM compensations WHERE execution_id = 'exec_never'"
        ).fetchone()
        assert rows is not None
        self.assertEqual(rows["n"], 0)


class LateResultOnRealPostgresTest(RealPostgresCase):
    """端到端：派生 → 到期 → 它**后来**跑完了 → 账本不再说"不知道"。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(
            self.conn, wait_timeout=timedelta(seconds=1)
        )
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, approvals=approvals, registry=self.registry
            ),
        )

    def _waker(self) -> ChildRunWaker:
        return ChildRunWaker(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(
                recovery=RunRecovery(
                    snapshots=self.snapshots,
                    factory=self._factory,
                    approvals=None,
                ),
            ),
        )

    def _expirer(self) -> ChildRunWaitExpirer:
        return ChildRunWaitExpirer(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(
                recovery=RunRecovery(
                    snapshots=self.snapshots,
                    factory=self._factory,
                    approvals=None,
                ),
            ),
        )

    def _late(self, status: str = "completed", result: Any = None) -> str:
        """派生 → 子 Run 的进程没了 → 到期 → 它**后来**有了结局。返回 child_run_id。"""
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_compensable("run_parent")]),
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
        child_id = stack.loop.pending_child.child_run_id
        del stack.loop.spawner._stacks[child_id]
        self._expirer().expire(child_id, now=self._future())
        self.registry.mark_finished(
            child_id, status, result if result is not None else {"summary": "报告写完了"}
        )
        return child_id

    def _future(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=5)

    def test_a_late_result_withdraws_the_unknown_in_real_postgres(self) -> None:
        """D-23 那一支：结局是 `failed` —— 副作用**发没发生仍然不知道**（S-11）。

        ⚠️ 这里刻意用 `failed`，不用 `completed`。

        `completed` 的迟到结果从 M41（空洞 233 / D-25）起**不再**走"改口"这一支：
        可信结果证明副作用确实发生了，账本要被升级成"待撤销"（PENDING），
        于是它不再断言"仍然 unresolved"。那一支在
        `test_child_late_upgrade_real_pg.py` 里。
        """
        from ._pg import real_pg

        child_id = self._late(status="failed", result={"summary": "炸了"})
        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.LATE)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        rows = other.execute(
            "SELECT status, reason FROM compensations WHERE run_id = 'run_parent'"
        ).fetchall()
        self.assertTrue(rows, "到期必须在账本上留一行")
        self.assertEqual(rows[0]["status"], "unresolved", "收回的不是那笔副作用")
        self.assertNotIn(UNKNOWN_MARKER, rows[0]["reason"])
        self.assertIn("WITHDRAWN", rows[0]["reason"])
        self.assertIn("failed", rows[0]["reason"])
        self.assertIn("S-11", rows[0]["reason"], "D-26：得说清是哪一样挡着")

    def test_the_late_row_leaves_the_undelivered_queue(self) -> None:
        """R-13 同款：不登记交付就永久占着队首。"""
        child_id = self._late()
        self.assertEqual(
            [h.child_run_id for h in self.registry.undelivered()], [child_id]
        )
        self._waker().wake(child_id)
        self.assertEqual([h.child_run_id for h in self.registry.undelivered()], [])
        self.assertEqual(self._waker().sweep().total, 0)

    def test_the_sweep_counts_it_as_late_not_delivered(self) -> None:
        child_id = self._late()
        result = self._waker().sweep()
        self.assertEqual(result.late, (child_id,))
        self.assertEqual(result.delivered, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
