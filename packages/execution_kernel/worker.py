"""Worker：真正跑 Task 的那个进程。

    Scheduler.dispatch()  → Atomic Claim（Lease + fencing_token + 新 Attempt）
        ↓
    Worker.run_claimed()  → Executor.execute(task, ctx)
        ↓
    Kernel.complete() / fail()      ← 回写必须带 fencing_token（E-22）

Worker 必须做对的五件事：

1. **回写带 fencing_token**
   Lease 过期后 Kernel 会把 Execution 交给别人；僵尸 Worker 的回写必须被拒（E-22）。

2. **取消是协作式的**
   CancellationToken 只在 Worker **主动检查**时才生效。不检查就取消不掉 ——
   这是代价，换来的是"不会在任意指令处被撕裂"（安全点语义）。

3. **心跳续租**
   长任务必须在执行过程中续 Lease，否则会被 Recovery 判成 STALE。

4. **失败要分类（FailureClass）**
   Kernel 只认 Failure Class，不认异常类型。Worker 负责把异常翻译成
   TRANSIENT / RESOURCE / PERMANENT / EXTERNAL_UNKNOWN / POLICY_DENIED。
   **EXTERNAL_UNKNOWN 不可盲重试**（外部副作用结果未知）—— 那要靠
   `idempotency.IdempotencyGuard` 回查。

5. **丢失 Lease 就停手**
   心跳失败 / 回写被拒 = 我可能已经不是持有者了。此时**不能**继续写结果。

E-7  只有 RUNNING Attempt 能持有有效 Lease
E-22 写回必须携带 fencing_token
E-17 Cancellation 生命周期管理属 Kernel，Worker 只响应
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from packages.agent_domain.errors import LeaseRequired, StaleWriteError
from packages.agent_domain.execution import (
    Attempt,
    ErrorInfo,
    Execution,
    FailureClass,
    Lease,
    Task,
)
from packages.agent_domain.execution.retry import RetryPolicy

from .cancellation import CancellationService, CancellationToken
from .kernel import ExecutionKernel
from .scheduler import Scheduler, WorkerCapability


class WorkerOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"        # 可重试失败 → 回到 PENDING 等重新调度
    CANCELLED = "cancelled"
    LOST_LEASE = "lost_lease"    # 心跳失败 / 回写被拒：我不再是持有者


class ExecutorError(Exception):
    """Executor 抛出的、可被 Kernel 理解的失败。

    `failure_class` 决定这次要不要重试 —— 它是 Kernel 唯一的重试判据。
    """

    def __init__(
        self,
        code: str,
        message: str,
        failure_class: FailureClass = FailureClass.PERMANENT,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.failure_class = failure_class
        self.retryable = retryable


@dataclass(frozen=True)
class ExecutionContext:
    """交给 Executor 的运行期上下文。

    Worker 把"取消"和"心跳"作为**能力**交给 Executor，
    Executor 自己决定在哪些安全点检查它们。
    """

    execution_id: str
    attempt_no: int
    task: Task
    idempotency_key: str
    cancellation: CancellationToken
    heartbeat: Callable[[], None]

    def check_cancelled(self) -> bool:
        return self.cancellation.is_cancelled()


class Executor(Protocol):
    """真正的执行器（LLM / Tool / MCP / HTTP / 子 Agent…）。

    Executor 不认识 Kernel，也不认识数据库 —— 它只拿到 `ExecutionContext`。
    """

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]: ...


@dataclass
class WorkerConfig:
    worker_id: str = "worker-1"
    lease_ttl: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=10)   # 必须 < lease_ttl
    poll_limit: int = 1

    def __post_init__(self) -> None:
        if self.heartbeat_interval >= self.lease_ttl:
            raise ValueError(
                "heartbeat_interval must be shorter than lease_ttl, "
                "otherwise the lease expires between two heartbeats"
            )


@dataclass
class Worker:
    kernel: ExecutionKernel
    scheduler: Scheduler
    executors: Mapping[str, Executor]
    config: WorkerConfig = field(default_factory=WorkerConfig)
    capability: WorkerCapability | None = None
    last_outcomes: dict[str, WorkerOutcome] = field(default_factory=dict)
    _heartbeats: dict[str, object] = field(default_factory=dict)

    # ------------------------------------------------------------------ 派发
    def dispatch_once(self, limit: int = 1) -> list[tuple[Execution, Attempt, Lease]]:
        """向 Scheduler 要活 —— Claim 是原子的，两个 Worker 不会拿到同一个 Execution。"""
        return list(
            self.scheduler.dispatch(
                worker_id=self.config.worker_id,
                capability=self.capability,
                limit=limit or self.config.poll_limit,
                ttl=self.config.lease_ttl,
            )
        )

    # ------------------------------------------------------------------ 执行
    def run_claimed(self, execution_id: str, lease: Lease) -> WorkerOutcome:
        """跑一个已经 Claim 到的 Execution。"""
        if self._is_cancelled(execution_id):
            self.kernel.cancel(execution_id, token=lease.fencing_token)
            self.last_outcomes[execution_id] = WorkerOutcome.CANCELLED
            return WorkerOutcome.CANCELLED

        task = self.kernel.task_of(execution_id)
        execution = self.kernel.repository.get(execution_id)
        attempt_no = execution.current_attempt_no if execution else 1

        ctx = ExecutionContext(
            execution_id=execution_id,
            attempt_no=attempt_no,
            task=task,
            idempotency_key=execution_id,          # E-21：幂等键 = execution_id
            cancellation=CancellationService(self.kernel).token_for(execution_id),
            heartbeat=lambda: self.heartbeat(execution_id, lease.fencing_token),
        )

        try:
            result = self._executor_for(task).execute(task, ctx)
        except ExecutorError as err:
            return self._on_failure(execution_id, lease, task, err)
        except (StaleWriteError, LeaseRequired):
            # E-22：我的 Lease 已经过期 / 被别人接管了。
            # 此时**不能**再写任何东西 —— 结果交给接管方（Recovery 会开新 Attempt）。
            self.last_outcomes[execution_id] = WorkerOutcome.LOST_LEASE
            return WorkerOutcome.LOST_LEASE
        except Exception as err:                    # noqa: BLE001 未分类异常一律按 PERMANENT
            return self._on_failure(
                execution_id,
                lease,
                task,
                ExecutorError(type(err).__name__, str(err), FailureClass.PERMANENT),
            )

        # 执行完了也要再看一眼取消：Executor 可能跑很久
        if self._is_cancelled(execution_id):
            self.kernel.cancel(execution_id, token=lease.fencing_token)
            self.last_outcomes[execution_id] = WorkerOutcome.CANCELLED
            return WorkerOutcome.CANCELLED

        try:
            self.kernel.complete(execution_id, token=lease.fencing_token, result=dict(result))
        except (StaleWriteError, LeaseRequired):
            # 我的 Lease 已经不是最新的了：结果**不能**写进去
            self.last_outcomes[execution_id] = WorkerOutcome.LOST_LEASE
            return WorkerOutcome.LOST_LEASE

        self.last_outcomes[execution_id] = WorkerOutcome.COMPLETED
        return WorkerOutcome.COMPLETED

    def _on_failure(
        self, execution_id: str, lease: Lease, task: Task, err: ExecutorError
    ) -> WorkerOutcome:
        """失败 → 翻译成 ErrorInfo（带 FailureClass）→ 交回 Kernel 决定是否重试。"""
        error = ErrorInfo(code=err.code, message=err.message, failure_class=err.failure_class)
        try:
            will_retry = self.kernel.fail(
                execution_id,
                token=lease.fencing_token,
                error=error,
                retry_policy=getattr(task, "retry_policy", None) or RetryPolicy(),
            )
        except (StaleWriteError, LeaseRequired):
            # E-22：我的 Lease 已经过期 / 被别人接管了。
            # 此时**不能**再写任何东西 —— 结果交给接管方（Recovery 会开新 Attempt）。
            self.last_outcomes[execution_id] = WorkerOutcome.LOST_LEASE
            return WorkerOutcome.LOST_LEASE

        outcome = WorkerOutcome.RETRYING if will_retry else WorkerOutcome.FAILED
        self.last_outcomes[execution_id] = outcome
        return outcome

    def _executor_for(self, task: Task) -> Executor:
        key = task.executor_type.value
        if key not in self.executors:
            raise ExecutorError(
                "EXECUTOR_NOT_FOUND",
                f"no executor registered for {key}",
                FailureClass.PERMANENT,
            )
        return self.executors[key]

    def _is_cancelled(self, execution_id: str) -> bool:
        return CancellationService(self.kernel).is_requested(execution_id)

    # ------------------------------------------------------------------ 续租
    def heartbeat(self, execution_id: str, token: int) -> None:
        """续租。抛 StaleWriteError / LeaseRequired 即"我不再是持有者"，由调用方决定怎么办。"""
        self.kernel.heartbeat(execution_id, token=token, ttl=self.config.lease_ttl)
        self._heartbeats[execution_id] = self.kernel.clock.now()

    def run_once(self, limit: int = 1) -> dict[str, WorkerOutcome]:
        """一个周期：派发 → 逐个跑。"""
        outcomes: dict[str, WorkerOutcome] = {}
        for _execution, _attempt, lease in self.dispatch_once(limit=limit):
            outcomes[lease.execution_id] = self.run_claimed(lease.execution_id, lease)
        return outcomes
