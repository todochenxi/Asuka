"""M73 · HTTP 推进之后必须落快照。

--------------------------------------------------------------------------
它复现的是一次真实部署里看到的现象

    · 一条 Run 通过 API 推进到 `completed`
    · api Pod 重启（滚动更新 / 探活重启 / 节点漂移）
    · 再查它：状态是 `created`，还挂着一个已经 approved 的审批

原因：`InProcessControlPlane.step_run / drive_run / cancel_run`
只在**内存**里推进，从不落快照；而后台进程走的 `RunDriver.drive()`
每次推进都落（`packages/agent_runtime/driving.py`）。

于是持久化只在后台那条路径上成立 —— 而线上绝大多数 Run 恰恰是
通过 API 推进的。这个洞在"进程不会死"的环境里永远不会被发现，
它只在进程真的会死的地方出现：Kubernetes。

--------------------------------------------------------------------------
为什么这里的"重启"是新建一个 ControlPlane

进程重启 = 内存没了，存储还在。
所以模拟它的正确做法是：**换一个 ControlPlane 实例，共享同一个快照存储**。

刻意不共享 `approvals` 之外的任何内存对象 ——
共享了就等于"进程没死"，那条用例会永远绿，
不管有没有人落快照（这正是它此前没被发现的原因）。
"""
from __future__ import annotations

import unittest
from typing import Any, Mapping

from examples.demo_stack import build_approval_demo_stack_factory
from packages.agent_api.dto import StartRunRequest
from packages.agent_api.service import InProcessControlPlane
from packages.agent_harness.approval import InMemoryApprovalStore
from packages.agent_runtime.recovery import InMemoryRunSnapshotStore


def _plane(snapshots: Any, approvals: Any) -> InProcessControlPlane:
    """走**真的** stack provider，不用测试专用旁路。"""
    return InProcessControlPlane(
        factory=build_approval_demo_stack_factory(),
        approvals=approvals,
        snapshots=snapshots,
    )


class TestAdvancePersistsASnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshots = InMemoryRunSnapshotStore()
        self.approvals = InMemoryApprovalStore()
        self.cp = _plane(self.snapshots, self.approvals)

    def _start(self, request: str = "compute 6*7") -> str:
        view = self.cp.start_run(
            StartRunRequest(agent_id="agent-demo", user_request=request)
        )
        return str(view.run_id)

    # ---------------------------------------------------------- 推进即落快照
    def test_driving_persists_a_snapshot(self):
        """推进之前没有快照 —— 推进之后必须有。"""
        run_id = self._start()
        self.assertIsNone(self.snapshots.latest(run_id))

        self.cp.drive_run(run_id)
        snap = self.snapshots.latest(run_id)
        self.assertIsNotNone(snap, "drive_run must persist a recoverable snapshot")

    def test_stepping_persists_a_snapshot(self):
        run_id = self._start()
        self.cp.step_run(run_id)
        self.assertIsNotNone(self.snapshots.latest(run_id))

    def test_the_snapshot_says_where_it_stopped(self):
        """`reason` 必须说出停在哪 —— 写成 `driven` 等于什么都没说（PR-19）。"""
        run_id = self._start()
        self.cp.drive_run(run_id)
        snap = self.snapshots.latest(run_id)
        assert snap is not None
        self.assertIn("stopped at", snap.reason)
        self.assertNotEqual(snap.reason.strip(), "driven")

    def test_the_snapshot_reason_matches_the_background_path(self):
        """HTTP 推进与后台推进用的是**同一句话**（B-7）。

        两种写法的同一件事，在运维眼里会变成两种不同的推进来源。
        """
        from packages.agent_runtime.driving import snapshot_reason

        run_id = self._start()
        self.cp.drive_run(run_id)
        snap = self.snapshots.latest(run_id)
        assert snap is not None
        # 终态 Run 无法 `rebuild`（R-3），所以这里比对的是**格式本身**：
        # 它必须与后台推进那条路径产出的是同一句话。
        self.assertTrue(
            snap.reason.startswith("driven: run stopped at "),
            f"reason {snap.reason!r} does not come from snapshot_reason()",
        )

    # ---------------------------------------------------------- 重启不回退
    def test_a_completed_run_is_still_completed_after_a_restart(self):
        """要害：这就是部署里那条"回退成 created"的 Run。"""
        run_id = self._start()
        self.cp.drive_run(run_id)  # → suspended（等审批）

        pending = self.approvals.pending()
        self.assertEqual(len(pending), 1)
        # 走 service 的裁决路径 —— 部署里 API 走的也是这一条
        from packages.agent_api.dto import DecisionRequest

        self.cp.decide(
            run_id,
            DecisionRequest(
                approval_id=pending[0].approval_id,
                decision="approve",
                by="ops@example.com",
            )
        )

        self.cp.drive_run(run_id)

        before = self.cp.get_run(run_id)
        self.assertEqual(before.status, "completed")

        # ── 进程重启：换一个实例，只共享存储 ──
        restarted = _plane(self.snapshots, self.approvals)
        after = restarted.get_run(run_id)

        self.assertEqual(
            after.status,
            "completed",
            "a completed run came back as "
            f"{after.status!r} after restart — the HTTP advance path never "
            "persisted a snapshot",
        )
        self.assertIsNone(after.waiting_for)

    def test_a_suspended_run_is_still_suspended_after_a_restart(self):
        """终态要留得住，挂起态同样要留得住 —— 它还在等人批。"""
        run_id = self._start()
        self.cp.drive_run(run_id)

        restarted = _plane(self.snapshots, self.approvals)
        after = restarted.get_run(run_id)
        self.assertEqual(after.status, "suspended")
        self.assertIsNotNone(after.pending_approval)

    # ---------------------------------------------------------- 叫停也落
    def test_cancelling_persists_a_snapshot(self):
        run_id = self._start()
        self.cp.cancel_run(run_id, reason="caller changed its mind", by="ops")
        self.assertIsNotNone(self.snapshots.latest(run_id))

    # ---------------------------------------------------------- 没有存储就不写
    def test_without_a_snapshot_store_nothing_is_written_and_nothing_breaks(self):
        """纯内存控制面：没有"恢复"这回事，落一次只会得到一个写不进去的报错。"""
        cp = InProcessControlPlane(
            factory=build_approval_demo_stack_factory(), approvals=InMemoryApprovalStore()
        )
        self.assertIsNone(cp.snapshots)
        run_id = cp.start_run(
            StartRunRequest(agent_id="agent-demo", user_request="compute 1+1")
        ).run_id
        cp.drive_run(run_id)  # 不许抛
