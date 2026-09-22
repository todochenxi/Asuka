"""Lease：谁在一段时间内拥有执行权。

E-20  Lease 挂在 Execution，不挂在 Task（Scheduler 选 Task，Claim Execution）
E-22  所有写回必须携带 fencing_token，落后即抛 StaleWriteError

三者不是一回事：

    Lease          谁拥有执行权（会过期）
    Lock           短临界区互斥（不跨进程生命周期）
    Fencing Token  防过期持有者回写（单调递增，不可伪造）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..errors import StaleWriteError
from ..ids import new_lease_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Lease:
    lease_id: str = field(default_factory=new_lease_id)
    execution_id: str = ""                  # E-20
    attempt_no: int = 1
    worker_id: str = ""
    fencing_token: int = 1                  # E-22：单调递增
    acquired_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime = field(default_factory=lambda: _utcnow() + timedelta(seconds=30))
    heartbeat_at: datetime = field(default_factory=_utcnow)
    version: int = 1

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("Lease.execution_id is required")
        if not self.worker_id:
            raise ValueError("Lease.worker_id is required")
        if self.fencing_token < 1:
            raise ValueError("Lease.fencing_token must be >= 1")
        if self.expires_at <= self.acquired_at:
            raise ValueError("Lease.expires_at must be after acquired_at")

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) > self.expires_at

    def authorize(self, token: int) -> None:
        """E-22：写回校验。token 落后于当前值 → 拒绝。

        否则旧 Worker 在 Lease 过期后复活，会污染已被新 Worker 接管的 Execution。
        """
        if token < self.fencing_token:
            raise StaleWriteError(
                f"E-22: rejected stale write from worker {self.worker_id}: "
                f"token={token} < current={self.fencing_token}"
            )

    def renew(self, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=30), token: int) -> None:
        self.authorize(token)
        now = now or _utcnow()
        self.heartbeat_at = now
        self.expires_at = now + ttl
        self.version += 1

    def next_token(self) -> int:
        """Recovery 后重新 Claim 时分配的新 token。"""
        return self.fencing_token + 1
