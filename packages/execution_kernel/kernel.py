"""Execution Kernel：可靠地把 Task 执行完。

这是 Runtime 交棒之后的唯一生命周期管理者（X-1）。

    Runtime  ──submit(task)──►  Kernel  ──►  Execution / Attempt / Lease / Checkpoint
                                        └──►  Event（写 Outbox）

本文件只做编排，具体能力分布在：
    scheduler.py           决定哪个 Task 现在该跑
    recovery_controller.py Lease 过期 → STALE → recover
    wakeup_controller.py   Wait Condition 满足 → Wake-up
    cancellation.py        取消的三段式
    idempotency.py         外部副作用去重
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from packages.agent_domain.errors import InvariantViolation, LeaseRequired
from packages.agent_domain.events.event import EXECUTION_CREATED, Event, new_event
from packages.agent_domain.execution import (
    Attempt,
    ErrorInfo,
    Execution,
    ExecutionAggregate,
    ExecutionStatus,
    KernelCheckpoint,
    Lease,
    RetryPolicy,
    SuspensionReason,
    Task,
)
from packages.agent_domain.execution.attempt import AttemptStatus

from .ports import (
    AttemptRepository,
    CancelSignalStore,
    Clock,
    ExecutionRepository,
    IdempotencyStore,
    LeaseIndex,
    OutboxStore,
    TaskRepository,
)


@dataclass
class KernelConfig:
    default_lease_ttl: timedelta = timedelta(seconds=30)
    default_retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    max_runnable_batch: int = 100


class ExecutionKernel:
    """Kernel 门面。所有 Execution 生命周期操作都必须经过它。"""

    def __init__(
        self,
        *,
        repository: ExecutionRepository,
        outbox: OutboxStore,
        clock: Clock,
        attempts: AttemptRepository | None = None,
        lease_index: LeaseIndex | None = None,
        cancel_signals: CancelSignalStore | None = None,
        idempotency: IdempotencyStore | None = None,
        tasks: TaskRepository | None = None,
        config: KernelConfig | None = None,
    ) -> None:
        self.repository = repository
        self.attempts = attempts
        self.lease_index = lease_index
        self.outbox = outbox
        self.clock = clock
        self.cancel_signals = cancel_signals
        self.idempotency = idempotency
        #: E-26：Task 的持久面。为 None 时 Task 只活在内存里 ——
        #: 这时"重启后把活捡起来"是做不到的，`task_of()` 会明确拒绝而不是编造。
        self.tasks = tasks
        self.config = config or KernelConfig()
        self._aggregates: dict[str, ExecutionAggregate] = {}
        self._tasks: dict[str, Task] = {}            # Scheduler 需要 priority / tenant / resource
        self.retries_used: dict[str, int] = {}       # run 级重试预算计数（简化：按 execution 记）

    # ------------------------------------------------------------ 内部
    def _emit(self, events: list[Event]) -> None:
        """X-3：状态变更与事件写入同一边界（真实实现里是同一个事务）。"""
        if events:
            self.outbox.append(events)

    def _persist(self, agg: ExecutionAggregate, *, expected_version: int | None = None) -> None:
        self.repository.save(agg.execution, expected_version=expected_version)
        self._persist_attempts(agg)
        self._sync_lease_index(agg)
        self._emit(agg.events)
        agg.events = []

    def _sync_lease_index(self, agg: ExecutionAggregate) -> None:
        """Redis 索引是 PG 的**派生视图**，每次状态写入后跟着刷。

        方向只能是 PG → Redis。反过来读 Redis 做裁决是错的。
        """
        if self.lease_index is None:
            return
        lease = agg.execution.lease
        if lease is not None and not agg.execution.is_terminal:
            self.lease_index.track(lease)
        else:
            self.lease_index.forget(agg.execution.execution_id)

    def _persist_attempts(self, agg: ExecutionAggregate) -> None:
        """E-4：Attempt 必须落库，否则重启后 Retry 预算 / 审计 / Replay 全丢。

        save 是 (execution_id, attempt_no) 上的幂等 upsert，所以整列表重写是安全的。
        """
        if self.attempts is None:
            return
        for attempt in agg.attempts:
            self.attempts.save(attempt)

    def aggregate(self, execution_id: str) -> ExecutionAggregate:
        """加载（或复用）聚合根。

        从 Repository 重建时**必须**把 Attempt 历史一起读回来 ——
        否则这个 Kernel 会以为自己是第 1 次尝试（E-4 / E-5 都会失守）。
        """
        agg = self._aggregates.get(execution_id)
        if agg is None:
            execution = self.repository.get(execution_id)
            if execution is None:
                raise KeyError(f"unknown execution: {execution_id}")
            agg = ExecutionAggregate(execution)
            if self.attempts is not None:
                agg.attempts = list(self.attempts.list_by_execution(execution_id))
            self._aggregates[execution_id] = agg
        return agg

    # ------------------------------------------------------------ 受理
    def submit(self, task: Task) -> Execution:
        """X-1：Runtime 产出 Task 后交棒；E-19：一个 Task 只有一个 Execution。

        E-26 / E-28：Task 在这里**落一次库**，且必须写在 Execution 之前 ——
        顺序不是随意的，013 那根外键（`executions.task_id → tasks.task_id`）
        会拒绝"先 Execution 后 Task"，而 X-3 要求两者在同一个事务里。

        先查 E-19 再写 Task：于是"重复交棒"报的是 E-19（说得清的那句），
        而不是一个谁也没法处理的唯一键冲突（PR-19）。
        """
        existing = self.repository.get_by_task(task.task_id)
        if existing is not None:
            raise InvariantViolation(
                f"E-19: task {task.task_id} already has execution {existing.execution_id}; "
                "re-run requires a NEW Task"
            )
        if self.tasks is not None:
            self.tasks.add(task)
        execution = Execution(task_id=task.task_id)
        self.repository.add(execution)
        agg = ExecutionAggregate(execution)
        self._aggregates[execution.execution_id] = agg
        self._tasks[execution.execution_id] = task
        self._emit(
            [
                new_event(
                    aggregate_type="execution",
                    aggregate_id=execution.execution_id,
                    event_type=EXECUTION_CREATED,
                    payload={"task_id": task.task_id, "status": execution.status.value},
                    aggregate_version=execution.version,
                )
            ]
        )
        return execution

    # ------------------------------------------------------------ 执行
    def claim(
        self,
        execution_id: str,
        *,
        worker_id: str,
        ttl: timedelta | None = None,
    ) -> tuple[Attempt, Lease]:
        agg = self.aggregate(execution_id)
        attempt, lease = agg.claim(
            worker_id=worker_id,
            now=self.clock.now(),
            ttl=ttl or self.config.default_lease_ttl,
        )
        self._persist(agg)
        return attempt, lease

    def heartbeat(self, execution_id: str, *, token: int, ttl: timedelta | None = None) -> None:
        agg = self.aggregate(execution_id)
        agg.heartbeat(token=token, now=self.clock.now(), ttl=ttl or self.config.default_lease_ttl)
        self._persist(agg)

    def complete(
        self,
        execution_id: str,
        *,
        token: int,
        result: Mapping[str, Any] | None = None,
        checkpoint: KernelCheckpoint | None = None,
    ) -> None:
        agg = self.aggregate(execution_id)
        agg.succeed(token=token, result=result, checkpoint=checkpoint, now=self.clock.now())
        self._persist(agg)

    def fail(
        self,
        execution_id: str,
        *,
        token: int,
        error: ErrorInfo,
        retry_policy: RetryPolicy | None = None,
    ) -> bool:
        """返回是否还会重试。重试 = 回到 PENDING 等 Scheduler 重新 Claim（E-4）。"""
        agg = self.aggregate(execution_id)
        used = self.retries_used.get(execution_id, 0)
        _, will_retry = agg.fail(
            token=token,
            error=error,
            retry_policy=retry_policy or self.config.default_retry_policy,
            retries_used_in_run=used,
            now=self.clock.now(),
        )
        if will_retry:
            self.retries_used[execution_id] = used + 1
        self._persist(agg)
        return will_retry

    def suspend(
        self,
        execution_id: str,
        *,
        reason: SuspensionReason,
        wait_condition: Mapping[str, Any],
    ) -> None:
        """X-11：Harness 只能**请求**；真正改状态的是 Kernel。"""
        agg = self.aggregate(execution_id)
        agg.suspend(reason=reason, wait_condition=wait_condition, now=self.clock.now())
        self._persist(agg)

    def wakeup(self, execution_id: str) -> None:
        """SUSPENDED → PENDING（Runnable Task），执行权仍归 Scheduler/Worker 的 Claim。"""
        agg = self.aggregate(execution_id)
        if agg.execution.status is not ExecutionStatus.SUSPENDED:
            raise InvariantViolation(
                f"wake-up only applies to SUSPENDED, got {agg.execution.status.value}"
            )
        with agg.execution.mutating():
            agg.execution.suspension = None
        from packages.agent_domain.execution.state_machine import ExecutionStateMachine

        agg.events.append(
            ExecutionStateMachine().transition(agg.execution, ExecutionStatus.PENDING)
        )
        self._persist(agg)

    def resume(self, execution_id: str, *, worker_id: str, ttl: timedelta | None = None) -> tuple[Attempt, Lease]:
        agg = self.aggregate(execution_id)
        attempt, lease = agg.resume(
            worker_id=worker_id, now=self.clock.now(), ttl=ttl or self.config.default_lease_ttl
        )
        self._persist(agg)
        return attempt, lease

    # ------------------------------------------------------------ 故障
    def mark_stale(self, execution_id: str) -> bool:
        """Lease 已过期 → STALE。由 Recovery Controller 调用。"""
        agg = self.aggregate(execution_id)
        if agg.execution.lease is None:
            raise LeaseRequired("E-7: no lease to expire")
        if not agg.execution.lease.is_expired(self.clock.now()):
            return False
        agg.expire_lease(now=self.clock.now())
        self._persist(agg)
        return True

    def recover(self, execution_id: str, *, worker_id: str = "recovery", ttl: timedelta | None = None) -> tuple[Attempt, Lease]:
        """E-23：STALE → 新 Attempt + 新 fencing_token → RUNNING。"""
        agg = self.aggregate(execution_id)
        attempt, lease = agg.recover(
            worker_id=worker_id, now=self.clock.now(), ttl=ttl or self.config.default_lease_ttl
        )
        self._persist(agg)
        return attempt, lease

    # ------------------------------------------------------------ 取消
    def request_cancel(self, execution_id: str, *, reason: str = "", by: str = "") -> None:
        """Durable Intent（repository）+ Fast Signal（cancel_signals）。

        M48 / 空洞 228：`reason` / `by` 透传给聚合根 —— B-8 要求
        一条取消能说出"谁叫停、为什么"，而只有发起请求的这一刻知道。
        """
        agg = self.aggregate(execution_id)
        agg.request_cancel(reason=reason, by=by)
        self._persist(agg)
        if self.cancel_signals is not None:
            self.cancel_signals.set(execution_id)

    def cancel(self, execution_id: str, *, token: int | None = None) -> None:
        agg = self.aggregate(execution_id)
        agg.cancel(token=token)
        self._persist(agg)
        if self.cancel_signals is not None:
            self.cancel_signals.clear(execution_id)

    # ------------------------------------------------------------ 查询
    def status_of(self, execution_id: str) -> ExecutionStatus:
        return self.aggregate(execution_id).execution.status

    def task_of(self, execution_id: str) -> Task:
        """Scheduler / Worker 需要 Task 上的 priority / tenant / resource / payload。"""
        return self._task_for(execution_id, self.aggregate(execution_id).execution)

    def tasks_for(self, execution_ids: Sequence[str]) -> Mapping[str, Task]:
        """批量版：一次把一批 Execution 的 Task 取回来（Scheduler 热路径）。

        先命中进程内缓存，缺的**一次**问仓储 —— 于是 N 个候选不是 N 次往返。
        """
        out: dict[str, Task] = {}
        wanted: dict[str, str] = {}                  # execution_id -> task_id
        for execution_id in execution_ids:
            cached = self._tasks.get(execution_id)
            if cached is not None:
                out[execution_id] = cached
                continue
            agg = self._aggregates.get(execution_id)
            execution = agg.execution if agg is not None else self.repository.get(execution_id)
            if execution is None:
                raise KeyError(f"unknown execution: {execution_id}")
            wanted[execution_id] = execution.task_id

        if not wanted:
            return out
        if self.tasks is None:
            raise self._no_task_store(next(iter(wanted)))

        found = self.tasks.get_many(sorted(set(wanted.values())))
        for execution_id, task_id in wanted.items():
            task = found.get(task_id)
            if task is None:
                raise self._missing_task(execution_id, task_id)
            self._tasks[execution_id] = task
            out[execution_id] = task
        return out

    def _task_for(self, execution_id: str, execution: Execution) -> Task:
        cached = self._tasks.get(execution_id)
        if cached is not None:
            return cached
        if self.tasks is None:
            raise self._no_task_store(execution_id)
        task = self.tasks.get(execution.task_id)
        if task is None:
            raise self._missing_task(execution_id, execution.task_id)
        self._tasks[execution_id] = task
        return task

    # ------------------------------------------------------- E-26 / E-27 报错
    @staticmethod
    def _no_task_store(execution_id: str) -> InvariantViolation:
        """进程重启之后内存字典是空的，而这个 Kernel 又没接 Task 仓储。"""
        return InvariantViolation(
            f"E-26: no Task for execution {execution_id} and this kernel has no "
            "task store, so it cannot know what this execution was asked to do; "
            "refusing to fabricate one — priority / tenant / resource / payload "
            "are correctness inputs, not performance hints"
        )

    @staticmethod
    def _missing_task(execution_id: str, task_id: str) -> InvariantViolation:
        """库里有 Execution，却没有它指的那条 Task（013 的外键本该挡住它）。"""
        return InvariantViolation(
            f"E-27: execution {execution_id} references task {task_id}, but no "
            "such task row exists; refusing to fabricate one — a default Task "
            "would silently mis-schedule it (wrong worker, waived tenant quota)"
        )

    def attempts_of(self, execution_id: str) -> list[AttemptStatus]:
        return [a.status for a in self.aggregate(execution_id).attempts]
