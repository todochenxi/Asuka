"""ExecutionAggregate：E-5、E-9、E-17、E-18、E-23、X-1、X-3、X-11。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from packages.agent_domain.errors import (
    IllegalTransition,
    LeaseRequired,
    StaleWriteError,
)
from packages.agent_domain.execution import (
    ErrorInfo,
    ExecutionStatus,
    FailureClass,
    RetryPolicy,
    SuspensionReason,
)

from .helpers import DEFAULT_TTL, make_aggregate


def _now(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


class TestClaimAndComplete(unittest.TestCase):
    def test_claim_then_succeed(self):
        agg = make_aggregate()
        attempt, lease = agg.claim(worker_id="w1", now=_now())
        self.assertEqual(agg.execution.status, ExecutionStatus.RUNNING)
        self.assertEqual(attempt.attempt_no, 1)
        self.assertEqual(lease.fencing_token, 1)
        self.assertEqual(lease.execution_id, agg.execution.execution_id)

        agg.succeed(token=lease.fencing_token, result={"answer": 42}, now=_now())
        self.assertEqual(agg.execution.status, ExecutionStatus.COMPLETED)
        self.assertIsNone(agg.execution.lease)
        # X-3：每次状态变更都有 Event
        self.assertTrue(any(e.payload.get("to") == "COMPLETED" for e in agg.events))

    def test_e5_only_one_running_attempt(self):
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        with self.assertRaises(IllegalTransition):
            agg.claim(worker_id="w2", now=_now())

    def test_x1_runtime_hands_off_at_task(self):
        """Runtime 交出 Task 后不再触碰生命周期：这里所有变更都经 aggregate（Kernel）。"""
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        with self.assertRaises(Exception):
            agg.execution.status = ExecutionStatus.COMPLETED   # 直接赋值被拒绝
        agg.succeed(token=lease.fencing_token, now=_now())
        self.assertEqual(agg.execution.status, ExecutionStatus.COMPLETED)


class TestLeaseAndRecovery(unittest.TestCase):
    def test_e22_stale_worker_cannot_write(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now(), ttl=timedelta(seconds=1))
        # 模拟：Lease 过期 → 被别的 Worker 接管（token 变成 2）
        agg.expire_lease(now=_now(2))
        new_attempt, new_lease = agg.recover(worker_id="w2", now=_now(2))
        self.assertEqual(new_attempt.attempt_no, 2)
        self.assertGreater(new_lease.fencing_token, lease.fencing_token)

        with self.assertRaises(StaleWriteError):
            agg.succeed(token=lease.fencing_token, now=_now(3))   # 旧 Worker 回写

    def test_e23_stale_must_go_through_recovery(self):
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now(), ttl=timedelta(seconds=1))
        agg.expire_lease(now=_now(2))
        self.assertEqual(agg.execution.status, ExecutionStatus.STALE)

        with self.assertRaises(IllegalTransition):
            agg.claim(worker_id="w1", now=_now(2))       # 不能直接续跑
        attempt, _ = agg.recover(worker_id="w3", now=_now(2))
        self.assertEqual(attempt.attempt_no, 2)
        self.assertEqual(agg.execution.status, ExecutionStatus.RUNNING)

    def test_heartbeat_requires_valid_token(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        agg.heartbeat(token=lease.fencing_token, now=_now(1))
        with self.assertRaises(StaleWriteError):
            agg.heartbeat(token=lease.fencing_token - 1, now=_now(1))

    def test_expire_without_lease_is_rejected(self):
        agg = make_aggregate()
        with self.assertRaises(LeaseRequired):
            agg.expire_lease(now=_now())


class TestRetry(unittest.TestCase):
    def test_e4_retry_opens_new_attempt(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        event, will_retry = agg.fail(
            token=lease.fencing_token,
            error=ErrorInfo(code="timeout", message="tool timeout", failure_class=FailureClass.TRANSIENT),
            retry_policy=RetryPolicy(max_attempts=3),
            now=_now(),
        )
        self.assertTrue(will_retry)
        self.assertEqual(agg.execution.status, ExecutionStatus.PENDING)

        attempt2, lease2 = agg.claim(worker_id="w1", now=_now(1))
        self.assertEqual(attempt2.attempt_no, 2)          # Retry = 新 Attempt，不是状态回退
        self.assertGreater(lease2.fencing_token, lease.fencing_token)

    def test_permanent_failure_goes_straight_to_failed(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        _, will_retry = agg.fail(
            token=lease.fencing_token,
            error=ErrorInfo(code="bad_args", message="invalid", failure_class=FailureClass.PERMANENT),
            retry_policy=RetryPolicy(max_attempts=3),
            now=_now(),
        )
        self.assertFalse(will_retry)
        self.assertEqual(agg.execution.status, ExecutionStatus.FAILED)


class TestSuspension(unittest.TestCase):
    def test_e9_e18_suspend_releases_lease_and_records_wait_condition(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        agg.suspend(
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approver": "risk-team"},
            now=_now(),
        )
        self.assertEqual(agg.execution.status, ExecutionStatus.SUSPENDED)
        self.assertIsNone(agg.execution.lease)                       # E-9
        self.assertEqual(
            agg.execution.suspension.reason, SuspensionReason.HUMAN_APPROVAL
        )
        self.assertEqual(agg.execution.suspension.wait_condition, {"approver": "risk-team"})

    def test_resume_after_wakeup(self):
        agg = make_aggregate()
        agg.claim(worker_id="w1", now=_now())
        agg.suspend(reason=SuspensionReason.TIMER, wait_condition={"at": "tomorrow"}, now=_now())
        attempt, _ = agg.resume(worker_id="w1", now=_now(1))
        self.assertEqual(agg.execution.status, ExecutionStatus.RUNNING)
        self.assertEqual(attempt.attempt_no, 2)


class TestCancellation(unittest.TestCase):
    def test_x11_harness_only_requests(self):
        agg = make_aggregate()
        _, lease = agg.claim(worker_id="w1", now=_now())
        agg.request_cancel(reason="stop", by="alice")
        self.assertTrue(agg.execution.cancellation_requested)
        self.assertEqual(agg.execution.status, ExecutionStatus.RUNNING)   # 请求 ≠ 已取消

        agg.cancel(token=lease.fencing_token)
        self.assertEqual(agg.execution.status, ExecutionStatus.CANCELLED)

    def test_cancelled_execution_cannot_be_claimed(self):
        agg = make_aggregate()
        agg.request_cancel(reason="stop", by="alice")
        with self.assertRaises(IllegalTransition):
            agg.claim(worker_id="w1", now=_now())


if __name__ == "__main__":
    unittest.main()
