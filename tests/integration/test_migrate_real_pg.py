"""M67 · 迁移器在真 PostgreSQL 上。

第一条用例是这个文件的地基：**证明裸重放是不行的**。
017 是一句 `ALTER TABLE ADD COLUMN`，跑第二遍就是 `column already exists`。
这条用例把它钉住 —— 于是"为什么要有一张账本表"不再是设计文档里的一句话，
而是一个每天被验证的事实。

其余用例覆盖生产真正会遇到的四种处境：
    首次上线（空库）   落后（有序补齐）   已最新（重跑无副作用）   历史被改（拒绝）
"""
from __future__ import annotations

import unittest

from apps.migrate import (
    MIGRATION_LOCK_KEY,
    MigrationError,
    apply_pending,
    connect,
    discover,
    ensure_bookkeeping,
    plan,
    read_applied,
    release_lock,
    with_lock,
)
from tests.integration._pg import dsn, migration_names, real_pg


class TestWhyALedgerIsNeeded(unittest.TestCase):
    """没有账本会怎样 —— 先把这个事实测出来。"""

    def test_replaying_a_migration_twice_fails(self):
        conn = real_pg()
        self.addCleanup(conn.close)

        sql = (
            "ALTER TABLE executions ADD COLUMN cancellation_reason "
            "TEXT NOT NULL DEFAULT ''"
        )
        # 017 已经在 `real_pg()` 的迁移里跑过一次了，这里就是"重放"
        with self.assertRaises(Exception) as ctx:
            conn.execute(sql)
        self.assertIn("already exists", str(ctx.exception).lower())


class TestMigrateOnRealPostgres(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = real_pg()
        self.addCleanup(self.conn.close)
        # 每次从一个**真空**的 schema 起步：这才等价于"第一次上线"
        self.conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.dsn = dsn()

    # -------------------------------------------------------------- 首次上线
    def test_applying_on_an_empty_database_creates_everything(self):
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        with_lock(conn)
        self.addCleanup(lambda: release_lock(conn))

        ensure_bookkeeping(conn)
        migrations = discover()
        p = plan(read_applied(conn), migrations)

        self.assertEqual(len(p.pending), len(migration_names()))
        applied = apply_pending(conn, p)

        self.assertEqual(len(applied), len(migration_names()))
        self.assertIn("017_execution_cancel_attribution.sql", applied)

        # 业务表真的在 —— 不是"过程没报错"而已
        for table in ("tasks", "executions", "approvals", "outbox_events", "run_snapshots"):
            row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            self.assertIsNotNone(row[0], f"{table} should exist")

    # ------------------------------------------------------------ 已最新
    def test_a_second_apply_does_nothing(self):
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        with_lock(conn)
        self.addCleanup(lambda: release_lock(conn))

        ensure_bookkeeping(conn)
        migrations = discover()
        apply_pending(conn, plan(read_applied(conn), migrations))

        # 第二次：这就是"上线脚本被跑了两遍"的真实处境
        again = plan(read_applied(conn), migrations)
        self.assertEqual(again.pending, ())
        self.assertTrue(again.up_to_date)
        self.assertEqual(apply_pending(conn, again), ())

    # ------------------------------------------------------------ 落后
    def test_only_the_tail_is_applied(self):
        """线上跑到一半再来一次 —— 已跑过的不许重放。"""
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        with_lock(conn)
        self.addCleanup(lambda: release_lock(conn))

        ensure_bookkeeping(conn)
        migrations = discover()
        head = migrations[:5]
        apply_pending(conn, plan(read_applied(conn), head))

        rest = plan(read_applied(conn), migrations)
        self.assertEqual([m.name for m in rest.pending], [m.name for m in migrations[5:]])
        apply_pending(conn, rest)

        final = plan(read_applied(conn), migrations)
        self.assertTrue(final.up_to_date)

    # ------------------------------------------------------------ 历史被改
    def test_a_modified_history_is_refused_not_replayed(self):
        """改动已上线的迁移 → 拒绝（退出码 4），且库**一下都不许动**。"""
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        with_lock(conn)
        self.addCleanup(lambda: release_lock(conn))

        ensure_bookkeeping(conn)
        migrations = discover()
        apply_pending(conn, plan(read_applied(conn), migrations))

        # 模拟"有人改了 017 这个文件"：账本上记着的指纹与磁盘上的不再一致
        conn.execute(
            "UPDATE agentos_schema_migrations SET checksum = %s WHERE name = %s",
            ("deadbeefdeadbeef", "017_execution_cancel_attribution.sql"),
        )
        p = plan(read_applied(conn), migrations)

        self.assertEqual(len(p.drifted), 1)
        self.assertEqual(p.drifted[0][0], "017_execution_cancel_attribution.sql")
        self.assertEqual(p.pending, ())  # 不许重放

        with self.assertRaises(MigrationError) as ctx:
            apply_pending(conn, p)
        self.assertEqual(ctx.exception.code, "MIGRATION_DRIFT")

    # ------------------------------------------------------------ 只读
    def test_check_on_a_virgin_database_does_not_write(self):
        """`check` 是上线前的门禁 —— 一个"看一眼"的命令不该有副作用。"""
        applied = read_applied(self.conn)
        self.assertEqual(applied, {})
        # 注意：`real_pg()` 那条连接是 dict_row，这里按列名取
        row = self.conn.execute(
            "SELECT to_regclass(%s)", ("agentos_schema_migrations",)
        ).fetchone()
        self.assertIsNone(row["to_regclass"], "check must not create the ledger table")

    # ------------------------------------------------------------ 并发
    def test_a_second_migrator_cannot_take_the_lock(self):
        """两个 Pod 同时跑 migrate：后一个排队，不是一起改。"""
        first = connect(self.dsn)
        self.addCleanup(first.close)
        with_lock(first)
        self.addCleanup(lambda: release_lock(first))

        second = connect(self.dsn)
        self.addCleanup(second.close)
        got = second.execute(
            "SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,)
        ).fetchone()[0]
        self.assertFalse(got, "the migration lock should already be held")
