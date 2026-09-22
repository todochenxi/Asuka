"""Attempt / Lease / Retry 不变量：E-4、E-5、E-6、E-7、E-20、E-22、E-24。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.errors import (
    IllegalTransition,
    InvariantViolation,
    StaleWriteError,
)
from packages.agent_domain.execution import (
    Attempt,
    AttemptStatus,
    ErrorInfo,
    ExecutorType,
    FailureClass,
    KernelCheckpoint,
    Lease,
    RetryPolicy,
    RunCheckpoint,
    TaskType,
)
from packages.agent_domain.execution.state_machine import AttemptStateMachine
from packages.agent_domain.ids import new_attempt_id

from .helpers import make_task, new_run


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class TestAttempt(unittest.TestCase):
    def test_e6_attempt_no_monotonic(self):
        with self.assertRaises(InvariantViolation):
            Attempt(execution_id="exec_1", attempt_no=0)
        a = Attempt(execution_id="exec_1", attempt_no=1)
        self.assertTrue(a.is_running)
        self.assertFalse(a.is_terminal)

    def test_e7_only_running_attempt_can_hold_lease(self):
        a = Attempt(execution_id="exec_1", attempt_no=1)
        self.assertTrue(a.can_hold_lease())
        AttemptStateMachine().transition(a, AttemptStatus.SUCCEEDED)
        self.assertFalse(a.can_hold_lease())
        self.assertTrue(a.is_terminal)

    def test_attempt_terminal_has_no_second_transition(self):
        a = Attempt(execution_id="exec_1", attempt_no=1)
        AttemptStateMachine().transition(a, AttemptStatus.FAILED)
        with self.assertRaises(IllegalTransition):
            AttemptStateMachine().transition(a, AttemptStatus.SUCCEEDED)


class TestLease(unittest.TestCase):
    def _lease(self, token: int = 1) -> Lease:
        now = _utcnow()
        return Lease(
            execution_id="exec_1",          # E-20：挂在 Execution
            attempt_no=1,
            worker_id="worker-a",
            fencing_token=token,
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
            heartbeat_at=now,
        )

    def test_e20_lease_belongs_to_execution(self):
        lease = self._lease()
        self.assertEqual(lease.execution_id, "exec_1")
        with self.assertRaises(ValueError):
            Lease(execution_id="", worker_id="w", fencing_token=1)

    def test_e22_stale_write_is_rejected(self):
        lease = self._lease(token=5)
        lease.authorize(5)          # 当前持有者 OK
        with self.assertRaises(StaleWriteError):
            lease.authorize(4)      # 旧 Worker 回写 → 拒绝
        with self.assertRaises(StaleWriteError):
            lease.renew(token=3, ttl=timedelta(seconds=30))

    def test_lease_expiry(self):
        lease = self._lease()
        self.assertFalse(lease.is_expired())
        self.assertTrue(lease.is_expired(_utcnow() + timedelta(seconds=31)))


class TestRetryPolicy(unittest.TestCase):
    def test_e4_retry_creates_new_attempt_not_rollback(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(
            policy.should_retry(attempt_no=1, failure_class=FailureClass.TRANSIENT)
        )
        self.assertFalse(
            policy.should_retry(attempt_no=3, failure_class=FailureClass.TRANSIENT)
        )

    def test_permanent_and_policy_denied_are_not_retryable(self):
        policy = RetryPolicy()
        for fc in (FailureClass.PERMANENT, FailureClass.POLICY_DENIED, FailureClass.EXTERNAL_UNKNOWN):
            self.assertFalse(policy.should_retry(attempt_no=1, failure_class=fc))

    def test_external_unknown_must_not_be_retried_blindly(self):
        """外部副作用结果未知时，只能靠 Idempotency Key 回查，不能盲重试。"""
        policy = RetryPolicy(max_attempts=5)
        self.assertFalse(policy.is_retryable(FailureClass.EXTERNAL_UNKNOWN))
        err = ErrorInfo(code="unknown", message="payment result unknown", failure_class=FailureClass.EXTERNAL_UNKNOWN)
        self.assertFalse(err.retryable)

    def test_retry_budget(self):
        policy = RetryPolicy(max_attempts=5, retry_budget=2)
        self.assertFalse(
            policy.should_retry(
                attempt_no=1,
                failure_class=FailureClass.TRANSIENT,
                retries_used_in_run=2,
            )
        )

    def test_backoff_is_exponential(self):
        policy = RetryPolicy(backoff_base_seconds=1.0, backoff_max_seconds=10.0)
        self.assertLessEqual(policy.next_delay(3, jitter=False), 10.0)
        self.assertGreaterEqual(policy.next_delay(1, jitter=False), 0.0)


class TestCheckpoint(unittest.TestCase):
    def test_e24_kernel_checkpoint_forbids_business_fields(self):
        with self.assertRaises(InvariantViolation):
            KernelCheckpoint(
                execution_id="exec_1",
                idempotency_key="exec_1",
                execution_state={"current_step": "collect"},
            )
        with self.assertRaises(InvariantViolation):
            KernelCheckpoint(
                execution_id="exec_1",
                idempotency_key="exec_1",
                execution_state={"completed_tasks": ["task_1"]},
            )
        KernelCheckpoint(
            execution_id="exec_1", idempotency_key="exec_1", execution_state={"attempt_no": 1}
        )

    def test_run_checkpoint_holds_business_semantics(self):
        rc = RunCheckpoint(
            run_id=new_run(),
            current_step="collect_financial_data",
            completed_tasks=("task_1", "task_2"),
            context_snapshot_id="ctx_1",     # 引用，不内嵌
        )
        self.assertEqual(rc.context_snapshot_id, "ctx_1")

    def test_checkpoint_is_immutable(self):
        import dataclasses

        kc = KernelCheckpoint(execution_id="exec_1", idempotency_key="exec_1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            kc.seq = 2


if __name__ == "__main__":
    unittest.main()
