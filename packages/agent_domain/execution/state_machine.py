"""StateMachine：Kernel 的通用机制。

E-15  StateMachine 是通用机制；第一版实现 ExecutionStateMachine + AttemptStateMachine
E-1   状态变更必须走 transition()，禁止直接赋值
E-13  更新带 version 校验（Optimistic Lock）
X-3   每次状态变更必须产生一个 Event

机制 vs 策略（重要）：

    机制（本文件）  能不能转、转换是否原子、是否产生 Event、version 是否递增  → Kernel
    策略（调用方）  什么时候发起转换、转换到哪                                → Runtime / Harness / Worker
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Mapping, TypeVar

from ..errors import IllegalTransition, TerminalStateError
from ..events.event import (
    ATTEMPT_CANCELLED,
    ATTEMPT_FAILED,
    ATTEMPT_STARTED,
    ATTEMPT_SUCCEEDED,
    ATTEMPT_TIMEOUT,
    EXECUTION_CANCELLED,
    EXECUTION_COMPLETED,
    EXECUTION_FAILED,
    EXECUTION_RESUMED,
    EXECUTION_RUNNING,
    EXECUTION_STALE,
    EXECUTION_SUSPENDED,
    Event,
    new_event,
)
from .attempt import Attempt, AttemptStatus
from .execution import Execution, ExecutionStatus, TERMINAL_EXECUTION_STATUSES

S = TypeVar("S")
E = TypeVar("E")


@dataclass(frozen=True)
class StateMachine(Generic[S]):
    name: str
    transitions: Mapping[str, frozenset[str]]
    terminal: frozenset[str]
    event_types: Mapping[str, str]

    # ------------------------------------------------------------ 查询
    def allowed_next(self, current: str) -> frozenset[str]:
        return self.transitions.get(current, frozenset())

    def can(self, current: str, to: str) -> bool:
        return to in self.allowed_next(current)

    def assert_legal(self, current: str, to: str) -> None:
        if current in self.terminal:
            raise TerminalStateError(
                f"E-2: {current} is terminal, cannot transition to {to}"
            )
        if not self.can(current, to):
            raise IllegalTransition(
                f"E-1: illegal transition {current} → {to} "
                f"(allowed: {sorted(self.allowed_next(current))})"
            )

    def event_type_for(self, to: str) -> str:
        return self.event_types[to]


#: Execution 状态转换表。
#: 注意 STALE → RUNNING **不在表中**：Lease 过期后必须经 `ExecutionAggregate.recover()`
#: 开新 Attempt + 分配新 fencing_token，不允许直接改状态续跑（E-23）。
EXECUTION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ExecutionStatus.PENDING.value: frozenset(
        {ExecutionStatus.RUNNING.value, ExecutionStatus.CANCELLED.value}
    ),
    ExecutionStatus.RUNNING.value: frozenset(
        {
            ExecutionStatus.PENDING.value,     # 失败后重试：回到可调度，等 Scheduler 重新 Claim
            ExecutionStatus.SUSPENDED.value,
            ExecutionStatus.STALE.value,
            ExecutionStatus.COMPLETED.value,
            ExecutionStatus.FAILED.value,
            ExecutionStatus.CANCELLED.value,
        }
    ),
    ExecutionStatus.STALE.value: frozenset(
        {ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value}
    ),
    ExecutionStatus.SUSPENDED.value: frozenset(
        {
            ExecutionStatus.PENDING.value,     # Wake-up：重新变成 Runnable Task，再被 Claim
            ExecutionStatus.FAILED.value,
            ExecutionStatus.CANCELLED.value,
        }
    ),
    ExecutionStatus.COMPLETED.value: frozenset(),
    ExecutionStatus.FAILED.value: frozenset(),
    ExecutionStatus.CANCELLED.value: frozenset(),
}

EXECUTION_EVENT_TYPES: Mapping[str, str] = {
    ExecutionStatus.PENDING.value: EXECUTION_RESUMED,   # 回到可调度：重试 / Wake-up
    ExecutionStatus.RUNNING.value: EXECUTION_RUNNING,
    ExecutionStatus.STALE.value: EXECUTION_STALE,
    ExecutionStatus.SUSPENDED.value: EXECUTION_SUSPENDED,
    ExecutionStatus.COMPLETED.value: EXECUTION_COMPLETED,
    ExecutionStatus.FAILED.value: EXECUTION_FAILED,
    ExecutionStatus.CANCELLED.value: EXECUTION_CANCELLED,
}

ATTEMPT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    AttemptStatus.RUNNING.value: frozenset(
        {
            AttemptStatus.SUCCEEDED.value,
            AttemptStatus.FAILED.value,
            AttemptStatus.TIMEOUT.value,
            AttemptStatus.CANCELLED.value,
        }
    ),
    AttemptStatus.SUCCEEDED.value: frozenset(),
    AttemptStatus.FAILED.value: frozenset(),
    AttemptStatus.TIMEOUT.value: frozenset(),
    AttemptStatus.CANCELLED.value: frozenset(),
}

ATTEMPT_EVENT_TYPES: Mapping[str, str] = {
    AttemptStatus.RUNNING.value: ATTEMPT_STARTED,
    AttemptStatus.SUCCEEDED.value: ATTEMPT_SUCCEEDED,
    AttemptStatus.FAILED.value: ATTEMPT_FAILED,
    AttemptStatus.TIMEOUT.value: ATTEMPT_TIMEOUT,
    AttemptStatus.CANCELLED.value: ATTEMPT_CANCELLED,
}


class ExecutionStateMachine(StateMachine[ExecutionStatus]):
    def __init__(self) -> None:
        super().__init__(
            name="execution",
            transitions=EXECUTION_TRANSITIONS,
            terminal=frozenset(s.value for s in TERMINAL_EXECUTION_STATUSES),
            event_types=EXECUTION_EVENT_TYPES,
        )

    def transition(
        self,
        execution: Execution,
        to: ExecutionStatus,
        *,
        expected_version: int | None = None,
        suspension: Any = None,
    ) -> Event:
        """唯一状态变更入口。返回 Event（调用方负责写入 Outbox，X-3）。"""
        current = execution.status.value
        self.assert_legal(current, to.value)
        execution.check_version(expected_version)

        with execution.mutating():
            execution.status = to
            execution.suspension = suspension
            if to is ExecutionStatus.SUSPENDED:
                pass  # suspension 必填，由 validate 兜底
            execution.version += 1

        execution.validate()
        return new_event(
            aggregate_type="execution",
            aggregate_id=execution.execution_id,
            event_type=self.event_type_for(to.value),
            payload={
                "from": current,
                "to": to.value,
                "task_id": execution.task_id,
                "version": execution.version,
                **(
                    {"suspension_reason": suspension.reason.value}
                    if suspension is not None
                    else {}
                ),
            },
            aggregate_version=execution.version,
        )


class AttemptStateMachine(StateMachine[AttemptStatus]):
    def __init__(self) -> None:
        super().__init__(
            name="attempt",
            transitions=ATTEMPT_TRANSITIONS,
            terminal=frozenset({s.value for s in AttemptStatus if s is not AttemptStatus.RUNNING}),
            event_types=ATTEMPT_EVENT_TYPES,
        )

    def transition(
        self,
        attempt: Attempt,
        to: AttemptStatus,
        *,
        expected_version: int | None = None,
    ) -> Event:
        current = attempt.status.value
        self.assert_legal(current, to.value)
        if expected_version is not None and expected_version != attempt.version:
            raise IllegalTransition(
                f"E-13: attempt version mismatch: expected={expected_version}, "
                f"actual={attempt.version}"
            )
        attempt.status = to
        attempt.version += 1
        return new_event(
            aggregate_type="attempt",
            aggregate_id=attempt.attempt_id,
            event_type=self.event_type_for(to.value),
            payload={
                "from": current,
                "to": to.value,
                "execution_id": attempt.execution_id,
                "attempt_no": attempt.attempt_no,
                "version": attempt.version,
            },
            aggregate_version=attempt.version,
        )
