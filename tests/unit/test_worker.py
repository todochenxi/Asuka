"""阶段 7（下）：Worker。

Worker 是"Lease / fencing / 取消 / 心跳"这几件事真正落地的地方，
所以测试重心是**边界情况**，不是 happy path：

    E-7   只有 RUNNING Attempt 能持有有效 Lease
    E-22  回写必须携带 fencing_token（僵尸 Worker 的写必须被拒）
    E-17  取消由 Kernel 裁决，Worker 只响应
    E-4   Retry = 新 Attempt（不是状态回退）
    X-11  Harness / Runtime 只能**请求**取消
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.execution import (
    ExecutionStatus,
    ExecutorType,
    FailureClass,
    Task,
    TaskType,
)
from packages.execution_kernel.cancellation import CancellationService
from packages.execution_kernel.inmemory import (
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import (
    ExecutorError,
    ExecutionContext,
    Worker,
    WorkerConfig,
    WorkerOutcome,
)

from .helpers import make_task


class ScriptedExecutor:
    """按脚本返回 / 抛错的测试执行器。"""

    def __init__(self, behavior) -> None:
        self.behavior = behavior
        self.calls: list[ExecutionContext] = []

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        self.calls.append(ctx)
        outcome = self.behavior(ctx) if callable(self.behavior) else self.behavior
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome or {"ok": True})


class WorkerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.repository = InMemoryExecutionRepository()
        self.kernel = ExecutionKernel(
            repository=self.repository,
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=self.clock,
        )
        self.scheduler = Scheduler(self.kernel)
        self.executor = ScriptedExecutor({"ok": True})

    def _worker(self, **config) -> Worker:
        cfg = WorkerConfig(worker_id="w1", **config)
        return Worker(
            kernel=self.kernel,
            scheduler=self.scheduler,
            executors={"native": self.executor},
            config=cfg,
        )

    def _submit(self, **task_kwargs) -> str:
        return self.kernel.submit(make_task(**task_kwargs)).execution_id

    def _status(self, execution_id: str) -> ExecutionStatus:
        return self.kernel.status_of(execution_id)


class HappyPathTest(WorkerTestBase):
    def test_run_once_completes(self) -> None:
        execution_id = self._submit()
        outcomes = self._worker().run_once()

        self.assertEqual(outcomes, {execution_id: WorkerOutcome.COMPLETED})
        self.assertEqual(self._status(execution_id), ExecutionStatus.COMPLETED)
        self.assertEqual(self.executor.calls[0].attempt_no, 1)

    def test_context_carries_idempotency_key_and_cancellation(self) -> None:
        execution_id = self._submit()
        self._worker().run_once()

        ctx = self.executor.calls[0]
        self.assertEqual(ctx.idempotency_key, execution_id)   # E-21
        self.assertFalse(ctx.cancellation.is_cancelled())


class FailureClassificationTest(WorkerTestBase):
    def test_transient_failure_goes_back_to_pending(self) -> None:
        """E-4：重试 = 回到 PENDING 等重新调度，不是把状态往回拨。"""
        self.executor = ScriptedExecutor(
            ExecutorError("TIMEOUT", "tool timed out", FailureClass.TRANSIENT)
        )
        execution_id = self._submit()

        outcomes = self._worker().run_once()
        self.assertEqual(outcomes[execution_id], WorkerOutcome.RETRYING)
        self.assertEqual(self._status(execution_id), ExecutionStatus.PENDING)

    def test_permanent_failure_is_terminal(self) -> None:
        self.executor = ScriptedExecutor(
            ExecutorError("BAD_ARGS", "invalid arguments", FailureClass.PERMANENT)
        )
        execution_id = self._submit()

        outcomes = self._worker().run_once()
        self.assertEqual(outcomes[execution_id], WorkerOutcome.FAILED)
        self.assertEqual(self._status(execution_id), ExecutionStatus.FAILED)

    def test_retry_creates_a_new_attempt(self) -> None:
        """重试后重新 Claim 必须是 attempt_no = 2。"""
        attempts: list[int] = []

        def flaky(ctx: ExecutionContext):
            attempts.append(ctx.attempt_no)
            if len(attempts) == 1:
                raise ExecutorError("TIMEOUT", "boom", FailureClass.TRANSIENT)
            return {"ok": True}

        self.executor = ScriptedExecutor(flaky)
        execution_id = self._submit()
        worker = self._worker()

        worker.run_once()
        worker.run_once()

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(self._status(execution_id), ExecutionStatus.COMPLETED)

    def test_unclassified_exception_is_permanent(self) -> None:
        """没分类的异常一律按 PERMANENT —— 不重试，避免把未知错误放大成风暴。"""
        self.executor = ScriptedExecutor(RuntimeError("unexpected"))
        execution_id = self._submit()

        self.assertEqual(
            self._worker().run_once()[execution_id], WorkerOutcome.FAILED
        )
        self.assertEqual(self._status(execution_id), ExecutionStatus.FAILED)

    def test_external_unknown_is_not_retried_blindly(self) -> None:
        """EXTERNAL_UNKNOWN 不可盲重试（外部副作用结果未知）。"""
        self.executor = ScriptedExecutor(
            ExecutorError("UNKNOWN", "no response", FailureClass.EXTERNAL_UNKNOWN)
        )
        execution_id = self._submit()

        outcomes = self._worker().run_once()
        self.assertNotEqual(outcomes[execution_id], WorkerOutcome.RETRYING)
        self.assertEqual(self._status(execution_id), ExecutionStatus.FAILED)

    def test_missing_executor_fails_permanently(self) -> None:
        worker = Worker(
            kernel=self.kernel, scheduler=self.scheduler, executors={},
            config=WorkerConfig(worker_id="w1"),
        )
        execution_id = self._submit(executor_type=ExecutorType.NATIVE)
        outcomes = worker.run_once()
        self.assertEqual(outcomes[execution_id], WorkerOutcome.FAILED)


class CancellationTest(WorkerTestBase):
    def test_cancel_requested_before_execution(self) -> None:
        execution_id = self._submit()
        CancellationService(self.kernel).request(execution_id, reason="stop", by="alice")

        # Scheduler 不会把它派发出去 —— 明知道要取消，就没必要再开工
        self.assertEqual(self._worker().run_once(), {})
        self.assertEqual(self.executor.calls, [])            # 根本没开始跑

        # 真正推进到终态的是 sweep：PENDING 没人会来响应取消，系统直接判死
        cancelled = CancellationService(self.kernel).sweep()
        self.assertEqual(cancelled, [execution_id])
        self.assertEqual(self._status(execution_id), ExecutionStatus.CANCELLED)

    def test_sweep_waits_for_worker_holding_a_live_lease(self) -> None:
        """RUNNING 且 Lease 有效 → 不能抢着判死，那是 Worker 的协作点。"""
        execution_id = self._submit()
        worker = self._worker()
        worker.dispatch_once()
        CancellationService(self.kernel).request(execution_id, reason="stop", by="alice")

        self.assertEqual(CancellationService(self.kernel).sweep(), [])
        self.assertEqual(self._status(execution_id), ExecutionStatus.RUNNING)

    def test_sweep_cancels_stale_execution(self) -> None:
        """STALE = Worker 已被判定失联，没人会来响应了。"""
        execution_id = self._submit()
        self._worker().dispatch_once()
        self.clock.advance(timedelta(seconds=31))
        self.kernel.mark_stale(execution_id)
        CancellationService(self.kernel).request(execution_id, reason="stop", by="alice")

        self.assertEqual(CancellationService(self.kernel).sweep(), [execution_id])
        self.assertEqual(self._status(execution_id), ExecutionStatus.CANCELLED)

    def test_cancel_during_execution_is_honored_after_safe_point(self) -> None:
        """取消是协作式的：Executor 跑完（安全点）后 Worker 才响应。

        执行**中途**不会被撕裂 —— 这是协作式取消换来的好处。
        """
        def slow(ctx: ExecutionContext):
            CancellationService(self.kernel).request(ctx.execution_id, reason="stop", by="alice")
            return {"ok": True}

        self.executor = ScriptedExecutor(slow)
        execution_id = self._submit()

        outcomes = self._worker().run_once()
        self.assertEqual(outcomes[execution_id], WorkerOutcome.CANCELLED)
        self.assertEqual(self._status(execution_id), ExecutionStatus.CANCELLED)

    def test_cancellation_token_is_visible_to_executor(self) -> None:
        """Token 是给 Executor 在**安全点**自己查的 —— Worker 不替它决定。"""
        seen: list[bool] = []

        def check(ctx: ExecutionContext):
            CancellationService(self.kernel).request(ctx.execution_id, reason="stop", by="alice")   # 外部发起取消
            seen.append(ctx.check_cancelled())                            # 安全点检查
            return {"ok": True}

        self.executor = ScriptedExecutor(check)
        execution_id = self._submit()
        worker = self._worker()
        lease = worker.dispatch_once()[0][2]

        outcome = worker.run_claimed(execution_id, lease)
        self.assertEqual(seen, [True])
        self.assertEqual(outcome, WorkerOutcome.CANCELLED)
        self.assertEqual(self._status(execution_id), ExecutionStatus.CANCELLED)


class HeartbeatTest(WorkerTestBase):
    def test_heartbeat_keeps_lease_alive(self) -> None:
        def long_running(ctx: ExecutionContext):
            self.clock.advance(timedelta(seconds=20))
            ctx.heartbeat()
            self.clock.advance(timedelta(seconds=20))
            return {"ok": True}

        self.executor = ScriptedExecutor(long_running)
        execution_id = self._submit()
        worker = self._worker(lease_ttl=timedelta(seconds=30),
                              heartbeat_interval=timedelta(seconds=10))

        self.assertEqual(worker.run_once()[execution_id], WorkerOutcome.COMPLETED)
        # 总耗时 40s > lease_ttl 30s，靠心跳续住了
        self.assertEqual(self._status(execution_id), ExecutionStatus.COMPLETED)

    def test_without_heartbeat_lease_expires(self) -> None:
        def long_running(ctx: ExecutionContext):
            self.clock.advance(timedelta(seconds=40))          # 不调 heartbeat
            return {"ok": True}

        self.executor = ScriptedExecutor(long_running)
        execution_id = self._submit()

        outcome = self._worker(lease_ttl=timedelta(seconds=30)).run_once()[execution_id]
        # Lease 过期后回写被拒 → Worker 认输，不能把结果写进去
        self.assertEqual(outcome, WorkerOutcome.LOST_LEASE)
        execution = self.repository.get(execution_id)
        assert execution is not None and execution.lease is not None
        self.assertTrue(execution.lease.is_expired(self.clock.now()))
        self.assertEqual(self._status(execution_id), ExecutionStatus.RUNNING)

    def test_config_rejects_heartbeat_slower_than_lease(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(lease_ttl=timedelta(seconds=30),
                         heartbeat_interval=timedelta(seconds=30))


class FencingTest(WorkerTestBase):
    def test_zombie_worker_write_is_rejected(self) -> None:
        """E-22：Lease 被别人接管后，老 Worker 拿着旧 token 回写必须失败。

        这是防止"僵尸 Worker 覆盖新 Worker 结果"的唯一机制。
        """
        execution_id = self._submit()
        worker = self._worker()

        dispatched = worker.dispatch_once()
        old_lease = dispatched[0][2]
        old_token = old_lease.fencing_token

        # Worker 失联 → Recovery 接管：STALE → 新 Attempt + 新 fencing_token
        self.clock.advance(timedelta(seconds=31))
        self.kernel.mark_stale(execution_id)
        self.kernel.recover(execution_id, worker_id="w2")

        # 老 Worker 醒过来，用旧 token 写回
        outcome = worker.run_claimed(execution_id, old_lease)
        self.assertEqual(outcome, WorkerOutcome.LOST_LEASE)
        self.assertEqual(old_token, old_lease.fencing_token)
        self.assertNotEqual(
            old_token, self.kernel.repository.get(execution_id).lease.fencing_token
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
