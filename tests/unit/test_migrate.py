"""M67 · 迁移器的纯层。

要害只有一条：**已上线的迁移被改过（drift）时，绝不许重放它**。

17 个文件里有裸的 `ALTER TABLE ADD COLUMN`（017），重放第二遍就是
`column already exists`。所以 drift 那条必须进 `drifted` 而不是 `pending` ——
进错了，一次"改了历史文件"就会变成一次"上线失败"，
而报错说的是列已存在，排查方向从一开始就错了。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps.migrate import (
    Migration,
    MigrationError,
    checksum,
    discover,
    plan,
    report,
    unwrap_transaction,
)


class TestUnwrapTransaction(unittest.TestCase):
    """8 个文件自带 `BEGIN/COMMIT`、9 个不带 —— 两种都要落对。"""

    def test_a_wrapped_file_is_unwrapped(self):
        body, did = unwrap_transaction("BEGIN;\nALTER TABLE t ADD COLUMN c TEXT;\nCOMMIT;\n")
        self.assertTrue(did)
        self.assertNotIn("BEGIN", body)
        self.assertNotIn("COMMIT", body)
        self.assertIn("ALTER TABLE t ADD COLUMN c TEXT;", body)

    def test_leading_comments_do_not_hide_the_begin(self):
        """注释块在前 —— 17 个文件全是这个形态。"""
        sql = "-- 说明\n-- 又是说明\n\nBEGIN;\nSELECT 1;\nCOMMIT;\n"
        body, did = unwrap_transaction(sql)
        self.assertTrue(did)
        self.assertIn("SELECT 1;", body)

    def test_an_unwrapped_file_is_left_alone(self):
        sql = "ALTER TABLE executions ADD COLUMN cancellation_reason TEXT NOT NULL DEFAULT '';"
        body, did = unwrap_transaction(sql)
        self.assertFalse(did)
        self.assertEqual(body, sql)

    def test_a_lone_begin_is_not_unwrapped(self):
        """首尾不成对就不剥 —— 中间的 COMMIT 是作者有意分段。"""
        body, did = unwrap_transaction("BEGIN;\nSELECT 1;\nSELECT 2;\n")
        self.assertFalse(did)

    def test_a_middle_commit_is_not_touched(self):
        sql = "BEGIN;\nSELECT 1;\nCOMMIT;\nSELECT 2;\nCOMMIT;"
        body, did = unwrap_transaction(sql)
        self.assertTrue(did)
        self.assertIn("COMMIT;", body)  # 中间那个留着


class TestChecksum(unittest.TestCase):
    def test_it_is_stable(self):
        self.assertEqual(checksum("SELECT 1;"), checksum("SELECT 1;"))

    def test_it_sees_a_one_character_change(self):
        self.assertNotEqual(checksum("SELECT 1;"), checksum("SELECT 2;"))


class TestDiscover(unittest.TestCase):
    def test_files_come_back_in_name_order(self):
        ms = discover()
        self.assertTrue(ms)
        names = [m.name for m in ms]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "001_kernel.sql")

    def test_a_missing_directory_is_named(self):
        with self.assertRaises(MigrationError) as ctx:
            discover("/definitely/not/here")
        self.assertEqual(ctx.exception.code, "NO_MIGRATIONS_DIR")

    def test_an_empty_directory_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MigrationError) as ctx:
                discover(d)
            self.assertEqual(ctx.exception.code, "NO_MIGRATIONS")

    def test_the_real_files_are_read_verbatim(self):
        """与集成测试、与生产执行的是同一份字节（B-7：不复制）。"""
        ms = discover()
        one = next(m for m in ms if m.name == "017_execution_cancel_attribution.sql")
        disk = (
            Path(__file__).resolve().parents[2]
            / "infrastructure" / "postgres" / "017_execution_cancel_attribution.sql"
        ).read_text(encoding="utf-8")
        self.assertEqual(one.sql, disk)


def _m(name: str, sql: str = "SELECT 1;") -> Migration:
    return Migration(name=name, sql=sql)


class TestPlan(unittest.TestCase):
    def test_a_fresh_database_has_everything_pending(self):
        ms = (_m("001_a.sql"), _m("002_b.sql"))
        p = plan({}, ms)
        self.assertEqual([m.name for m in p.pending], ["001_a.sql", "002_b.sql"])
        self.assertTrue(p.ok)
        self.assertFalse(p.up_to_date)

    def test_a_fully_applied_database_is_up_to_date(self):
        ms = (_m("001_a.sql"), _m("002_b.sql"))
        p = plan({m.name: m.checksum for m in ms}, ms)
        self.assertEqual(p.pending, ())
        self.assertTrue(p.up_to_date)

    def test_only_the_tail_is_pending(self):
        ms = (_m("001_a.sql"), _m("002_b.sql"), _m("003_c.sql"))
        p = plan({ms[0].name: ms[0].checksum}, ms)
        self.assertEqual([m.name for m in p.pending], ["002_b.sql", "003_c.sql"])
        self.assertEqual(p.applied_count, 1)

    def test_a_modified_applied_migration_is_drift_not_pending(self):
        """要害：它**不许**出现在 pending 里 —— 重放一条已上线的迁移会炸。"""
        applied_sql = "ALTER TABLE t ADD COLUMN c TEXT;"
        on_disk_sql = "ALTER TABLE t ADD COLUMN c TEXT; -- 改了一下"
        ms = (_m("017_x.sql", on_disk_sql),)
        p = plan({"017_x.sql": checksum(applied_sql)}, ms)

        self.assertEqual(len(p.drifted), 1)
        self.assertEqual(p.pending, ())          # ← 关键断言
        self.assertFalse(p.ok)
        self.assertFalse(p.up_to_date)

    def test_drift_does_not_stop_reporting_later_pending(self):
        """drift 与 pending 要同时说得出来，不是一个盖住另一个。"""
        ms = (_m("001_a.sql", "SELECT 1;"), _m("002_b.sql", "SELECT 2;"))
        p = plan({"001_a.sql": checksum("SELECT old;")}, ms)
        self.assertEqual(len(p.drifted), 1)
        self.assertEqual([m.name for m in p.pending], ["002_b.sql"])

    def test_report_names_the_two_checksums(self):
        """让人能直接 diff，而不是只看见"有个文件不对"。"""
        ms = (_m("017_x.sql", "new"),)
        p = plan({"017_x.sql": checksum("old")}, ms)
        text = report(p)
        self.assertIn("017_x.sql", text)
        self.assertIn("DRIFT", text)
        self.assertIn(checksum("old"), text)
        self.assertIn(checksum("new"), text)

    def test_report_counts_are_readable(self):
        ms = (_m("001_a.sql"), _m("002_b.sql"))
        self.assertIn("pending: 2", report(plan({}, ms)))
