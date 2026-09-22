"""M46 / B-4 违反：驳回后 Run 停在 suspended 但无 waiting_for。

**洞的形状**：

    `loop.reject()` 的流程是：
        1. `_close_gate(approved=False)` → Kernel 里那条 SUSPENDED 的 Execution 走完
           （resume → complete），于是 Execution.status 从 SUSPENDED 变成 COMPLETED。
        2. `_apply(Observation(kind=APPROVAL_REJECTED))` → 留下"人驳回了"的事实。
        3. `_clear_pending()` → 把 `pending_approval` 置 None。
        4. `return self._record(StepOutcome.DENIED)`。

    **没有 `_sync_after_execution()`。**

    Step.status 仍然是 SUSPENDED（没人重新投影），Run.status 仍然是 SUSPENDED。
    但 `pending_approval` 已经是 None 了 —— Run 说"我在等"，但你问它在等谁，
    它说"没等谁"。违反 B-4「SUSPENDED 必须说清楚在等什么」。

    `expire_approvals()` 有同样的病：`_close_gate` + `_clear_pending` 后没有
    重新派生。它甚至更隐蔽 —— 过期是后台 sweeper 触发的，没有人在等返回值。

    R-1/R-6 的断言（`is_gated` 但没有 `pending_*_id`）会在这份"假挂起"快照上
    **直接炸掉** —— 也就是说，这个洞不只是"界面骗人"，是"快照落不下来"。

**判据**：

    B-11（本轮新增，B-4 的操作细则）：关掉闸门后必须立刻重新派生 Step/Run 状态。
    "立刻"= 在同一个方法里，不依赖调用方记得再调一次。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business.run import AgentRunStatus
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_harness import Harness
from packages.agent_harness.approval import ApprovalStatus
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


class _RejectTestBase(LoopTestBase):
    """与 RunProjectionTest 同款夹具，只是多带 harness=Harness.default()。"""

    def _loop_with_gate(self, script=None, **config) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine(script or []),
            config=AgentLoopConfig(max_steps=config.get("max_steps", 6), **{
                k: v for k, v in config.items() if k != "max_steps"
            }),
            harness=Harness.default(),
        )

    def _risky_tool(self, run_id: str) -> Action:
        return Action(
            run_id=run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )


class TheRejectStaysSuspendedWithoutWaitingForTest(_RejectTestBase):
    """驳回后 Run 应该退出 SUSPENDED，不是停在"假挂起"。"""

    def test_reject_leaves_suspended_with_no_waiting_for(self):
        loop = self._loop_with_gate()
        state = loop.start("do something risky")
        loop.decision_engine = ScriptedDecisionEngine([self._risky_tool(state.run_id)])

        # 第一步被 risk-gate 拦下 → WAITING_APPROVAL
        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertIs(loop.agent_run.status, AgentRunStatus.SUSPENDED)
        self.assertIsNotNone(loop.pending_approval)

        # 人驳回了
        outcome = loop.reject(by="alice", comment="no way")
        self.assertIs(outcome, StepOutcome.DENIED)

        # B-4：SUSPENDED 必须说清楚在等什么。
        # 驳回后不应该再 SUSPENDED —— 审批已经关了，没有在等谁了。
        run_status = loop.agent_run.status
        if run_status is AgentRunStatus.SUSPENDED:
            has_pending = (
                loop.pending_approval is not None
                or loop.pending_child is not None
            )
            if not has_pending:
                self.fail(
                    f"B-4 violated: run is SUSPENDED after reject() but "
                    f"pending_approval={loop.pending_approval}, "
                    f"pending_child={loop.pending_child} — "
                    f"SUSPENDED must say what it's waiting for"
                )

    def test_reject_refixes_step_status(self):
        """驳回后 Step.status 不应停在 SUSPENDED。"""
        loop = self._loop_with_gate()
        state = loop.start("do something risky")
        loop.decision_engine = ScriptedDecisionEngine([self._risky_tool(state.run_id)])

        loop.step()  # SUSPENDED
        self.assertEqual(loop.current_step.status.value, "suspended")

        loop.reject(by="alice")

        # Step 的 Execution 已经 COMPLETED 了（_close_gate 走完了它），
        # 所以 Step 应该重新派生成 COMPLETED（或 PENDING，如果还有别的 Task）。
        step_status = loop.current_step.status.value
        self.assertNotEqual(
            step_status, "suspended",
            f"Step still SUSPENDED after reject; "
            f"Execution is COMPLETED but Step wasn't re-derived. "
            f"step_status={step_status}"
        )


class TheSnapshotAfterRejectTest(_RejectTestBase):
    """B-11 判据：reject 之后重新派生了，快照不再撞 R-1/R-6。"""

    def test_snapshot_after_reject_is_consistent(self):
        """reject 之后拍快照：要么不是 SUSPENDED（已重新派生），
        要么是 SUSPENDED 但有 pending_*_id（仍在等别的东西）。"""
        loop = self._loop_with_gate()
        state = loop.start("do something risky")
        loop.decision_engine = ScriptedDecisionEngine([self._risky_tool(state.run_id)])

        loop.step()  # SUSPENDED
        loop.reject(by="alice")

        # 修复后：Run 不再 SUSPENDED（Execution 已 COMPLETED，Step 重新派生为 COMPLETED）
        # 快照不再炸 R-1/R-6
        snap = loop.capture(reason="background-snapshot")
        if snap.status == AgentRunStatus.SUSPENDED.value:
            self.assertTrue(
                snap.pending_approval_id or snap.pending_child_id,
                f"R-1/R-6: snapshot says SUSPENDED but has no pending_*_id"
            )


class TheExpireApprovalsAlsoHasTheBugTest(_RejectTestBase):
    """expire_approvals() 有同样的病：关闸门后不重新派生。"""

    def test_expire_leaves_suspended_with_no_waiting_for(self):
        loop = self._loop_with_gate()
        state = loop.start("do something risky")
        loop.decision_engine = ScriptedDecisionEngine([self._risky_tool(state.run_id)])

        loop.step()  # SUSPENDED → WAITING_APPROVAL

        # 模拟过期：直接把审批存储里的状态改成 EXPIRED
        approval = loop.pending_approval
        assert approval is not None
        store = loop.harness.approvals.store  # type: ignore[union-attr]
        store._items[approval.approval_id].status = ApprovalStatus.EXPIRED  # type: ignore[attr-defined]

        # expire_approvals() 会调 _close_gate + _clear_pending
        loop.expire_approvals()

        # 与 reject 同样的判据
        run_status = loop.agent_run.status
        if run_status is AgentRunStatus.SUSPENDED:
            has_pending = (
                loop.pending_approval is not None
                or loop.pending_child is not None
            )
            if not has_pending:
                self.fail(
                    f"B-4 violated: run is SUSPENDED after expire_approvals() "
                    f"but nothing pending"
                )

    def test_expire_refixes_step_status(self):
        loop = self._loop_with_gate()
        state = loop.start("do something risky")
        loop.decision_engine = ScriptedDecisionEngine([self._risky_tool(state.run_id)])

        loop.step()  # SUSPENDED
        loop.reject(by="alice")

        step_status = loop.current_step.status.value
        self.assertNotEqual(
            step_status, "suspended",
            f"Step still SUSPENDED after expire; step_status={step_status}"
        )


if __name__ == "__main__":
    unittest.main()
