"""Kafka 消费者侧：**按 event_id 去重**。

因为投递是 at-least-once（见 publisher.py），重复不是异常而是常态。
所以去重不是"优化"，是**正确性的一部分**。

    X-5   PostgreSQL 是唯一 Truth；Kafka 只是 Event Log
    I-5   Event 一旦产生不可变 —— 这是 Replay 的前提，也是去重的前提

处理顺序是 **先 handler，再 mark**：

    handler() 成功 → 进程崩溃 → 没来得及 mark
        ↓
    重新消费时会再跑一次 handler

这意味着 `ProcessedEventStore` 只是**尽力而为**的去重：
真正的保证仍然要求 handler 本身幂等（用 `event_id` 或 `idempotency_key` 作为副作用键）。
反过来（先 mark 再 handler）能做到"最多一次"，但会在崩溃时静默丢处理 ——
对审计 / Evaluation 这类场景，丢处理比重复处理更难发现，所以不采用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Protocol, Sequence

from packages.agent_domain.events.event import Event


class ProcessedEventStore(Protocol):
    """已处理事件登记表。PG 实现见 `adapters/postgres.PostgresProcessedEventStore`。"""

    def seen(self, event_id: str) -> bool: ...

    def mark(self, event_id: str, event_type: str, processed_at: datetime | None = None) -> bool:
        """返回 True 表示**本次**登记成功（即之前没处理过）。"""


@dataclass
class IdempotentConsumer:
    """消费一条事件：已处理过就跳过，否则处理并登记。"""

    store: ProcessedEventStore
    handler: Callable[[Event], None]
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def consume(self, event: Event) -> bool:
        """返回 True 表示真的处理了；False 表示重复事件已跳过。"""
        if self.store.seen(event.event_id):
            self.skipped.append(event.event_id)
            return False
        self.handler(event)
        self.store.mark(event.event_id, event.event_type)
        self.processed.append(event.event_id)
        return True

    def consume_batch(self, events: Iterable[Event]) -> tuple[int, int]:
        """返回 (实际处理数, 去重跳过数)。"""
        events = list(events)
        done = sum(1 for e in events if self.consume(e))
        return done, len(events) - done


class InMemoryProcessedEventStore:
    """测试 / 单进程场景用。生产用 PG（跨进程可见）。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, datetime]] = {}

    def seen(self, event_id: str) -> bool:
        return event_id in self._rows

    def mark(
        self, event_id: str, event_type: str, processed_at: datetime | None = None
    ) -> bool:
        if event_id in self._rows:
            return False
        self._rows[event_id] = (event_type, processed_at or datetime.now())
        return True

    def all(self) -> Sequence[str]:
        return list(self._rows)
