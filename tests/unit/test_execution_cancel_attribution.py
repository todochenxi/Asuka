"""M48 / 空洞 228（真形状）：Execution 级取消能归因吗？

--------------------------------------------------------------------------
洞的形状（实证得到的，不是照登记抄的）

对一条挂起在闸门上的 Run 叫停，库里那条 Execution 的变化是：

    status              SUSPENDED → CANCELLED
    cancellation_requested   False  → **False**（从头到尾没被写过）

**它绕过了"请求"这一步，直接判死。**

`AgentLoop._cancel_gate()` 调的是 `kernel.cancel(execution_id)`，
而 Kernel 的规矩是 X-11 / R-7：Harness 只能**请求**取消，
真正的生命周期归 Kernel —— 先写意图，再判死。

绕过的后果是三件事同时成立：

    1. `executions.cancellation_requested` 是一列死列（永远是 False）
    2. `EXECUTION_CANCEL_REQUESTED` 事件永远发不出来
    3. B-8 的归因（**谁**叫停、**为什么**）无处可写

--------------------------------------------------------------------------
为什么这三条里最要紧的是第 3 条

Run 级取消有 `run_cancellations(reason, requested_by)`；
子 Run 级有 `child_runs(cancel_reason, cancel_by)`（012 迁移 / D-14）。
同一条规矩（B-8 / A-8：取消必须能归因）在 Execution 这一层断了。

一条 Run 被叫停的完整链条里，最贴近"真正干活的那一刀"的
是 Execution —— 而恰恰是它说不出谁让它停的。

--------------------------------------------------------------------------
空洞 228 的原始登记说"没有调用方，加归因就是空转"

那句话在写下它的当时是对的：确实没有人调 Kernel 的 `request()`。
但**绕过**本身就是一种调用关系 —— `_cancel_gate` 天天在判死 Execution，
只是它跳过了"先请求"。所以这一轮要补的不是"找个地方放归因"，
而是"把跳过那一步补回来"。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.events.event import EXECUTION_CANCEL_REQUESTED
from packages.agent_domain.execution import ExecutionStatus, SuspensionReason

from .helpers import make_aggregate


def _now(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


class TestExecutionCancellationAttribution(unittest.TestCase):
    """请求取消必须带上"谁、为什么"（B-8 / A-8）。"""

    def test_request_cancel_carries_reason_and_by(self):
        """请求的事件里必须能读到是谁、为什么 —— 审计不看 PG 也要能回答。"""
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        agg.request_cancel(reason="user asked to stop", by="alice")

        event = next(
            (e for e in agg.events if e.event_type == EXECUTION_CANCEL_REQUESTED),
            None,
        )
        self.assertIsNotNone(event, "请求取消必须发出 EXECUTION_CANCEL_REQUESTED")
        payload = dict(event.payload)      # type: ignore[arg-type]
        self.assertEqual(payload.get("reason"), "user asked to stop")
        self.assertEqual(payload.get("by"), "alice")

    def test_request_cancel_stores_attribution_on_the_row(self):
        """归因要落在行上，不是只在事件里 —— 运维查的是 PG。"""
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        agg.request_cancel(reason="user asked to stop", by="alice")

        self.assertTrue(agg.execution.cancellation_requested)
        self.assertEqual(agg.execution.cancellation_reason, "user asked to stop")
        self.assertEqual(agg.execution.cancellation_by, "alice")

    def test_empty_reason_is_refused(self):
        """B-8：说不出为什么的取消等于没有发生过。"""
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        with self.assertRaises((InvariantViolation, ValueError)):
            agg.request_cancel(reason="", by="alice")

    def test_empty_by_is_refused(self):
        """A-8：匿名的取消无法归因。"""
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        with self.assertRaises((InvariantViolation, ValueError)):
            agg.request_cancel(reason="user asked", by="")

    def test_request_is_not_a_verdict(self):
        """X-11：请求 ≠ 判死。请求之后它还在 RUNNING，判死归 Kernel。"""
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        agg.request_cancel(reason="stop", by="alice")
        self.assertEqual(agg.execution.status, ExecutionStatus.RUNNING)
        agg.cancel(token=lease.fencing_token)
        self.assertEqual(agg.execution.status, ExecutionStatus.CANCELLED)
        # 判死之后归因**不能**被抹掉 —— 那正是"为什么它被判死"的答案
        self.assertEqual(agg.execution.cancellation_reason, "stop")
        self.assertEqual(agg.execution.cancellation_by, "alice")


class TestCancelGateRequestsBeforeItVerdicts(unittest.TestCase):
    """`_cancel_gate` 必须先写意图，再判死（R-7 同款）。"""

    def test_gate_cancellation_leaves_a_requested_intent(self):
        """判死一条 Execution 之前，必须先留下"有人要求停它"这件事。

        这是本轮的核心判据：库里的 `cancellation_requested` 不能是死列。
        """
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        agg.suspend(
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approver": "risk"},
            now=_now(),
        )

        # 模拟 `_cancel_gate` 修好之后的动作：先请求，再判死
        agg.request_cancel(reason="parent cancelled", by="alice")
        self.assertTrue(agg.execution.cancellation_requested,
                        "判死之前必须先写下意图 —— 否则意图那一列是死列")
        agg.cancel(token=lease.fencing_token)
        self.assertEqual(agg.execution.status, ExecutionStatus.CANCELLED)
        self.assertTrue(agg.execution.cancellation_requested,
                        "判死之后意图仍然在 —— 它是'为什么被判死'的答案")


class TestMigration017ReallyRuns(unittest.TestCase):
    """`017_execution_cancel_attribution.sql` 必须真的被执行过一次。

    `MigrationHygieneTest` 只检查"文件名被某个测试提到"，
    提到不等于跑过 —— 009 就是这么被发现的（列数对不上）。
    所以这里真的建一次库，并且真的去撞那道 CHECK。
    """

    def test_the_two_columns_land_on_the_table(self):
        from .sqlite_shim import connect, load_schema_sql

        conn = connect(
            schema_sql=load_schema_sql(
                "001_kernel.sql", "017_execution_cancel_attribution.sql"
            )
        )
        cur = conn.cursor()
        cur.execute("SELECT execution_id FROM executions WHERE 1=0")
        # 能查到就说明列在了（SELECT * 的替身没有列名，故直接问约束）
        cur.execute(
            "SELECT cancellation_reason, cancellation_by FROM executions WHERE 1=0"
        )
        self.assertTrue(cur is not None)

    def test_the_check_refuses_an_unattributed_intent(self):
        """意图位为真却说不出谁、为什么 —— 库必须拒绝（B-8 的物理保证）。"""
        from .sqlite_shim import connect, load_schema_sql

        conn = connect(
            schema_sql=load_schema_sql(
                "001_kernel.sql", "017_execution_cancel_attribution.sql"
            )
        )
        cur = conn.cursor()
        # 先插一条正常的（意图为假、归因为空 → 允许）
        cur.execute(
            "INSERT INTO executions (execution_id, task_id, idempotency_key, "
            "execution_mode, status, current_attempt_no, version) "
            "VALUES ('e1','t1','e1','task','PENDING',0,1)"
        )
        conn.commit()
        # 再把意图点亮却不给归因 → 必须被 CHECK 拦下
        with self.assertRaises(Exception):
            cur.execute(
                "UPDATE executions SET cancellation_requested = 1 WHERE execution_id='e1'"
            )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
