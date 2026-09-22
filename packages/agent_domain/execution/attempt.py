"""Attempt：Execution 的一次实际执行尝试。

E-4  Retry = 开新 Attempt，不是状态回退
E-5  同一 Execution 同一时刻最多一个 RUNNING 的 Attempt
E-6  attempt_no 单调递增，不可复用
E-7  只有 RUNNING 的 Attempt 才能持有有效 Lease

Attempt 是终态对象：RUNNING → SUCCEEDED / FAILED / TIMEOUT / CANCELLED，不做二次转换。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from ..errors import InvariantViolation
from ..ids import new_attempt_id
from .retry import ErrorInfo

RUNNING = "RUNNING"


class AttemptStatus(str, Enum):
    RUNNING = RUNNING
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.TIMEOUT,
        AttemptStatus.CANCELLED,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Attempt:
    attempt_id: str = field(default_factory=new_attempt_id)
    execution_id: str = ""
    attempt_no: int = 1                     # 从 1 开始，单调递增（E-6）
    status: AttemptStatus = AttemptStatus.RUNNING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: ErrorInfo | None = None
    result: Mapping[str, Any] | None = None
    checkpoint_id: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise InvariantViolation("Attempt.execution_id is required")
        if self.attempt_no < 1:
            raise InvariantViolation("E-6: attempt_no must start from 1")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ATTEMPT_STATUSES

    @property
    def is_running(self) -> bool:
        return self.status is AttemptStatus.RUNNING

    def can_hold_lease(self) -> bool:
        """E-7：只有 RUNNING 的 Attempt 能持有有效 Lease。"""
        return self.is_running
