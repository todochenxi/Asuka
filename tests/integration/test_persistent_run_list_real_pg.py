"""M117 / M118 · 跨重启的 Run 清单与账本，在**真 PG** 上。

unit 层用内存快照存模拟重启；这里用真 PG：一个连接写快照，一个**全新的**
`PostgresRunSnapshotStore` + 空 ControlPlane 去读 —— 那才叫"进程重启"。
"""
from __future__ import annotations

from datetime import datetime, timezone

from packages.agent_domain.business.snapshot import RunSnapshot
from packages.agent_runtime.adapters.postgres import PostgresRunSnapshotStore

from ._pg import RealPostgresCase


def _snapshot(run_id: str, *, trace: tuple = (), at: datetime | None = None) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        agent_id="agent-api",
        status="completed",
        step_count=2,
        state={"run_id": run_id, "goal": {"objective": "x", "run_id": run_id}},
        trace=trace,
        created_at=at or datetime(2026, 9, 24, tzinfo=timezone.utc),
    )


class PersistentRunListOnRealPostgresTest(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresRunSnapshotStore(self.conn)

    def test_list_runs_survives_a_new_store_object(self) -> None:
        self.store.save(_snapshot("run_a"))
        # "新进程"：一个全新的 store，只共享数据库
        fresh = PostgresRunSnapshotStore(self.conn)
        runs = list(fresh.list_runs())
        self.assertEqual([r.run_id for r in runs], ["run_a"])
        self.assertEqual(runs[0].agent_id, "agent-api")

    def test_distinct_on_keeps_only_the_newest(self) -> None:
        self.store.save(_snapshot("run_a", at=datetime(2026, 9, 24, tzinfo=timezone.utc)))
        self.store.save(_snapshot("run_a", at=datetime(2026, 9, 24, 1, tzinfo=timezone.utc)))
        runs = list(PostgresRunSnapshotStore(self.conn).list_runs())
        self.assertEqual(len(runs), 1)

    def test_the_ledger_is_readable_after_restart(self) -> None:
        self.store.save(
            _snapshot(
                "run_hist",
                trace=(
                    {"seq": 1, "kind": "task.submitted", "run_id": "run_hist",
                     "step_id": "step_1", "task_id": "task_1", "execution_id": "exec_1",
                     "attempt_no": 0, "payload": {"action_type": "llm_call"}},
                    {"seq": 2, "kind": "run.finished", "run_id": "run_hist",
                     "step_id": "", "task_id": "", "execution_id": "", "attempt_no": 0,
                     "payload": {}},
                ),
            )
        )
        got = PostgresRunSnapshotStore(self.conn).latest("run_hist")
        assert got is not None
        self.assertEqual(len(got.trace), 2)
        self.assertEqual(got.trace[0]["kind"], "task.submitted")


if __name__ == "__main__":
    import unittest

    unittest.main()
