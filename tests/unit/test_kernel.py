"""Execution Kernel（阶段 3/4）：调度 / 恢复 / 唤醒 / 取消 / 幂等。

不接数据库、不接 Kafka —— 全部走 `ports.py` 的内存实现。
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import (
    ErrorInfo,
    ExecutionStatus,
    ExecutorType,
    FailureClass,
    RetryPolicy,
    SuspensionReason,
    TaskType,
)
from packages.execution_kernel import (
    CancellationService,
    ExecutionKernel,
    IdempotencyGuard,
    InMemoryCancelSignals,
    InMemoryExecutionRepository,
    InMemoryIdempotencyStore,
    InMemoryOutbox,
    KernelConfig,
    ManualClock,
    RecoveryController,
    RecordingEventPublisher,
    Scheduler,
    SchedulingPolicy,
    WakeupController,
    WorkerCapability,
    approval_satisfied,
)
from packages.execution_kernel.idempotency import UNKNOWN

from .helpers import make_task, new_run


def build_kernel(*, ttl_seconds: int = 30, retry: RetryPolicy | None = None):
    clock = ManualClock()
    repo = InMemoryExecutionRepository()
    outbox = InMemoryOutbox()
    signals = InMemoryCancelSignals()
    idem = InMemoryIdempotencyStore()
    kernel = ExecutionKernel(
        repository=repo,
        outbox=outbox,
        clock=clock,
        cancel_signals=signals,
        idempotency=idem,
        config=KernelConfig(
            default_lease_ttl=timedelta(seconds=ttl_seconds),
            default_retry_policy=retry or RetryPolicy(max_attempts=3),
        ),
    )
    scheduler = Scheduler(kernel)
    return kernel, scheduler, clock, repo, outbox, signals, idem


class TestSubmitAndDispatch(unittest.TestCase):
    def test_submit_and_claim(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        task = make_task(run_id=run_id, task_type=TaskType.TOOL_CALL)
        execution = kernel.submit(task)
        self.assertEqual(execution.status, ExecutionStatus.PENDING)

        dispatched = scheduler.dispatch(worker_id="w1", limit=1)
        self.assertEqual(len(dispatched), 1)
        _, attempt, lease = dispatched[0]
        self.assertEqual(attempt.attempt_no, 1)
        self.assertEqual(lease.worker_id, "w1")
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.RUNNING)

    def test_e19_one_task_one_execution(self):
        kernel, *_ = build_kernel()
        task = make_task()
        kernel.submit(task)
        with self.assertRaises(InvariantViolation):
            kernel.submit(task)

    def test_priority_ordering(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        low = kernel.submit(make_task(run_id=run_id, priority=0))
        high = kernel.submit(make_task(run_id=run_id, priority=10))
        selected = [e.execution_id for e in scheduler.select(limit=2)]
        self.assertEqual(selected[0], high.execution_id)
        self.assertEqual(selected[1], low.execution_id)

    def test_capability_filter(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        mcp_task = kernel.submit(
            make_task(run_id=run_id, executor_type=ExecutorType.MCP)
        )
        native_only = WorkerCapability(executors=frozenset({"native"}))
        self.assertEqual(scheduler.select(limit=5, capability=native_only), [])
        self.assertEqual(
            [e.execution_id for e in scheduler.select(limit=5)],
            [mcp_task.execution_id],
        )

    def test_tenant_fairness(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        a1 = kernel.submit(make_task(run_id=run_id, tenant_id="t-a", priority=10))
        a2 = kernel.submit(make_task(run_id=run_id, tenant_id="t-a", priority=9))
        b1 = kernel.submit(make_task(run_id=run_id, tenant_id="t-b", priority=8))
        order = [e.execution_id for e in scheduler.select(limit=3)]
        self.assertEqual(order[0], a1.execution_id)
        self.assertEqual(order[1], b1.execution_id)   # 穿插，避免单租户饿死其他租户
        self.assertEqual(order[2], a2.execution_id)


class TestCompleteAndRetry(unittest.TestCase):
    def test_complete_flow_and_events(self):
        kernel, scheduler, _, _, outbox, _, _ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        _, attempt, lease = scheduler.dispatch(worker_id="w1", limit=1)[0]
        kernel.complete(execution.execution_id, token=lease.fencing_token, result={"ok": True})
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.COMPLETED)

        types = [e.event_type for e in outbox.all()]
        self.assertIn("execution.created", types)
        self.assertIn("execution.running", types)
        self.assertIn("attempt.succeeded", types)
        self.assertIn("execution.completed", types)

    def test_e4_retry_goes_back_to_pending_then_redispatch(self):
        kernel, scheduler, _, _, _, _, _ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        _, attempt1, lease1 = scheduler.dispatch(worker_id="w1", limit=1)[0]

        will_retry = kernel.fail(
            execution.execution_id,
            token=lease1.fencing_token,
            error=ErrorInfo(code="timeout", message="t", failure_class=FailureClass.TRANSIENT),
        )
        self.assertTrue(will_retry)
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.PENDING)

        _, attempt2, lease2 = scheduler.dispatch(worker_id="w1", limit=1)[0]
        self.assertEqual(attempt2.attempt_no, 2)                  # 新 Attempt，不是状态回退
        self.assertGreater(lease2.fencing_token, lease1.fencing_token)

    def test_non_retryable_goes_to_failed(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        _, _, lease = scheduler.dispatch(worker_id="w1", limit=1)[0]
        will_retry = kernel.fail(
            execution.execution_id,
            token=lease.fencing_token,
            error=ErrorInfo(code="perm", message="bad", failure_class=FailureClass.PERMANENT),
        )
        self.assertFalse(will_retry)
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.FAILED)
        self.assertEqual(scheduler.select(limit=5), [])     # FAILED 不再进入 Runnable


class TestRecovery(unittest.TestCase):
    def test_e23_lease_expiry_then_recovery(self):
        kernel, scheduler, clock, repo, _, _, _ = build_kernel(ttl_seconds=30)
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        _, attempt1, lease1 = scheduler.dispatch(worker_id="w1", limit=1)[0]

        clock.advance(timedelta(seconds=31))
        controller = RecoveryController(kernel)
        result = controller.run_once()
        self.assertEqual(result["stale"], [execution.execution_id])
        self.assertEqual(result["recovered"], [execution.execution_id])
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.RUNNING)

        agg = kernel.aggregate(execution.execution_id)
        self.assertEqual(agg.attempts[-1].attempt_no, 2)
        self.assertGreater(agg.execution.lease.fencing_token, lease1.fencing_token)

        # 旧 Worker 拿着旧 token 回写 → 被拒
        from packages.agent_domain.errors import StaleWriteError

        with self.assertRaises(StaleWriteError):
            kernel.complete(execution.execution_id, token=lease1.fencing_token, result={})

    def test_scan_before_expiry_finds_nothing(self):
        kernel, scheduler, clock, *_ = build_kernel(ttl_seconds=30)
        run_id = new_run()
        kernel.submit(make_task(run_id=run_id))
        scheduler.dispatch(worker_id="w1", limit=1)
        clock.advance(timedelta(seconds=5))
        self.assertEqual(RecoveryController(kernel).scan(), [])


class TestSuspensionAndWakeup(unittest.TestCase):
    def test_suspend_then_wakeup_then_dispatch(self):
        kernel, scheduler, *_ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        scheduler.dispatch(worker_id="w1", limit=1)

        kernel.suspend(
            execution.execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approver": "risk-team"},
        )
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.SUSPENDED)
        self.assertIsNone(kernel.aggregate(execution.execution_id).execution.lease)

        # 审批未到：不唤醒
        wakeup = WakeupController(kernel)
        self.assertEqual(wakeup.run_once(approval_satisfied({})), [])
        # 审批到达：唤醒成 Runnable Task
        self.assertEqual(
            wakeup.run_once(approval_satisfied({"risk-team": "approved"})),
            [execution.execution_id],
        )
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.PENDING)

        _, attempt, _ = scheduler.dispatch(worker_id="w2", limit=1)[0]
        self.assertEqual(attempt.attempt_no, 2)


class TestCancellation(unittest.TestCase):
    def test_three_part_cancellation(self):
        kernel, scheduler, _, _, _, signals, _ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        _, _, lease = scheduler.dispatch(worker_id="w1", limit=1)[0]

        service = CancellationService(kernel)
        service.request(execution.execution_id, reason="stop", by="alice")
        # Durable Intent（PG）
        self.assertTrue(kernel.repository.get(execution.execution_id).cancellation_requested)
        # Fast Signal（Redis）
        self.assertTrue(signals.get(execution.execution_id))
        # Runtime Propagation
        token = service.token_for(execution.execution_id)
        self.assertTrue(token.is_cancelled())
        # 请求 ≠ 已取消
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.RUNNING)

        service.cancel(execution.execution_id, token=lease.fencing_token)
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.CANCELLED)
        self.assertFalse(signals.get(execution.execution_id))


class TestIdempotency(unittest.TestCase):
    def test_same_key_executes_once(self):
        _, _, _, _, _, _, store = build_kernel()
        guard = IdempotencyGuard(store)
        calls = []

        def external():
            calls.append(1)
            return {"charge_id": "ch_1"}

        r1, first = guard.execute("exec_1", external)
        r2, second = guard.execute("exec_1", external)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(r1, r2)
        self.assertEqual(len(calls), 1)     # 没有重复副作用

    def test_external_unknown_must_be_probed(self):
        _, _, _, _, _, _, store = build_kernel()
        guard = IdempotencyGuard(store)

        def boom():
            raise RuntimeError("network lost after request sent")

        with self.assertRaises(RuntimeError):
            guard.execute("exec_2", boom)
        self.assertEqual(store.get("exec_2"), {"status": UNKNOWN})

        with self.assertRaises(InvariantViolation):
            IdempotencyGuard.assert_not_blind_retry(FailureClass.EXTERNAL_UNKNOWN)

        self.assertIsNone(guard.resolve_unknown("exec_2", lambda: None))
        self.assertEqual(
            guard.resolve_unknown("exec_2", lambda: {"charge_id": "ch_2", "status": "paid"}),
            {"charge_id": "ch_2", "status": "paid"},
        )


class TestOutboxPublishing(unittest.TestCase):
    def test_x3_events_go_to_outbox_and_publisher(self):
        kernel, scheduler, _, _, outbox, _, _ = build_kernel()
        run_id = new_run()
        execution = kernel.submit(make_task(run_id=run_id))
        scheduler.dispatch(worker_id="w1", limit=1)

        publisher = RecordingEventPublisher(outbox)
        self.assertGreater(publisher.drain(), 0)
        self.assertEqual(outbox.pending(), [])          # 已发布
        publisher.drain()                                # 重复投递是允许的（至少一次）
        ids = [e.event_id for e in publisher.published]
        self.assertEqual(len(ids), len(set(ids)))        # 但 event_id 唯一，消费者可去重


if __name__ == "__main__":
    unittest.main()
