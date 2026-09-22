"""真 PostgreSQL 上的**账本事件**（M40 / 空洞 232）。

--------------------------------------------------------------------------
这一层要验什么（单测验不了的）

单测（`tests/unit/test_compensation_events.py`）里 PG 那一份跑在
`sqlite_shim` 上，而 shim 只有"形状"：它证明不了下面三件事 ——

  1. **X-3 的"同一事务"** 是真的。事件行与账本行必须**一起**提交、
     **一起**回滚。shim 上的 sqlite 连接没有生产那种事务边界，
     于是一起回滚这件事在那里根本无从谈起。

  2. `payload` 是**真 JSONB**。下游是按 `payload->>'run_id'` 聚合的，
     而 shim 存进去的是文本，读回来是字符串 —— 那不是同一件事。

  3. **端到端那条迟到的结果**（空洞 232 登记的地方）在真库上
     真的留下了 `compensation.unresolved` 与 `compensation.amended` 两条。
     只验 store 的直接调用，等于没验到唤醒路径上那个 `SagaCoordinator`。

--------------------------------------------------------------------------
为什么"另一条连接"是硬要求

同一个连接读自己刚写的，读到的是自己的内存 —— 那条断言什么也证明不了。
所以下面每一条都从**另一条连接**读。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.execution.execution import ExecutionStatus
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

from ._pg import RealPostgresCase, dsn, real_pg
from .test_child_wait_deadline_real_pg import _compensable

RUN = "run_events"


def _outbox_rows(conn: Any) -> list[Any]:
    return conn.execute(
        "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
        "       aggregate_version, published_at"
        "  FROM outbox_events ORDER BY occurred_at, event_id"
    ).fetchall()


class CompensationEventsOnRealPostgresTest(RealPostgresCase):
    """账本的一辈子，在真库上留下的一串事件。"""

    def setUp(self) -> None:
        super().setUp()
        self.outbox = PostgresOutboxStore(self.conn)
        self.store = PostgresCompensationStore(self.conn, events=self.outbox)
        self.saga = SagaCoordinator(store=self.store)

    def _book(self, execution_id: str = "exec_1") -> Any:
        return self.saga.record(
            run_id=RUN,
            step_id="step_1",
            task_id="task_1",
            execution_id=execution_id,
            action=_compensable(RUN),
            result={"ticket_id": "t-1"},
            execution_status=ExecutionStatus.COMPLETED,
        )

    def test_the_whole_life_is_visible_to_another_connection(self) -> None:
        record = self._book()
        assert record is not None
        self.saga.compensate(RUN, executor=lambda _action: True)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        rows = _outbox_rows(other)
        self.assertEqual(
            [r["event_type"] for r in rows],
            ["compensation.recorded", "compensation.claimed", "compensation.compensated"],
        )
        for r in rows:
            self.assertEqual(r["aggregate_type"], "compensation")
            self.assertEqual(r["aggregate_id"], record.compensation_id)
            self.assertIsNone(r["published_at"], "还没投出去 —— 下游要能捞到")

    def test_the_payload_is_real_jsonb(self) -> None:
        """下游按 `payload->>'run_id'` 聚合。文本列做不到这件事。"""
        self._book("exec_1")

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT payload FROM outbox_events WHERE event_type = 'compensation.recorded'"
        ).fetchone()
        assert row is not None
        payload = row["payload"]
        self.assertIsInstance(payload, dict, "JSONB 读回来必须是 dict，不是字符串")
        self.assertEqual(payload["run_id"], RUN)
        self.assertEqual(payload["execution_id"], "exec_1")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["tool"], "cancel_ticket")

    def test_the_version_follows_the_ledger(self) -> None:
        """下游按 `aggregate_version` 排序 / 去重。"""
        self._book("exec_1")
        self.saga.compensate(RUN, executor=lambda _action: False)
        self.saga.amend_unresolved(execution_id="exec_1", reason="D-23: it completed")

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        versions = [r["aggregate_version"] for r in _outbox_rows(other)]
        self.assertEqual(versions, [1, 2, 3, 4])
        self.assertEqual(
            [r["event_type"] for r in _outbox_rows(other)][-1],
            "compensation.amended",
        )

    def test_reopening_is_not_a_new_record(self) -> None:
        """UNRESOLVED → PENDING 落地后也是 PENDING，但事件必须叫 reopened。"""
        self.saga.record_unresolved(
            run_id=RUN, step_id="step_1", task_id="task_1",
            execution_id="exec_1", action=_compensable(RUN),
            reason="S-11: we do not know",
        )
        row = self.store.get_by_execution("exec_1")
        assert row is not None
        self.saga.reopen(row.compensation_id)
        self.assertIs(
            self.store.get_by_execution("exec_1").status, CompensationStatus.PENDING
        )

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(
            [r["event_type"] for r in _outbox_rows(other)],
            ["compensation.unresolved", "compensation.reopened"],
        )

    def test_a_touch_that_changes_no_status_says_nothing(self) -> None:
        record = self._book()
        assert record is not None
        record.touch()
        self.store.save(record)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual([r["event_type"] for r in _outbox_rows(other)],
                         ["compensation.recorded"])


class X3OneTransactionTest(RealPostgresCase):
    """X-3 那半句"**同一事务**" —— 单测验不了，只有真库验得了。"""

    def _txn(self) -> Any:
        """一条**非 autocommit** 的连接：事务边界由我说了算。"""
        from ._pg import _connect

        conn = _connect(dsn(), autocommit=False)
        self.addCleanup(conn.close)
        return conn

    def test_the_row_and_the_event_commit_together(self) -> None:
        conn = self._txn()
        store = PostgresCompensationStore(conn, events=PostgresOutboxStore(conn))
        saga = SagaCoordinator(store=store)
        self.assertIsNotNone(
            saga.record(
                run_id=RUN, step_id="step_1", task_id="task_1",
                execution_id="exec_1", action=_compensable(RUN),
                result={}, execution_status=ExecutionStatus.COMPLETED,
            )
        )
        conn.commit()

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(other.execute(
            "SELECT count(*) AS n FROM compensations WHERE execution_id = 'exec_1'"
        ).fetchone()["n"], 1)
        self.assertEqual(
            [r["event_type"] for r in _outbox_rows(other)],
            ["compensation.recorded"],
        )

    def test_a_rollback_takes_the_event_with_it(self) -> None:
        """这就是"两件事实不同事务"会出的那件事的另一面。

        账本回滚了、事件留下 → 下游会去撤销一个**根本没登记过**的副作用。
        反过来（事件回滚、账本留下）则是本轮要闭合的空洞本身。
        两种都是"两件事实不同事务"，都必须是**不可能**出现的。
        """
        conn = self._txn()
        store = PostgresCompensationStore(conn, events=PostgresOutboxStore(conn))
        saga = SagaCoordinator(store=store)
        self.assertIsNotNone(
            saga.record(
                run_id=RUN, step_id="step_1", task_id="task_1",
                execution_id="exec_2", action=_compensable(RUN),
                result={}, execution_status=ExecutionStatus.COMPLETED,
            )
        )
        conn.rollback()

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(other.execute(
            "SELECT count(*) AS n FROM compensations WHERE execution_id = 'exec_2'"
        ).fetchone()["n"], 0)
        self.assertEqual(_outbox_rows(other), [])

    def test_an_outbox_on_another_connection_is_not_the_same_transaction(self) -> None:
        """对照：证明上面两条不是白测的。

        事件写在**另一条**连接上时，账本回滚了而事件**留下来** ——
        也就是"下游会去撤销一个根本没登记过的副作用"。
        组合根必须传同一个 conn（见 `apps/_bootstrap.pg_compensation_store`）。
        """
        conn = self._txn()
        separate = real_pg(fresh=False)
        self.addCleanup(separate.close)
        store = PostgresCompensationStore(conn, events=PostgresOutboxStore(separate))
        saga = SagaCoordinator(store=store)
        self.assertIsNotNone(
            saga.record(
                run_id=RUN, step_id="step_1", task_id="task_1",
                execution_id="exec_3", action=_compensable(RUN),
                result={}, execution_status=ExecutionStatus.COMPLETED,
            )
        )
        conn.rollback()

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(other.execute(
            "SELECT count(*) AS n FROM compensations WHERE execution_id = 'exec_3'"
        ).fetchone()["n"], 0)
        # 账本没了，事件还在 —— 这就是"两件事实不同事务"长什么样
        self.assertEqual(
            [r["event_type"] for r in _outbox_rows(other)],
            ["compensation.recorded"],
        )


class TheLateResultIsAnnouncedTest(RealPostgresCase):
    """空洞 232 登记的那一处：D-23 的改口，在真库上有没有留下事件。

    store 的直接调用验不到唤醒路径上那个 `SagaCoordinator` ——
    它拿的是组合根递进去的那本账，而那本账有没有连着 outbox
    是**装配**的事，不是调用方式的事。
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(
            self.conn, wait_timeout=timedelta(seconds=1)
        )
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.outbox = PostgresOutboxStore(self.conn)
        self.compensations = PostgresCompensationStore(
            self.conn, events=self.outbox
        )
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

    def _late(self, status: str = "completed") -> str:
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
        self._expirer().expire(child_id, now=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.registry.mark_finished(child_id, status, {"summary": "报告写完了"})
        return child_id

    def test_the_withdrawal_is_announced(self) -> None:
        """改口那一次也要有事件（X-15）—— 而且它排在到期记账之后。

        ⚠️ 刻意用 `failed`：M41（D-25）之后，`completed` 的迟到结果被
        **升级**（`compensation.upgraded`）而不是改口 —— 那一支在
        `test_child_late_upgrade_real_pg.py`。
        """
        child_id = self._late(status="failed")
        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.LATE)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        types = [
            r["event_type"]
            for r in _outbox_rows(other)
            if r["aggregate_type"] == "compensation"
        ]
        self.assertIn("compensation.unresolved", types, "到期记账那一次")
        self.assertIn("compensation.amended", types, "改口那一次 —— 本轮登记的空缺")
        self.assertEqual(types.index("compensation.unresolved"),
                         len(types) - 2)
        self.assertEqual(types[-1], "compensation.amended")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
