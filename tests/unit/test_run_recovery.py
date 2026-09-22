"""M20：Run Recovery —— 从快照把挂起的 Run 装载回来。

覆盖的不变量：

    R-1  进入 SUSPENDED 前必须落 Snapshot（只落 RunCheckpoint 恢复不了：它是指针不是数据）
    R-2  恢复必须带上预算计数与已花费 —— 否则"挂起—恢复"是重置预算的后门
    R-3  终态 Run 不可恢复
    R-4  审计账本必须延续（run_id 连续 + 序号连续 + 留一条 RECOVERED）
    R-5  恢复由 Runtime 做，不由 API 做

跑在 sqlite 上的 PG 方言替身，但 schema 直接读 `infrastructure/postgres/` 原文。
"""
from __future__ import annotations

import unittest

from packages.agent_api import (
    InProcessControlPlane,
    decide_approval,
    get_run,
    list_approvals,
    start_run,
)
from packages.agent_domain.business.snapshot import RunSnapshot
from packages.agent_domain.errors import IllegalTransition, InvariantViolation
from packages.agent_harness.adapters.postgres import PostgresApprovalStore
from packages.agent_runtime.adapters.postgres import PostgresRunSnapshotStore
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.recovery import RunRecovery
from packages.agent_runtime.trace import RunTrace
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
)
from packages.execution_kernel.inmemory import ManualClock
from packages.execution_kernel.kernel import ExecutionKernel

from .sqlite_shim import connect, load_schema_sql
from .test_control_plane_api import (
    RiskyThenFinish,
    build_gateway,
    build_tool_runtime,
)
from .test_agent_loop_full import Interpreter, Planner

# 009 是 004 的 companion：快照表补"在等哪条子 Run"那一列（R-6）。
# 少了它，`PostgresRunSnapshotStore` 的 INSERT 会比表多一个参数 ——
# 也就是说"加载 004 却不加载 009"是一个**跑不起来**的组合。
# 018 同款（R-7 / M86）：再补一列"注入的智能实现自述的进度"。
_SCHEMA = (
    "001_kernel.sql",
    "003_approvals.sql",
    "004_run_snapshots.sql",
    "009_snapshot_pending_child.sql",
    "018_snapshot_port_progress.sql",
)


# ---------------------------------------------------------------- 底座
class RecoveryTestBase(unittest.TestCase):
    """一套"共享的数据库"：审批、快照、Kernel 全部落在同一份持久存储上。

    Kernel 也必须是持久的 —— 恢复的前提是那条 SUSPENDED 的 Execution 还在库里。
    内存 Kernel 一重启就没了，那样测的不是恢复，是巧合。
    """

    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.clock = ManualClock()
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            clock=self.clock,
        )
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.approvals = PostgresApprovalStore(self.conn)
        self.gateway = build_gateway()
        self.tool_runtime = build_tool_runtime()

    def tearDown(self) -> None:
        self.conn.close()

    def _factory(self, agent_id: str, approvals):
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=Interpreter(),
            planner=Planner(),
            decision_engine=RiskyThenFinish(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
            kernel=self.kernel,
            approval_store=approvals,
            snapshots=self.snapshots,
        )

    def _new_control_plane(self, **kwargs):
        kwargs.setdefault("approvals", self.approvals)
        return InProcessControlPlane(factory=self._factory, **kwargs)

    def _start_and_gate(self, cp):
        """起一个 Run 并让它被闸门挡住（高风险工具调用）。"""
        resp = start_run(cp, {"agent_id": "agent-rec", "user_request": "compute 6*7"})
        self.assertEqual(resp.status, 201, resp.body)
        run_id = resp.body["run_id"]
        stack = cp.runs[run_id]
        self.assertEqual(stack.loop.step().value, "waiting_approval")
        return run_id, stack


# ---------------------------------------------------------------- R-1
class SnapshotWritingTest(RecoveryTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.cp = self._new_control_plane(snapshots=self.snapshots)
        self.run_id, self.stack = self._start_and_gate(self.cp)

    def test_r1_snapshot_exists_once_gated(self) -> None:
        snap = self.snapshots.latest(self.run_id)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.pending_approval_id, self.stack.loop.pending_approval.approval_id)

    def test_r1_snapshot_carries_the_whole_state(self) -> None:
        """只带 current_step 的快照是"指针"，不是"数据"。"""
        snap = self.snapshots.latest(self.run_id)
        assert snap is not None
        self.assertIn("observations", snap.state)
        self.assertTrue(snap.state["observations"])       # 请求审批这条事实在里面
        self.assertEqual(
            snap.state["goal"]["objective"], self.stack.loop.state.goal.objective
        )

    def test_r1_snapshot_carries_the_audit_ledger(self) -> None:
        """R-4 的前半段：账本要被带走，否则重启就把一个 Run 的账切成两截。"""
        snap = self.snapshots.latest(self.run_id)
        assert snap is not None
        self.assertTrue(snap.trace)
        self.assertEqual(
            {e["run_id"] for e in snap.trace}, {self.run_id}
        )

    def test_snapshot_without_state_is_refused(self) -> None:
        """没有 State 的快照不是"退化版快照"，是**没法恢复的快照**。"""
        with self.assertRaises(InvariantViolation):
            RunSnapshot(run_id="run_x", state={})

    def test_gated_snapshot_must_carry_approval_id(self) -> None:
        """挂着等审批却不知道在等哪条 —— 恢复出来也接不上人。"""
        with self.assertRaises(InvariantViolation):
            RunSnapshot(
                run_id="run_x",
                status="suspended",
                state={"run_id": "run_x"},
                pending_approval_id=None,
            )


# ---------------------------------------------------------------- R-2 / R-3
class RestoreSemanticsTest(RecoveryTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.cp = self._new_control_plane(snapshots=self.snapshots)
        self.run_id, self.stack = self._start_and_gate(self.cp)

    def _rebuilt(self):
        recovery = RunRecovery(
            snapshots=self.snapshots, factory=self._factory, approvals=self.cp.approvals
        )
        return recovery.rebuild(self.run_id)

    def test_r2_counters_come_back(self) -> None:
        """预算计数 / 连续被拒 / 已花费，一个都不能从零开始。"""
        loop = self.stack.loop
        loop.steps = 7
        loop.consecutive_denials = 2
        loop.harness.cost.spent_cost = 1.25
        loop.harness.cost.spent_tokens = 900
        snap = loop.capture(reason="test")
        self.snapshots.save(snap)

        rebuilt = self._rebuilt()
        self.assertEqual(rebuilt.loop.steps, 7)
        self.assertEqual(rebuilt.loop.consecutive_denials, 2)
        self.assertEqual(rebuilt.loop.harness.cost.spent_cost, 1.25)
        self.assertEqual(rebuilt.loop.harness.cost.spent_tokens, 900)

    def test_r2_the_budget_reset_backdoor_this_closes(self) -> None:
        """对照：不恢复计数会怎样 —— 走到预算上限的 Run 挂起一次就又是一条好汉。

        这条存在的意义是证明上面那条**不是白测的**。
        """
        loop = self.stack.loop
        loop.steps = 7
        self.assertEqual(loop.steps, 7)

        # 不落快照、直接换一个新 Loop（等价于"恢复"时只把 State 装回来）
        fresh = self._factory("agent-rec", self.cp.approvals)
        self.assertEqual(fresh.loop.steps, 0)               # ← 预算无声地重置了

    def test_r3_terminal_run_cannot_be_restored(self) -> None:
        """终态不可变（§10）。能"恢复"一个已结束的 Run，等于给终态开后门。"""
        loop = self.stack.loop
        snap = loop.capture(reason="test")
        terminal = RunSnapshot(
            run_id=snap.run_id,
            agent_id=snap.agent_id,
            status="completed",
            state=snap.state,
            steps=snap.steps,
            trace=snap.trace,
        )
        self.snapshots.save(terminal)
        with self.assertRaises(IllegalTransition):
            RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=self.cp.approvals
            ).rebuild(self.run_id)

    def test_restored_loop_is_still_gated(self) -> None:
        """恢复出来必须知道自己在等谁 —— 否则 approve() 找不到 pending_approval。"""
        rebuilt = self._rebuilt()
        self.assertIsNotNone(rebuilt.loop.pending_approval)
        self.assertEqual(
            rebuilt.loop.pending_approval.approval_id,
            self.stack.loop.pending_approval.approval_id,
        )


# ---------------------------------------------------------------- R-4
class TraceContinuityTest(unittest.TestCase):
    def test_r4_restored_trace_keeps_its_sequence(self) -> None:
        trace = RunTrace()
        trace.run_id = "run_1"
        trace.append("a")
        trace.append("b")
        carried = tuple(trace.entries)

        fresh = RunTrace()
        fresh.restore(carried)
        self.assertEqual(len(fresh), 2)
        self.assertEqual(fresh.entries[1].seq, 2)

    def test_r4_broken_sequence_is_refused_not_spliced(self) -> None:
        """断号的账本不能悄悄接上 —— 那会让"丢了一段"看起来像"本来就没有"。"""
        trace = RunTrace()
        trace.run_id = "run_1"
        trace.append("a")
        trace.append("b")
        trace.append("c")
        # 掉**中间**那条：剩 seq=1 与 seq=3。掉最后一条不构成断号，那是截断不是空洞。
        holed = (trace.entries[0], trace.entries[2])

        fresh = RunTrace()
        with self.assertRaises(InvariantViolation):
            fresh.restore(holed)

    def test_r4_cannot_restore_over_an_existing_ledger(self) -> None:
        """L-6：往非空账本里装 = 覆盖 = 改写事实。"""
        trace = RunTrace()
        trace.run_id = "run_1"
        trace.append("a")
        with self.assertRaises(InvariantViolation):
            trace.restore([])


# ---------------------------------------------------------------- 端到端
class RestartThenDecideTest(RecoveryTestBase):
    """M19 解决了"看得见"，这一轮解决"点得动"。"""

    def test_decide_after_a_restart(self) -> None:
        first = self._new_control_plane(snapshots=self.snapshots)
        run_id, stack = self._start_and_gate(first)
        approval = list_approvals(first, run_id).body["items"][0]

        # ── 重启：内存里的 RuntimeStack 全丢，PG 还在 ──
        second = self._new_control_plane(snapshots=self.snapshots)
        self.assertEqual(second.runs, {})
        # 待批事项还看得见（M19）
        self.assertEqual(len(list_approvals(second, run_id).body["items"]), 1)
        # Run 也还查得到
        self.assertEqual(get_run(second, run_id).status, 200)

        resp = decide_approval(
            second, run_id, approval["approval_id"],
            {"decision": "approve", "by": "alice"},
        )
        self.assertEqual(resp.status, 200, resp.body)

        # 被闸门挡住的那个动作真的执行了（Kernel 里那条 SUSPENDED 被唤醒并走完）
        rebuilt = second.runs[run_id]
        self.assertIn("executed", [h.value for h in rebuilt.loop.history])
        # 审批状态是真的被推进了，不是"批了一下"
        decided = second.approvals.get(approval["approval_id"])
        self.assertEqual(decided.status.value, "approved")
        self.assertEqual(decided.decided_by, "alice")

    def test_r5_trace_survives_the_restart(self) -> None:
        """恢复出来的 Run 的账本里，前半段还在 —— 不是一个新账本。"""
        first = self._new_control_plane(snapshots=self.snapshots)
        run_id, _ = self._start_and_gate(first)
        before = len(first.runs[run_id].loop.trace)

        second = self._new_control_plane(snapshots=self.snapshots)
        get_run(second, run_id)                     # 触发从快照装载
        rebuilt = second.runs[run_id]

        self.assertGreaterEqual(len(rebuilt.loop.trace), before)
        self.assertIn("run.recovered", rebuilt.loop.trace.kinds())

    def test_without_snapshots_the_restart_404s(self) -> None:
        """对照：不给快照存储，重启后就点不动了（404）。

        这条存在的意义是证明上面那条**不是白测的**。
        """
        first = self._new_control_plane()
        run_id, _ = self._start_and_gate(first)
        approval = list_approvals(first, run_id).body["items"][0]

        second = self._new_control_plane()
        resp = decide_approval(
            second, run_id, approval["approval_id"],
            {"decision": "approve", "by": "alice"},
        )
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body["error"]["code"], "RUN_NOT_FOUND")

    def test_unknown_run_still_404s(self) -> None:
        """恢复失败 ≠ 假装有这个 Run。"""
        cp = self._new_control_plane(snapshots=self.snapshots)
        self.assertEqual(get_run(cp, "run_nope").status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
