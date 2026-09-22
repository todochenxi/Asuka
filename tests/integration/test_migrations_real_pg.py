"""9 份迁移 + PG 专有约束，在**真 PostgreSQL** 上验一遍。

单元测试里这些跑在 `sqlite_shim` 上，而 shim 的做法是"翻译三件事、其余放行"：

    `::jsonb`                 shim 直接剥掉（sqlite 里是语法错误）
    `ON CONFLICT DO NOTHING`  sqlite 恰好有同名语法 → 碰巧通过
    `CHECK (action <> '{}')`  剥掉 cast 后变成字符串比较 → 碰巧等价

**每一处都碰巧对上，但没有一处是被 PostgreSQL 认可过的。**
本文件把它们一条条钉在真 PG 上（PR-23：换掉替身，这条还会不会红）。

每条不变量配一个控制组。
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from ._pg import MIGRATIONS_DIR, RealPostgresCase, far_enough_deadline, migration_names

#: `008_child_runs.sql` 里那些"下推到 DB 的保证"对应的约束名。
UNIQUE_D1 = "child_runs_parent_execution_id_key"
CHECK_KIND = "child_runs_kind_known"
CHECK_IDS = "child_runs_ids_not_empty"
CHECK_ACTION = "child_runs_action_not_empty"


def _child_row(**over: Any) -> tuple:
    base = {
        "child_run_id": "child_1",
        "kind": "agent",
        "parent_run_id": "run_1",
        "parent_execution_id": "exec_1",
        "parent_task_id": "task_1",
        "target": "researcher",
        "action": json.dumps({"action_type": "AGENT_DELEGATION"}),
        # 015：一次派生必须有等待上限（D-18）。裸 INSERT 绕过了 bind()，
        # 于是这一格得自己给 —— 不给的话 `child_runs_wait_deadline_required`
        # 会挡住它，而那正是 D-18 本身。
        #
        # 016：它还得在**平台上限之内**（D-32）。以前这里写的是 2099 年，
        # 那是"等到世界末日"的另一种写法，而当时它是合法的。
        "wait_until": far_enough_deadline(),
    }
    base.update(over)
    return (
        base["child_run_id"],
        base["kind"],
        base["parent_run_id"],
        base["parent_execution_id"],
        base["parent_task_id"],
        base["target"],
        base["action"],
        base["wait_until"],
    )


INSERT_CHILD = """INSERT INTO child_runs (
    child_run_id, kind, parent_run_id, parent_execution_id, parent_task_id,
    target, action, wait_until
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""


class MigrationAppliesTest(RealPostgresCase):
    def test_every_migration_applies_to_real_postgres(self) -> None:
        """全部迁移的**原文**在真 PG 上执行成功。

        这一条看着朴素，但它是本层存在的理由：此前 9 份 SQL
        只在 sqlite 上被 `executescript` 过一次，而 sqlite 会把
        `::jsonb` 直接删掉。真 PG 上跑不通的话，部署会在第一步就炸。
        """
        names = migration_names()
        self.assertTrue(names, "no migration files found")
        tables = {
            r["table_name"]
            for r in self.conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            ).fetchall()
        }
        for expected in (
            "executions",
            "attempts",
            "tasks",
            "kernel_checkpoints",
            "run_checkpoints",
            "outbox_events",
            "processed_events",
            "approvals",
            "run_snapshots",
            "compensations",
            "outbox_delivery",
            "idempotency_keys",
            "child_runs",
        ):
            self.assertIn(expected, tables, f"{expected} was not created")

    def test_the_control_a_half_migrated_database_is_visibly_half(self) -> None:
        """控制组：009 真的是必要的 —— 只跑到 004 时那一列不存在。

        没有这条，"全部迁移跑通"也可能只是"009 其实什么都没加"。
        """
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        for name in ("004_run_snapshots.sql",):
            self.conn.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        cols = {
            r["column_name"]
            for r in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='run_snapshots'"
            ).fetchall()
        }
        self.assertIn("pending_approval_id", cols)
        self.assertNotIn("pending_child_id", cols)


class D1PhysicalGuaranteeTest(RealPostgresCase):
    """D-6：`UNIQUE(parent_execution_id)` 在真 PG 上真的拦得住。"""

    def test_a_second_child_for_the_same_execution_is_rejected(self) -> None:
        self.conn.execute(INSERT_CHILD, _child_row())
        with self.assertRaises(Exception) as cm:
            self.conn.execute(
                INSERT_CHILD, _child_row(child_run_id="child_2")
            )
        self.assertIn("child_runs_parent_execution_id_key", str(cm.exception))

    def test_the_control_different_executions_each_get_one(self) -> None:
        """控制组：不是"只肯存一条" —— 不同的父 Execution 各派生一条。"""
        self.conn.execute(INSERT_CHILD, _child_row())
        self.conn.execute(
            INSERT_CHILD, _child_row(child_run_id="child_2", parent_execution_id="exec_2")
        )
        n = self.conn.execute("SELECT count(*) AS n FROM child_runs").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_on_conflict_do_nothing_loses_with_rowcount_zero(self) -> None:
        """D-6 的认领判据：`ON CONFLICT DO NOTHING` + rowcount。

        这是 `PostgresChildRunRegistry.bind()` 判断"我赢没赢"的唯一依据，
        而此前它只在 shim 自己实现的 rowcount 上被验过。
        """
        self.conn.execute(INSERT_CHILD, _child_row())
        cur = self.conn.execute(
            INSERT_CHILD + " ON CONFLICT (parent_execution_id) DO NOTHING",
            _child_row(child_run_id="child_loser"),
        )
        self.assertEqual(cur.rowcount, 0, "输的那一方必须看得见自己输了")
        winner = self.conn.execute(
            "SELECT child_run_id FROM child_runs WHERE parent_execution_id='exec_1'"
        ).fetchone()
        self.assertEqual(winner["child_run_id"], "child_1")

    def test_the_control_a_winning_insert_reports_rowcount_one(self) -> None:
        """控制组：赢的那一方 rowcount = 1 —— 上一条不是因为永远返回 0。"""
        cur = self.conn.execute(
            INSERT_CHILD + " ON CONFLICT (parent_execution_id) DO NOTHING",
            _child_row(),
        )
        self.assertEqual(cur.rowcount, 1)


class CheckConstraintTest(RealPostgresCase):
    """`CHECK` 在真 PG 上生效（sqlite 对 CHECK 的支持是另一套实现）。"""

    def _rejects(self, label: str, **over: Any) -> None:
        with self.assertRaises(Exception) as cm:
            self.conn.execute(INSERT_CHILD, _child_row(**over))
        self.assertTrue(str(cm.exception), f"{label} 被拦下但没给原因")

    def test_an_unknown_kind_is_rejected(self) -> None:
        self._rejects("kind", child_run_id="c3", kind="nonsense", parent_execution_id="e3")

    def test_an_empty_action_is_rejected(self) -> None:
        """`CHECK (action <> '{}'::jsonb)` —— 空 JSONB 在 sqlite 上只是字符串比较。"""
        self._rejects("action", child_run_id="c4", action="{}", parent_execution_id="e4")

    def test_empty_ids_are_rejected(self) -> None:
        self._rejects("ids", child_run_id="c5", parent_task_id="", parent_execution_id="e5")

    def test_the_control_a_complete_row_is_accepted(self) -> None:
        """控制组：合法的一行照样进得去 —— 上三条不是因为表写不进去。"""
        self.conn.execute(INSERT_CHILD, _child_row())
        n = self.conn.execute("SELECT count(*) AS n FROM child_runs").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_the_jsonb_column_comes_back_as_structured_data(self) -> None:
        """`::jsonb` 不只是"能存"：读回来必须是**结构**，不是字符串。

        `PostgresChildRunRegistry._row_to_child_run()` 直接把这一列喂给
        `action_from_dict()`。真 PG 返回 dict、shim 返回 dict ——
        两边一致才有意义，这条把"一致"钉住。
        """
        self.conn.execute(
            INSERT_CHILD,
            _child_row(action=json.dumps({"action_type": "AGENT_DELEGATION", "n": 1})),
        )
        row = self.conn.execute("SELECT action FROM child_runs").fetchone()
        self.assertIsInstance(row["action"], dict, "jsonb 必须被解析成 dict")
        self.assertEqual(row["action"]["n"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
