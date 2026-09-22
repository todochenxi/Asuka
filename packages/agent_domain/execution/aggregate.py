"""ExecutionAggregate：Kernel 侧的强一致边界（阶段 1 为纯内存实现）。

它把 Execution / Attempt / Lease 的一致性收敛在一个地方，使以下不变量**可被代码强制**：

    E-5   同一 Execution 同一时刻最多一个 RUNNING Attempt
    E-7   只有 RUNNING Attempt 能持有有效 Lease
    E-9   进入 SUSPENDED 必须释放 Lease
    E-17  Cancellation 生命周期管理属 Kernel；Harness 只能发起请求
    E-19  Task : Execution = 1 : 1
    E-22  写回必须携带 fencing_token
    E-23  STALE 必须经 Recovery（新 Attempt + 新 fencing_token）
    X-1   Runtime 产出 Task 即交棒，不再触碰生命周期

阶段 3 的 Execution Kernel 会基于同样的接口换成 PG / Redis 实现；
阶段 1 这里只保证"领域语义正确"，不碰任何基础设施。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..errors import (
    IllegalTransition,
    InvariantViolation,
    LeaseRequired,
    StaleWriteError,
)
from ..events.event import (
    EXECUTION_CANCEL_REQUESTED,
    EXECUTION_RESUMED,
    LEASE_ACQUIRED,
    LEASE_EXPIRED,
    Event,
    new_event,
)
from ..ids import new_attempt_id
from .attempt import Attempt, AttemptStatus
from .checkpoint import KernelCheckpoint
from .execution import Execution, ExecutionStatus, Suspension, SuspensionReason
from .lease import Lease
from .retry import ErrorInfo, FailureClass, RetryPolicy
from .state_machine import AttemptStateMachine, ExecutionStateMachine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExecutionAggregate:
    execution: Execution
    attempts: list[Attempt] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    _fencing_counter: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution, Execution):
            raise InvariantViolation("ExecutionAggregate requires an Execution")

    # ------------------------------------------------------------ 内部工具
    def _emit(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def _next_token(self) -> int:
        self._fencing_counter += 1
        return self._fencing_counter

    @property
    def running_attempt(self) -> Attempt | None:
        for a in self.attempts:
            if a.is_running:
                return a
        return None

    def _require_lease(self, token: int | None = None, now: datetime | None = None) -> Lease:
        """now 由调用方注入（Kernel 的 Clock），领域层不自己取系统时间。"""
        lease = self.execution.lease
        if lease is None:
            raise LeaseRequired("E-7: execution has no active lease")
        if lease.is_expired(now or _utcnow()):
            raise LeaseRequired("E-22: lease already expired")
        if token is not None:
            lease.authorize(token)     # E-22
        return lease

    # ------------------------------------------------------------ 生命周期
    def claim(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(seconds=30),
    ) -> tuple[Attempt, Lease]:
        """Worker 领取 Execution：PENDING → RUNNING，开 Attempt #n，分配 Lease + fencing_token。"""
        now = now or _utcnow()
        self.execution.assert_not_terminal()

        if self.execution.status is ExecutionStatus.RUNNING:
            raise IllegalTransition("E-5: execution already has a running attempt")
        if self.execution.status not in (ExecutionStatus.PENDING, ExecutionStatus.STALE):
            raise IllegalTransition(
                f"E-1: cannot claim execution in status {self.execution.status.value}"
            )
        if self.execution.status is ExecutionStatus.STALE:
            # E-23：STALE 不允许直接续跑，必须走 recover()
            raise IllegalTransition(
                "E-23: stale execution must be recovered via recover(), not claimed directly"
            )
        if self.execution.cancellation_requested:
            raise IllegalTransition("E-17: execution has a pending cancellation request")

        attempt_no = self.execution.current_attempt_no + 1
        attempt = Attempt(
            attempt_id=new_attempt_id(),
            execution_id=self.execution.execution_id,
            attempt_no=attempt_no,             # E-6：单调递增
            status=AttemptStatus.RUNNING,
            started_at=now,
        )
        token = self._next_token()
        lease = Lease(
            execution_id=self.execution.execution_id,   # E-20
            attempt_no=attempt_no,
            worker_id=worker_id,
            fencing_token=token,                        # E-22
            acquired_at=now,
            expires_at=now + ttl,
            heartbeat_at=now,
        )

        with self.execution.mutating():
            self.attempts.append(attempt)
            self.execution.current_attempt_no = attempt_no
            self.execution.lease = lease
            self.execution.status = ExecutionStatus.RUNNING
            self.execution.version += 1
        self.execution.validate()

        self._emit(
            new_event(
                aggregate_type="execution",
                aggregate_id=self.execution.execution_id,
                event_type=EXECUTION_RESUMED if attempt_no > 1 else "execution.running",
                payload={
                    "worker_id": worker_id,
                    "attempt_no": attempt_no,
                    "fencing_token": token,
                    "version": self.execution.version,
                },
                aggregate_version=self.execution.version,
            )
        )
        self._emit(
            new_event(
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                event_type=LEASE_ACQUIRED,
                payload={
                    "execution_id": self.execution.execution_id,
                    "attempt_no": attempt_no,
                    "worker_id": worker_id,
                    "fencing_token": token,
                },
            )
        )
        return attempt, lease

    def heartbeat(self, *, token: int, now: datetime | None = None, ttl: timedelta = timedelta(seconds=30)) -> None:
        lease = self._require_lease(token, now)
        lease.renew(now=now, ttl=ttl, token=token)

    def succeed(
        self,
        *,
        token: int,
        result: Mapping[str, Any] | None = None,
        checkpoint: KernelCheckpoint | None = None,
        now: datetime | None = None,
    ) -> Event:
        now = now or _utcnow()
        self._require_lease(token, now)
        attempt = self.running_attempt
        if attempt is None:
            raise InvariantViolation("E-5: no running attempt to succeed")

        attempt.result = dict(result or {})
        attempt.finished_at = now
        attempt.checkpoint_id = checkpoint.checkpoint_id if checkpoint else None
        self._emit(AttemptStateMachine().transition(attempt, AttemptStatus.SUCCEEDED))
        with self.execution.mutating():
            self.execution.lease = None        # 成功即释放
        return self._emit(
            ExecutionStateMachine().transition(
                self.execution, ExecutionStatus.COMPLETED
            )
        )

    def fail(
        self,
        *,
        token: int,
        error: ErrorInfo,
        retry_policy: RetryPolicy | None = None,
        retries_used_in_run: int = 0,
        now: datetime | None = None,
    ) -> tuple[Event, bool]:
        """Attempt 失败。返回 (event, will_retry)。

        will_retry 只表示"策略允许重试"，真正的重试由 Scheduler 重新调度触发（新 Attempt）。
        """
        now = now or _utcnow()
        self._require_lease(token, now)
        attempt = self.running_attempt
        if attempt is None:
            raise InvariantViolation("E-5: no running attempt to fail")

        attempt.error = error
        attempt.finished_at = now
        status = AttemptStatus.TIMEOUT if error.failure_class is FailureClass.TRANSIENT and error.code == "timeout" else AttemptStatus.FAILED
        self._emit(AttemptStateMachine().transition(attempt, status))

        policy = retry_policy or RetryPolicy()
        will_retry = policy.should_retry(
            attempt_no=attempt.attempt_no,
            failure_class=error.failure_class,
            retries_used_in_run=retries_used_in_run,
        )

        with self.execution.mutating():
            self.execution.lease = None
        if will_retry:
            # 回到可调度状态：交给 Scheduler 重新 Claim（新 Attempt）
            event = self._emit(
                ExecutionStateMachine().transition(self.execution, ExecutionStatus.PENDING)
            )
        else:
            event = self._emit(
                ExecutionStateMachine().transition(self.execution, ExecutionStatus.FAILED)
            )
        return event, will_retry

    def expire_lease(self, *, now: datetime | None = None) -> Event:
        """Lease 过期：RUNNING → STALE（不是简单回到 QUEUED）。"""
        now = now or _utcnow()
        lease = self.execution.lease
        if lease is None:
            raise LeaseRequired("E-7: no lease to expire")
        if not lease.is_expired(now):
            raise IllegalTransition("lease has not expired yet")

        self._emit(
            new_event(
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                event_type=LEASE_EXPIRED,
                payload={"execution_id": self.execution.execution_id, "worker_id": lease.worker_id},
            )
        )
        with self.execution.mutating():
            self.execution.lease = None
        return self._emit(
            ExecutionStateMachine().transition(self.execution, ExecutionStatus.STALE)
        )

    def recover(
        self,
        *,
        worker_id: str | None = None,
        now: datetime | None = None,
        ttl: timedelta = timedelta(seconds=30),
    ) -> tuple[Attempt, Lease]:
        """E-23：STALE → 新 Attempt + 新 fencing_token → RUNNING。

        唯一允许把 STALE 拉回 RUNNING 的路径。
        """
        now = now or _utcnow()
        if self.execution.status is not ExecutionStatus.STALE:
            raise IllegalTransition(
                f"E-23: recover() only applies to STALE execution, got {self.execution.status.value}"
            )
        attempt = self.running_attempt
        if attempt is not None:
            # 旧 Worker 的 Attempt 必须被标记（不再允许它写回）
            self._emit(AttemptStateMachine().transition(attempt, AttemptStatus.CANCELLED))

        attempt_no = self.execution.current_attempt_no + 1
        new_attempt = Attempt(
            attempt_id=new_attempt_id(),
            execution_id=self.execution.execution_id,
            attempt_no=attempt_no,
            status=AttemptStatus.RUNNING,
            started_at=now,
        )
        token = self._next_token()
        lease = Lease(
            execution_id=self.execution.execution_id,
            attempt_no=attempt_no,
            worker_id=worker_id or "recovery",
            fencing_token=token,
            acquired_at=now,
            expires_at=now + ttl,
            heartbeat_at=now,
        )
        with self.execution.mutating():
            self.attempts.append(new_attempt)
            self.execution.current_attempt_no = attempt_no
            self.execution.lease = lease
            self.execution.status = ExecutionStatus.RUNNING
            self.execution.version += 1
        self.execution.validate()
        self._emit(
            new_event(
                aggregate_type="execution",
                aggregate_id=self.execution.execution_id,
                event_type=EXECUTION_RESUMED,
                payload={
                    "reason": "recovery",
                    "attempt_no": attempt_no,
                    "fencing_token": token,
                    "version": self.execution.version,
                },
                aggregate_version=self.execution.version,
            )
        )
        return new_attempt, lease

    # ------------------------------------------------------------ 挂起
    def suspend(
        self,
        *,
        reason: SuspensionReason,
        wait_condition: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Event:
        """E-9 / E-18：进入 SUSPENDED 必须释放 Lease 并记录 wait condition。"""
        now = now or _utcnow()
        if self.execution.status is not ExecutionStatus.RUNNING:
            raise IllegalTransition(
                f"E-1: cannot suspend execution in status {self.execution.status.value}"
            )
        attempt = self.running_attempt
        if attempt is not None:
            self._emit(AttemptStateMachine().transition(attempt, AttemptStatus.CANCELLED))
        with self.execution.mutating():
            self.execution.lease = None        # E-9
        return self._emit(
            ExecutionStateMachine().transition(
                self.execution,
                ExecutionStatus.SUSPENDED,
                suspension=Suspension(reason=reason, wait_condition=wait_condition, suspended_at=now),
            )
        )

    def resume(self, *, worker_id: str, now: datetime | None = None, ttl: timedelta = timedelta(seconds=30)) -> tuple[Attempt, Lease]:
        """Wake-up 之后：SUSPENDED → RUNNING（新 Attempt）。"""
        if self.execution.status is not ExecutionStatus.SUSPENDED:
            raise IllegalTransition("wake-up only applies to SUSPENDED execution")
        with self.execution.mutating():
            self.execution.suspension = None
        # Wake-up：SUSPENDED → PENDING（Runnable Task），再由 Scheduler/Worker Claim
        self._emit(
            ExecutionStateMachine().transition(self.execution, ExecutionStatus.PENDING)
        )
        return self.claim(worker_id=worker_id, now=now, ttl=ttl)

    # ------------------------------------------------------------ 取消
    def request_cancel(self, *, reason: str = "", by: str = "") -> Event:
        """X-11：Harness / Runtime 只能**请求**取消，真正的生命周期归 Kernel。

        ------------------------------------------------------------------
        M48 / 空洞 228：B-8 / A-8 —— 请求必须带归因

        在此之前这一层只有 `cancellation_requested` 一个布尔位，
        于是"谁叫停的、为什么"在这条链路上**无处可写**：
        Run 级（`run_cancellations`）有、子 Run 级（D-14）有，
        唯独最贴近"真正干活那一刀"的 Execution 没有。

        现在 `reason` / `by` 是**必填**，空串直接抛：
        一条查不到是谁、说不出为什么的取消，等于取消没有发生过。

        ------------------------------------------------------------------
        为什么归因写在**请求**上，不写在**判死**上

        判死（`cancel()`）是 Kernel 的动作，它可能由 Sweeper 发起 ——
        那时"为什么"已经不是调用方那一句了。
        而"谁要求停它、因为什么"是**请求那一刻**的事实，
        只有请求的人知道。所以归因跟着请求走。

        判死之后归因**不**被抹掉（见 `test_request_is_not_a_verdict`）：
        那正是"它为什么会被判死"的答案。
        """
        if not reason:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty reason; "
                "a cancellation nobody can explain is unauditable"
            )
        if not by:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty 'by'; "
                "an anonymous cancellation cannot be attributed (A-8)"
            )
        if self.execution.is_terminal:
            raise IllegalTransition("E-2: execution already terminal")
        with self.execution.mutating():
            self.execution.cancellation_requested = True
            self.execution.cancellation_reason = reason
            self.execution.cancellation_by = by
            self.execution.version += 1
        return self._emit(
            new_event(
                aggregate_type="execution",
                aggregate_id=self.execution.execution_id,
                event_type=EXECUTION_CANCEL_REQUESTED,
                payload={
                    "version": self.execution.version,
                    # 审计不看 PG 也要能回答"谁、为什么"（X-15 同款）。
                    "reason": reason,
                    "by": by,
                },
                aggregate_version=self.execution.version,
            )
        )

    def cancel(self, *, token: int | None = None) -> Event:
        """Kernel 执行真正的取消。"""
        if token is not None and self.execution.lease is not None:
            try:
                self.execution.lease.authorize(token)
            except StaleWriteError:
                pass  # 过期 Worker 也能被系统级取消，但不允许它修改业务状态
        attempt = self.running_attempt
        if attempt is not None:
            self._emit(AttemptStateMachine().transition(attempt, AttemptStatus.CANCELLED))
        with self.execution.mutating():
            self.execution.lease = None
        return self._emit(
            ExecutionStateMachine().transition(self.execution, ExecutionStatus.CANCELLED)
        )
