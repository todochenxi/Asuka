"""`PostgresMemoryStore`：记忆落库（M96）。

此前只有内存实现 —— 进程一重启"它记得什么"就没了，而记忆的全部意义是跨 Run。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from packages.agent_context.adapters.memory_postgres import PostgresMemoryStore
from packages.agent_context.memory import MemoryLayer, MemoryRecord

from .sqlite_shim import connect, load_schema_sql


class PostgresMemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql("020_memories.sql"))
        self.addCleanup(self.conn.close)
        self.store = PostgresMemoryStore(self.conn)

    def _record(self, **over) -> MemoryRecord:
        base = dict(
            layer=MemoryLayer.EPISODIC,
            subject="agent-it",
            content="how do I set a timeout => EXPIRE key seconds",
            source_run_id="run_1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        base.update(over)
        return MemoryRecord(**base)

    def test_save_and_search_round_trip(self) -> None:
        self.store.save(self._record())

        found = self.store.search(layer=MemoryLayer.EPISODIC, subject="agent-it")
        self.assertEqual(len(found), 1)
        self.assertIn("EXPIRE key seconds", found[0].content)
        self.assertEqual(found[0].source_run_id, "run_1")

    def test_search_filters_by_subject_and_query(self) -> None:
        self.store.save(self._record(subject="agent-it", content="redis expire"))
        self.store.save(self._record(subject="agent-research", content="redis set"))

        self.assertEqual(len(self.store.search(subject="agent-it")), 1)
        self.assertEqual(len(self.store.search(query="redis")), 2)
        self.assertEqual(len(self.store.search(query="expire")), 1)

    def test_delete_removes_the_row(self) -> None:
        rec = self._record()
        self.store.save(rec)
        self.store.delete(rec.memory_id)
        self.assertEqual(self.store.search(subject="agent-it"), [])

    def test_episodic_without_a_source_run_is_refused(self) -> None:
        """C-8 在领域层兜底：不知道从哪来的事件记忆既无法审计也无法定点删除。"""
        with self.assertRaises(ValueError):
            self._record(source_run_id="")


if __name__ == "__main__":
    unittest.main()
