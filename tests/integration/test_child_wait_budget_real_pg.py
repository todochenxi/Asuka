"""真 PostgreSQL 上的**按次派生等待上限**（M43 / 空洞 230）。

--------------------------------------------------------------------------
这一层要验的是什么

单测（`tests/unit/test_child_wait_budget.py`）跑的是内存登记处 +
`sqlite_shim`。三件事只有真库说得清：

  1. 016 那条 `CHECK (wait_until <= spawned_at + interval '6 hours')`
     由 PG 自己判定。替身那边是把 `interval` 翻成 `strftime('%Y-%m-%d
     %H:%M:%S.%f', datetime(...))` —— 而 sqlite 的 `%f` 是"秒.毫秒"
     不是微秒，于是**恰好等于上限**那一格在替身上判不准。
     边界这一格只能交给真库。
  2. 裁决发生在写库**之前**，于是库里连一行都没有。这在替身上也验得出来，
     但"没有一行"要由另一个连接用 SQL 数一遍才算数（跨进程）。
  3. 声明写进去之后，它在**另一条连接**上读出来还是那个数 ——
     "这次派生约定了多久"是一行里的事实，不是派它出去那个进程的记忆。

--------------------------------------------------------------------------
为什么"恰好等于上限"要单独验

`<=` 与 `<` 只差一个字符，而它们的差别是"一次派生能不能要满 6 小时"。
替身判不准这一格（见上），所以这里必须真跑一次。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.agent_domain.errors import InvariantViolation
from packages.agent_runtime.adapters.postgres import PostgresChildRunRegistry
from packages.agent_runtime.delegation import MAX_CHILD_WAIT_TIMEOUT

from ._pg import RealPostgresCase, real_pg
from tests.unit.test_child_run_wake import _handle


CHECK_CEILING = "child_runs_wait_deadline_ceiling"

RAW_INSERT = """
INSERT INTO child_runs (child_run_id, kind, parent_run_id, parent_execution_id,
                        parent_task_id, target, action, spawned_at, wait_until)
VALUES (%s, 'agent', 'run_1', %s, 'task_1', 'researcher',
        '{"action_type": "AGENT_DELEGATION"}'::jsonb, %s, %s)
"""


class TheCeilingIsRealTest(RealPostgresCase):
    """016 那条 CHECK 得是**数据库**在判，不是注释里写着玩。"""

    def _constraint_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'child_runs'::regclass"
        ).fetchall()
        return {r["conname"] for r in rows}

    def test_the_check_is_in_real_postgres(self) -> None:
        self.assertIn(CHECK_CEILING, self._constraint_names())

    def test_a_raw_insert_beyond_the_ceiling_is_refused(self) -> None:
        """裸 INSERT 绕过 Python —— 那一道由数据库守（D-33 的第二道）。"""
        spawned = datetime.now(timezone.utc)
        with self.assertRaises(Exception) as cm:
            self.conn.execute(
                RAW_INSERT,
                (
                    "child_far",
                    "exec_far",
                    spawned,
                    spawned + MAX_CHILD_WAIT_TIMEOUT + timedelta(minutes=1),
                ),
            )
        self.assertIn(CHECK_CEILING, str(cm.exception))

    def test_asking_for_exactly_the_ceiling_is_allowed(self) -> None:
        """`<=` 不是 `<`：一次派生可以要满平台上限。

        替身判不准这一格（sqlite 的 `%f` 不是微秒），所以它只能在这里验。
        """
        spawned = datetime.now(timezone.utc)
        self.conn.execute(
            RAW_INSERT,
            ("child_max", "exec_max", spawned, spawned + MAX_CHILD_WAIT_TIMEOUT),
        )
        row = self.conn.execute(
            "SELECT wait_until - spawned_at AS agreed FROM child_runs "
            "WHERE child_run_id = 'child_max'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["agreed"], MAX_CHILD_WAIT_TIMEOUT)


class D31TheDeclarationIsARowFactTest(RealPostgresCase):
    """声明写进库里之后，它在**别的连接**上还是那个数。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)

    def _agreed_on(self, conn: Any, child_run_id: str) -> timedelta:
        row = conn.execute(
            "SELECT wait_until - spawned_at AS agreed FROM child_runs "
            "WHERE child_run_id = %s",
            (child_run_id,),
        ).fetchone()
        assert row is not None, f"{child_run_id} 一行都没有"
        return row["agreed"]

    def test_the_declaration_is_visible_to_another_connection(self) -> None:
        self.registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(self._agreed_on(other, "child_1"), timedelta(hours=2))

    def test_without_a_declaration_the_default_is_what_lands(self) -> None:
        """控制组：没声明时落点是全局默认 —— 于是两者在库里可区分。"""
        registry = PostgresChildRunRegistry(self.conn, wait_timeout=timedelta(minutes=7))
        registry.bind(_handle("child_1"))
        self.assertEqual(self._agreed_on(self.conn, "child_1"), timedelta(minutes=7))


class D33NothingIsWrittenWhenTheArbitrationRefusesTest(RealPostgresCase):
    """裁决在写库之前 —— 越界时库里连一行都没有（跨进程数一遍）。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)

    def _rows(self, conn: Any) -> int:
        row = conn.execute("SELECT count(*) AS n FROM child_runs").fetchone()
        assert row is not None
        return int(row["n"])

    def test_a_budget_beyond_the_ceiling_leaves_no_row(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.bind(_handle("child_1", wait_timeout=timedelta(hours=7)))
        # 抛的是**点名**的错误，不是 IntegrityError（PR-19）
        self.assertIn("D-32", str(cm.exception))

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        self.assertEqual(self._rows(other), 0, "拒绝之后一行都没写进去")

    def test_the_row_that_lands_keeps_what_it_agreed_to(self) -> None:
        bound = self.registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        assert bound.wait_until is not None
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT wait_until FROM child_runs WHERE child_run_id = 'child_1'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["wait_until"], bound.wait_until)


class D34OneDeadlineTest(RealPostgresCase):
    """冻结之后 handle 上没有第二个上限（与内存登记处同一个答案）。"""

    def test_the_declaration_is_gone_after_the_freeze(self) -> None:
        registry = PostgresChildRunRegistry(self.conn)
        handle = registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        self.assertIsNone(handle.wait_timeout)
        # 而且**读回来**也没有 —— 替身与真库在这一点上必须一致
        read_back = registry.for_child("child_1")
        assert read_back is not None
        self.assertIsNone(read_back.wait_timeout)
        assert read_back.wait_until is not None
        self.assertEqual(
            read_back.wait_until - read_back.spawned_at, timedelta(hours=2)
        )
