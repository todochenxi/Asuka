"""M117 / M118：跨重启的 Run 清单与账本。

空洞：`GET /runs` 读的是**内存** `self.runs`，`GET /runs/{id}/trace` 走
`_must_stack` —— 于是进程重启后，旧 Run 一个都列不出来、账本也读不到。
持久源其实在 PG（`run_snapshots`），缺的只是**读它的出口**。

这里用内存版 `RunSnapshotStore` 模拟"重启"：新建一个空 ControlPlane，
只把**同一份** snapshots 递进去。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from examples.demo_stack import build_approval_demo_stack_factory, build_stack_factory
from packages.agent_api.dto import StartRunRequest
from packages.agent_api.service import InProcessControlPlane
from packages.agent_domain.business.snapshot import RunSnapshot
from packages.agent_harness.approval import InMemoryApprovalStore
from packages.agent_runtime.recovery import InMemoryRunSnapshotStore, RunSummary


def _snapshot(run_id: str, *, status: str = "completed", at: datetime | None = None,
              trace: tuple = ()) -> RunSnapshot:
    # R-1/R-6：SUSPENDED 快照必须说清在等谁 —— 这里给一个审批 id
    pending = "appr_1" if status == "suspended" else None
    return RunSnapshot(
        run_id=run_id,
        agent_id="agent-api",
        status=status,
        step_count=2,
        pending_approval_id=pending,
        state={"run_id": run_id, "goal": {"objective": "x", "run_id": run_id}},
        trace=trace,
        created_at=at or datetime(2026, 9, 24, tzinfo=timezone.utc),
    )


class SnapshotListRunsTest(unittest.TestCase):
    def test_empty_is_empty(self) -> None:
        self.assertEqual(list(InMemoryRunSnapshotStore().list_runs()), [])

    def test_it_keeps_only_the_newest_snapshot_per_run(self) -> None:
        store = InMemoryRunSnapshotStore()
        t0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
        store.save(_snapshot("run_a", status="suspended", at=t0))
        store.save(_snapshot("run_a", status="completed", at=t0 + timedelta(seconds=5)))
        runs = list(store.list_runs())
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "completed")

    def test_it_orders_newest_first(self) -> None:
        store = InMemoryRunSnapshotStore()
        t0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
        store.save(_snapshot("run_old", at=t0))
        store.save(_snapshot("run_new", at=t0 + timedelta(minutes=1)))
        self.assertEqual([r.run_id for r in store.list_runs()], ["run_new", "run_old"])

    def test_limit_is_respected(self) -> None:
        store = InMemoryRunSnapshotStore()
        t0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
        for i in range(5):
            store.save(_snapshot(f"run_{i}", at=t0 + timedelta(seconds=i)))
        self.assertEqual(len(store.list_runs(limit=2)), 2)


class CrossRestartTest(unittest.TestCase):
    """模拟重启：新建空 ControlPlane，只共享同一份 snapshots。"""

    def test_a_restarted_process_lists_a_persisted_run(self) -> None:
        snapshots = InMemoryRunSnapshotStore()
        snapshots.save(_snapshot("run_gone"))

        cp = InProcessControlPlane(
            factory=build_stack_factory(),
            approvals=InMemoryApprovalStore(),
            snapshots=snapshots,
        )
        runs = cp.list_runs()
        self.assertEqual([r.run_id for r in runs], ["run_gone"])
        self.assertEqual(runs[0].source, "snapshots")
        # 装载不回来（R-3：终态不可恢复）—— 所以 loadable=False
        self.assertFalse(runs[0].loadable)

    def test_a_restarted_process_can_read_the_snapshot_ledger(self) -> None:
        """M118：账本读得到 —— 这正是"可审计"该给的。"""
        snapshots = InMemoryRunSnapshotStore()
        snapshots.save(
            _snapshot(
                "run_gone",
                trace=(
                    {"seq": 1, "kind": "task.submitted", "run_id": "run_gone",
                     "step_id": "step_1", "task_id": "task_1", "execution_id": "exec_1",
                     "attempt_no": 0, "payload": {"action_type": "llm_call"}},
                    {"seq": 2, "kind": "run.finished", "run_id": "run_gone",
                     "step_id": "", "task_id": "", "execution_id": "", "attempt_no": 0,
                     "payload": {}},
                ),
            )
        )
        cp = InProcessControlPlane(
            factory=build_stack_factory(),
            approvals=InMemoryApprovalStore(),
            snapshots=snapshots,
        )
        trace = cp.get_trace("run_gone")
        self.assertEqual(trace.source, "snapshot")
        self.assertEqual(len(trace.entries), 2)
        self.assertEqual(trace.layers["actions"][0]["action_type"], "llm_call")
        # 只有账本时推不出 Goal / Plan —— 不放那个键
        self.assertNotIn("goal", trace.layers)

    def test_an_unknown_run_still_404s(self) -> None:
        cp = InProcessControlPlane(
            factory=build_stack_factory(),
            approvals=InMemoryApprovalStore(),
            snapshots=InMemoryRunSnapshotStore(),
        )
        with self.assertRaises(Exception):
            cp.get_trace("run_never_existed")

    def test_an_in_memory_run_is_not_shadowed_by_snapshots(self) -> None:
        """装载过的那条优先（可推进）；同一 run_id 不在清单里出现两次。"""
        snapshots = InMemoryRunSnapshotStore()
        cp = InProcessControlPlane(
            factory=build_stack_factory(),
            approvals=InMemoryApprovalStore(),
            snapshots=snapshots,
        )
        view = cp.start_run(
            StartRunRequest(agent_id="agent-api", user_request="compute 6*7")
        )
        # Loop 每次推进都落快照；这里手工补一条，确保 run_id 会同时出现在两处
        snapshots.save(_snapshot(view.run_id))
        runs = cp.list_runs()
        self.assertEqual([r.run_id for r in runs].count(view.run_id), 1)
        self.assertEqual(runs[0].source, "memory")

    def test_without_a_snapshot_store_it_is_memory_only(self) -> None:
        cp = InProcessControlPlane(factory=build_stack_factory())
        cp.start_run(StartRunRequest(agent_id="agent-api", user_request="a"))
        self.assertEqual(len(cp.list_runs()), 1)
        # 没配 snapshots：查一条不存在的 Run 仍 404
        with self.assertRaises(Exception):
            cp.get_trace("nope")


class TheFileCabinetIsRealTest(unittest.TestCase):
    """控制组：RunSummary 真的带上了那几列 —— 上一条不是"恰好通过"。"""

    def test_summary_carries_the_listing_columns(self) -> None:
        s = RunSummary(run_id="run_1", agent_id="a", status="completed", step_count=3)
        self.assertEqual((s.run_id, s.agent_id, s.status, s.step_count),
                         ("run_1", "a", "completed", 3))


if __name__ == "__main__":
    unittest.main()
