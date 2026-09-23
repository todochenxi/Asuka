"""`PostgresContextSnapshotStore`：Context 快照落库（M95）。

替换 `InMemoryContextSnapshotStore` —— 后者进程一重启，"模型当时看到了什么"
就查不到了，而那正是 C-5 要回答的问题。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from packages.agent_context.adapters.postgres import PostgresContextSnapshotStore
from packages.agent_context.budget import ContextPlan
from packages.agent_context.items import knowledge_chunk, system
from packages.agent_context.snapshot import build_snapshot

from .sqlite_shim import connect, load_schema_sql


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _plan():
    return ContextPlan(
        kept=(
            knowledge_chunk(
                "redis:expire:001", "EXPIRE key seconds", citation="Redis · EXPIRE"
            ),
            system("you are a technical assistant"),
        ),
        dropped=(),
        total_tokens=12,
    )


class ContextSnapshotStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql("019_context_snapshots.sql"))
        self.addCleanup(self.conn.close)
        self.store = PostgresContextSnapshotStore(self.conn)

    def test_save_and_get_round_trip(self) -> None:
        snap = build_snapshot(
            run_id="run_1",
            plan=_plan(),
            clock=_Clock(),
            model_id="deepseek-chat",
            execution_id="exec_1",
        )
        self.store.save(snap)

        got = self.store.get(snap.snapshot_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.run_id, "run_1")
        self.assertEqual(got.total_tokens, 12)
        self.assertEqual(got.model_id, "deepseek-chat")
        self.assertEqual(got.execution_id, "exec_1")
        self.assertEqual([i.key for i in got.items], ["redis:expire:001", "system"])
        self.assertEqual(got.items[0].reference, "Redis · EXPIRE")
        self.assertTrue(got.items[1].pinned)

    def test_list_for_returns_every_assembly_of_a_run(self) -> None:
        first = build_snapshot(run_id="run_1", plan=_plan(), clock=_Clock())
        second = build_snapshot(run_id="run_1", plan=_plan(), clock=_Clock())
        other = build_snapshot(run_id="run_2", plan=_plan(), clock=_Clock())
        for snap in (first, second, other):
            self.store.save(snap)

        ids = [s.snapshot_id for s in self.store.list_for("run_1")]
        self.assertEqual(sorted(ids), sorted([first.snapshot_id, second.snapshot_id]))

    def test_a_missing_snapshot_is_none_not_empty(self) -> None:
        """控制组：查不到是 `None`，不是"一个空快照" —— 两者含义完全不同。"""
        self.assertIsNone(self.store.get("ctx_does_not_exist"))


if __name__ == "__main__":
    unittest.main()
