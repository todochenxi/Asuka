"""真 PostgreSQL 上的**账本升级**（M41 / 空洞 233）。

--------------------------------------------------------------------------
这一层要验什么

单测（`tests/unit/test_child_late_upgrade.py`）跑的是 `sqlite_shim`，
而 shim 里的 `args` 是**文本**，`args = '{}'::jsonb` 被 `_strip_jsonb_cast`
抹掉之后退化成"字符串等于 `{}`"。真库上它是 JSONB 相等 —— 两件事。

于是三件事只有真库说得清：

  1. `args = '{}'::jsonb` 是**JSONB 判等**。升完级之后 `args->>'ticket_id'`
     能用 SQL 取出来 —— 下游（`WHERE args->>'ticket_id' = ...`）靠的就是这个。
     在 shim 上那句 SQL 根本不成立（文本列没有 `->>`），
     于是"撤销参数真的落进 JSONB 里了"这条断言在那里是**恒假**的。
  2. 两**条连接**同时补同一行：赢家拿到 rowcount 1，输家拿到 0（S-4 同款）。
     shim 是同一个文件上的单连接，"两个进程"这件事在那里无从谈起。
  3. 端到端那一条：派生 → 到期 → 迟到的 `completed` → 账本变成"待撤销"，
     并且能被**另一个进程**原样读出来。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

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

from tests.unit.test_child_late_upgrade import _compensable_from_result
from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import RealPostgresCase, real_pg

RUN = "run_parent"


class UpgradeOnRealPostgresTest(RealPostgresCase):
    """`args = '{}'::jsonb` —— 判据活在 PG 的 WHERE 里。"""

    def setUp(self) -> None:
        super().setUp()
        self.outbox = PostgresOutboxStore(self.conn)
        self.store = PostgresCompensationStore(self.conn, events=self.outbox)
        self.saga = SagaCoordinator(store=self.store)

    def _book(self, execution_id: str, reason: str = "we do not know yet") -> Any:
        return self.saga.record_unresolved(
            run_id=RUN,
            step_id="step_1",
            task_id="task_1",
            execution_id=execution_id,
            action=_compensable_from_result(RUN),
            reason=reason,
        )

    def test_the_upgrade_writes_real_jsonb(self) -> None:
        """升完级之后 `args->>'ticket_id'` 能用 SQL 取出来。

        在 `sqlite_shim` 上这句 SQL 根本不成立（那一列是文本），
        所以这条断言在那里**写不出来** —— 它不是"多跑一遍"，是只有真库能问的问题。
        """
        self._book("exec_1")
        upgraded = self.saga.upgrade_to_compensable(
            execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25: upgraded"
        )
        self.assertIsNotNone(upgraded)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT status, reason, version, args->>'ticket_id' AS ticket "
            "  FROM compensations WHERE execution_id = 'exec_1'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["ticket"], "t-9")
        self.assertEqual(row["reason"], "", "PENDING 的行不带理由（S-5 的约定）")
        self.assertEqual(row["version"], 2, "升级必须留版本号线索（E-25）")

    def test_a_row_that_never_lacked_args_is_not_touched(self) -> None:
        """`args` 非空 = 它是撤销动作**跑失败**了（S-5/S-6），
        真相到达不构成自动重试的理由。真库上挡住它的是 WHERE，不是调用方。
        """
        self._book("exec_1")
        self.conn.execute(
            "UPDATE compensations SET args = %s::jsonb WHERE execution_id = 'exec_1'",
            ('{"ticket_id": "t-1"}',),
        )

        self.assertIsNone(
            self.saga.upgrade_to_compensable(
                execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25"
            )
        )
        row = self.conn.execute(
            "SELECT status, args->>'ticket_id' AS ticket FROM compensations "
            "WHERE execution_id = 'exec_1'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["ticket"], "t-1", "旧参数不许被迟到的结果覆盖")

    def test_two_connections_race_and_only_one_wins(self) -> None:
        """S-4 / A-11 同款：两个进程同时补同一行 —— 判胜负靠 rowcount。

        "先读一下是不是 unresolved 再写"两边都会通过，然后撤销两次。
        这里两条连接各自 autocommit，所以是真的并发写；
        第二条 UPDATE 必须匹配 0 行，而不是再把账本改一遍。
        """
        self._book("exec_1")

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        other_store = PostgresCompensationStore(
            other, events=PostgresOutboxStore(other)
        )
        other_saga = SagaCoordinator(store=other_store)

        first = self.saga.upgrade_to_compensable(
            execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25: A"
        )
        second = other_saga.upgrade_to_compensable(
            execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25: B"
        )

        winners = [r for r in (first, second) if r is not None]
        self.assertEqual(len(winners), 1, "升级只许发生一次（S-2 / S-4）")

        row = other.execute(
            "SELECT status, version, args->>'ticket_id' AS ticket FROM compensations "
            "WHERE execution_id = 'exec_1'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["ticket"], "t-9")
        self.assertEqual(row["version"], 2, "版本号只被赢家加了一次")


class LateResultUpgradeOnRealPostgresTest(RealPostgresCase):
    """端到端：派生 → 到期 → 它**后来**跑完了 → 账本变成"待撤销"。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(
            self.conn, wait_timeout=timedelta(seconds=1)
        )
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.outbox = PostgresOutboxStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn, events=self.outbox)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=self.outbox,
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

    def _late(self, *, status: str, result: dict) -> str:
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_compensable_from_result(RUN)]),
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
        stack.loop.start("go", run_id=RUN)
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        del stack.loop.spawner._stacks[child_id]
        self._expirer().expire(
            child_id, now=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        self.registry.mark_finished(child_id, status, result)
        return child_id

    def _row(self, conn: Any) -> Any:
        row = conn.execute(
            "SELECT status, reason, args, version FROM compensations "
            f"WHERE run_id = '{RUN}'"
        ).fetchone()
        assert row is not None, "到期必须在账本上留一行"
        return row

    def test_a_late_completed_result_makes_the_row_compensable(self) -> None:
        """这一轮真正要闭合的那一支：账本不再说"撤销不了"。"""
        child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.assertEqual(self._row(self.conn)["status"], "unresolved")  # 到期那一刻

        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.LATE)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = self._row(other)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["args"], {"ticket_id": "t-9"})
        self.assertNotIn("WE DO NOT KNOW", row["reason"])

    def test_the_undo_args_are_queryable_as_jsonb(self) -> None:
        """下游按 `args->>'ticket_id'` 聚合 —— 只有真 JSONB 撑得住。"""
        child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        ticket = other.execute(
            f"SELECT args->>'ticket_id' AS ticket FROM compensations "
            f"WHERE run_id = '{RUN}' AND status = 'pending'"
        ).fetchone()
        assert ticket is not None
        self.assertEqual(ticket["ticket"], "t-9")

    def test_the_upgrade_is_announced_to_another_connection(self) -> None:
        """X-15：账本变了就得有事件，而且它排在到期记账之后。"""
        child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        types = [
            r["event_type"]
            for r in other.execute(
                "SELECT event_type FROM outbox_events "
                "WHERE aggregate_type = 'compensation' "
                "ORDER BY occurred_at, event_id"
            ).fetchall()
        ]
        self.assertEqual(types, ["compensation.unresolved", "compensation.upgraded"])

        payload = other.execute(
            "SELECT payload FROM outbox_events WHERE event_type = "
            "'compensation.upgraded'"
        ).fetchone()
        assert payload is not None
        self.assertIn("D-25", payload["payload"]["reason"])
        self.assertIn("WITHDRAWN", payload["payload"]["reason"])
        self.assertEqual(payload["payload"]["status"], "pending")

    def test_a_completed_result_without_the_key_stays_unresolved(self) -> None:
        """D-26：跑完了但结果里没有撤销要的那个键 —— 留在 UNRESOLVED 并点名。"""
        child_id = self._late(status="completed", result={"summary": "报告写完了"})
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = self._row(other)
        self.assertEqual(row["status"], "unresolved")
        self.assertIn("D-26", row["reason"])
        self.assertIn("ticket_id", row["reason"], "得说清缺的是哪个键")
        self.assertEqual(row["args"], {}, "参数取不到就不许带着猜的去撤销")

    def test_it_is_idempotent(self) -> None:
        """同一条迟到的结果被扫到第二次（兜底扫 / 重复投递）：账本不再动。"""
        child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        before = self._row(other)

        self._waker().wake(child_id)  # 第二次：已 delivered

        after = self._row(other)
        self.assertEqual(
            (before["status"], before["args"], before["version"]),
            (after["status"], after["args"], after["version"]),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
