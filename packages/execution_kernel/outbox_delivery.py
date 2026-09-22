"""Outbox 投递状态：多副本 publisher 的领地表（M21 / §59）。

`OutboxPublisher.drain()` 在**单实例**下是对的（见 `publisher.py`）。
一旦部署两个副本，两边都会 `pending(limit=N)` 拿到**同一批**（谁都还没 mark），
于是同一条事件被投两次。at-least-once 语义下这"不算错"（消费者按 event_id 去重），
但 N 副本 = N 倍放大，而且是在**没有任何故障**的情况下放大 —— 那是纯浪费。

所以"谁正在投这条"必须落库。这就是 `OutboxDeliveryStore`。

它和 `OutboxStore` 的分工：

    OutboxStore          业务事务写下的"发生了什么"（X-3：与状态同事务）
    OutboxDeliveryStore  投递进程写下的"发到哪一步了"（本文件）

承载的不变量：

    PR-3  认领必须原子 —— 与 A-11 / S-4 同源：两个实例同时扫到同一条，
          只有一个能拿到。判定权在存储层（单条 UPDATE ... WHERE），
          不在进程内存里 —— 进程内存里的"我看看有没有人占着"永远有窗口。
    PR-4  领地必须有租约（claimed_until）。进程被 SIGKILL 时没有机会归还领地，
          租约过期是**唯一**的安全网。所以 claim 必须带 ttl，没有例外。
    PR-5  毒消息必须让位：attempts 达上限 → dead_at。一条永远投不出去的事件
          如果一直回到候选集，重试预算会全烧在它身上。
    PR-6  进死信必须带 last_error，而且查得出来。静默丢弃比停摆更坏。
    PR-10 死信不许自动复活 —— 与 S-14 同源：状态倒流只能由人显式发起（`reopen`）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Protocol, Sequence

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DeliveryRecord:
    """一条事件的投递状态。

    行只在"在途"期间存在：
        claim 成功 → 有行
        投递成功   → mark_sent 删行（领地归还，表里只剩下在途的）
        判死       → 留行（dead_at 非空），等人工处理
    """

    event_id: str
    attempts: int = 0
    claimed_by: str | None = None
    claimed_until: datetime | None = None
    last_error: str | None = None
    dead_at: datetime | None = None

    @property
    def is_dead(self) -> bool:
        return self.dead_at is not None

    def held_by(self, owner: str, now: datetime) -> bool:
        """PR-4：租约过期 = 不再持有。"""
        return (
            self.claimed_by == owner
            and self.claimed_until is not None
            and self.claimed_until > now
        )

    def claimable(self, now: datetime) -> bool:
        if self.is_dead:
            return False
        return self.claimed_until is None or self.claimed_until <= now

    def summary(self) -> str:
        if self.is_dead:
            return f"dead attempts={self.attempts} error={self.last_error}"
        if self.claimed_by:
            return f"held by {self.claimed_by} until {self.claimed_until}"
        return f"free attempts={self.attempts}"


class OutboxDeliveryStore(Protocol):
    """投递领地。实现必须是**存储层原子**的 —— 见 PR-3。"""

    def claim(self, event_id: str, owner: str, ttl: timedelta, now: datetime) -> bool:
        """PR-3 / PR-4：原子认领。返回 False = 别人正持有 / 已判死。"""

    def mark_sent(self, event_ids: Sequence[str]) -> None:
        """投递成功 → 归还领地（删行）。"""

    def mark_failed(
        self, event_id: str, error: str, *, max_attempts: int, now: datetime
    ) -> bool:
        """PR-5 / PR-6：attempts+1；达上限就判死并写 dead_at。

        返回 True 表示**这一次**把它判死了。
        """

    def release(self, owner: str, event_ids: Sequence[str] | None = None) -> int:
        """PR-2：显式归还领地。不传 event_ids = 归还这个 owner 的全部。返回条数。"""

    def dead_ids(self, limit: int = 1000) -> Sequence[str]:
        """PR-6：死信必须查得出来（publisher 用它把死信排除出候选集）。"""

    def dead(self, limit: int = 100) -> Sequence[DeliveryRecord]:
        """带原因的死信。运维看的是 last_error，不是 event_id。"""

    def held(self, owner: str, now: datetime) -> Sequence[str]:
        """这个 owner 当前仍持有的 event_id。"""

    def reopen(self, event_id: str) -> bool:
        """PR-14：人工重开死信。唯一的复活通道。"""

    def get(self, event_id: str) -> DeliveryRecord | None: ...


class InMemoryOutboxDeliveryStore:
    """内存实现：语义与 PG 实现一致（含原子认领），只用于单进程 / 测试。

    ⚠️ 这里的"原子"靠 `Lock`。真实的原子性必须由**单条 SQL** 提供 ——
    进程之间的竞争，进程内的锁管不着（PR-3 的判定权必须在存储层）。
    """

    def __init__(self) -> None:
        self._rows: dict[str, DeliveryRecord] = {}
        self._lock = Lock()

    def claim(self, event_id: str, owner: str, ttl: timedelta, now: datetime) -> bool:
        with self._lock:
            row = self._rows.get(event_id)
            if row is None:
                self._rows[event_id] = DeliveryRecord(
                    event_id=event_id,
                    claimed_by=owner,
                    claimed_until=now + ttl,
                )
                return True
            if not row.claimable(now) and row.claimed_by != owner:
                return False
            # 同一 owner 重新 claim = 续租，允许
            self._rows[event_id] = replace(
                row, claimed_by=owner, claimed_until=now + ttl
            )
            return True

    def mark_sent(self, event_ids: Sequence[str]) -> None:
        with self._lock:
            for event_id in event_ids:
                self._rows.pop(event_id, None)

    def mark_failed(
        self, event_id: str, error: str, *, max_attempts: int, now: datetime
    ) -> bool:
        with self._lock:
            row = self._rows.get(event_id)
            if row is None or row.is_dead:
                return False
            attempts = row.attempts + 1
            if attempts >= max_attempts:
                self._rows[event_id] = replace(
                    row,
                    attempts=attempts,
                    last_error=error,
                    dead_at=now,
                    claimed_by=None,
                    claimed_until=None,
                )
                return True
            # 没判死 → 归还领地，下一轮可以再认领
            self._rows[event_id] = replace(
                row,
                attempts=attempts,
                last_error=error,
                claimed_by=None,
                claimed_until=None,
            )
            return False

    def release(self, owner: str, event_ids: Sequence[str] | None = None) -> int:
        with self._lock:
            targets = (
                list(event_ids)
                if event_ids is not None
                else [eid for eid, r in self._rows.items() if r.claimed_by == owner]
            )
            released = 0
            for event_id in targets:
                row = self._rows.get(event_id)
                if row is None or row.is_dead or row.claimed_by != owner:
                    continue
                self._rows[event_id] = replace(
                    row, claimed_by=None, claimed_until=None
                )
                released += 1
            return released

    def dead_ids(self, limit: int = 1000) -> Sequence[str]:
        with self._lock:
            return [eid for eid, r in self._rows.items() if r.is_dead][:limit]

    def dead(self, limit: int = 100) -> Sequence[DeliveryRecord]:
        with self._lock:
            rows = [r for r in self._rows.values() if r.is_dead]
        rows.sort(key=lambda r: r.dead_at or _EPOCH)
        return rows[:limit]

    def held(self, owner: str, now: datetime) -> Sequence[str]:
        with self._lock:
            return [eid for eid, r in self._rows.items() if r.held_by(owner, now)]

    def reopen(self, event_id: str) -> bool:
        with self._lock:
            row = self._rows.get(event_id)
            if row is None or not row.is_dead:
                return False
            self._rows[event_id] = replace(
                row, attempts=0, last_error=None, dead_at=None
            )
            return True

    def get(self, event_id: str) -> DeliveryRecord | None:
        with self._lock:
            return self._rows.get(event_id)

    def all(self) -> Sequence[DeliveryRecord]:
        with self._lock:
            return list(self._rows.values())
