"""端口的内存实现（阶段 3 用）。

不引入任何基础设施：这些类只是为了把 Kernel 的语义跑通。
阶段 5/6 会把 ExecutionRepository 换成 PostgreSQL、CancelSignalStore 换成 Redis。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Iterable, Mapping, Sequence

from packages.agent_domain.events.event import Event
from packages.agent_domain.execution import Attempt, Execution, ExecutionStatus, Task


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用：可控时间，方便制造 Lease 过期。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now


class InMemoryExecutionRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, Execution] = {}
        self._by_task: dict[str, str] = {}
        self._lock = Lock()

    def add(self, execution: Execution) -> None:
        with self._lock:
            if execution.execution_id in self._by_id:
                raise ValueError(f"execution {execution.execution_id} already exists")
            self._by_id[execution.execution_id] = execution
            self._by_task[execution.task_id] = execution.execution_id

    def get(self, execution_id: str) -> Execution | None:
        return self._by_id.get(execution_id)

    def get_by_task(self, task_id: str) -> Execution | None:
        execution_id = self._by_task.get(task_id)
        return self._by_id.get(execution_id) if execution_id else None

    def save(self, execution: Execution, expected_version: int | None = None) -> None:
        with self._lock:
            current = self._by_id.get(execution.execution_id)
            if current is None:
                raise KeyError(execution.execution_id)
            if expected_version is not None and current.version != expected_version:
                from packages.agent_domain.errors import ConcurrentStateError

                raise ConcurrentStateError(
                    f"E-13: stale save, expected version {expected_version}, "
                    f"actual {current.version}"
                )
            self._by_id[execution.execution_id] = execution

    def list_by_status(self, status: ExecutionStatus, limit: int = 100) -> Sequence[Execution]:
        return [e for e in self._by_id.values() if e.status is status][:limit]

    def list_with_expired_lease(self, now: datetime, limit: int = 100) -> Sequence[Execution]:
        out = []
        for e in self._by_id.values():
            if e.lease is not None and e.lease.is_expired(now):
                out.append(e)
        return out[:limit]

    def all(self) -> Sequence[Execution]:
        return list(self._by_id.values())


class InMemoryTaskRepository:
    """Task 的内存实现（E-26）。

    与 PG 实现共用同一个语义：`add()` 第一次写为准（E-28，重演不改）。
    """

    def __init__(self) -> None:
        self._rows: dict[str, Task] = {}

    def add(self, task: Task) -> None:
        self._rows.setdefault(task.task_id, task)

    def get(self, task_id: str) -> Task | None:
        return self._rows.get(task_id)

    def get_many(self, task_ids: Sequence[str]) -> Mapping[str, Task]:
        wanted = set(task_ids)
        return {tid: t for tid, t in self._rows.items() if tid in wanted}


class InMemoryAttemptRepository:
    """Attempt 历史的内存实现（E-4：Retry = 新 Attempt，历史必须可查）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], Attempt] = {}

    def save(self, attempt: Attempt) -> None:
        self._rows[(attempt.execution_id, attempt.attempt_no)] = attempt

    def get(self, execution_id: str, attempt_no: int) -> Attempt | None:
        return self._rows.get((execution_id, attempt_no))

    def list_by_execution(self, execution_id: str) -> Sequence[Attempt]:
        return sorted(
            (a for (eid, _), a in self._rows.items() if eid == execution_id),
            key=lambda a: a.attempt_no,
        )


class InMemoryOutbox:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._published: set[str] = set()

    def append(self, events: Iterable[Event]) -> None:
        self._events.extend(events)

    def pending(
        self, limit: int = 100, exclude: Sequence[str] = ()
    ) -> Sequence[Event]:
        skip = set(exclude)
        return [
            e
            for e in self._events
            if e.event_id not in self._published and e.event_id not in skip
        ][:limit]

    def mark_published(self, event_ids: Sequence[str]) -> None:
        self._published.update(event_ids)

    def all(self) -> Sequence[Event]:
        return list(self._events)


class InMemoryCancelSignals:
    def __init__(self) -> None:
        self._signals: set[str] = set()

    def set(self, execution_id: str, ttl_seconds: int = 3600) -> None:
        self._signals.add(execution_id)

    def get(self, execution_id: str) -> bool:
        return execution_id in self._signals

    def clear(self, execution_id: str) -> None:
        self._signals.discard(execution_id)


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._data: dict[str, Mapping[str, Any]] = {}

    def get(self, key: str) -> Mapping[str, Any] | None:
        return self._data.get(key)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self._data[key] = value


class RecordingEventPublisher:
    """测试用：记录发布过的事件（模拟 Outbox Publisher → Kafka）。"""

    def __init__(self, outbox: InMemoryOutbox) -> None:
        self._outbox = outbox
        self.published: list[Event] = []

    def publish(self, events: Sequence[Event]) -> int:
        self.published.extend(events)
        self._outbox.mark_published([e.event_id for e in events])
        return len(events)

    def drain(self) -> int:
        """把 pending 的事件全部发布一次（至少一次投递）。"""
        return self.publish(list(self._outbox.pending()))
