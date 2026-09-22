"""outbox_publisher 进程（M21）。

这是 §45 里 `apps/outbox_publisher` 的落点，也是整个 `apps/` 层第一次真正存在。

它只做三件事，业务逻辑一点都不在这里：

    1. 用 `ProcessRuntime` 把 `OutboxPublisher.drain()` 变成一个有生命周期的进程
    2. 退出时把自己还握着的领地**显式归还**（PR-2）
    3. 把"活着"和"在干活"分开报出去（PR-8）

认领、投递、毒消息隔离全在 `packages/execution_kernel/publisher.py`
与 `outbox_delivery.py`。这里只负责决定"还要不要跑下一轮"。

适配器（OutboxStore / EventPublisher / OutboxDeliveryStore）由组合根注入 ——
本模块不 import 任何数据库或 Kafka 客户端，`packages/` 保持零第三方依赖。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Sequence

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.outbox_delivery import (
    DeliveryRecord,
    OutboxDeliveryStore,
)
from packages.execution_kernel.ports import Clock, EventPublisher, OutboxStore
from packages.execution_kernel.publisher import OutboxPublisher

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)


@dataclass
class OutboxPublisherConfig:
    instance_id: str = field(
        default_factory=lambda: f"outbox-publisher-{os.getpid()}"
    )
    batch_size: int = 100
    lease: timedelta = timedelta(seconds=30)
    max_attempts: int = 5
    idle_sleep: float = 0.05
    max_idle_sleep: float = 2.0
    error_sleep: float = 0.25
    max_consecutive_failures: int = 5


class OutboxPublisherApp:
    """`python -m apps.outbox_publisher` 跑的就是它。

    生产里由组合根构造：PG 连接 → `PostgresOutboxStore` +
    `PostgresOutboxDeliveryStore`，Kafka producer → `KafkaEventPublisher`，
    再配一个 `SignalStop()` 让它能响应 SIGTERM。
    """

    def __init__(
        self,
        *,
        outbox: OutboxStore,
        publisher: EventPublisher,
        delivery: OutboxDeliveryStore,
        config: OutboxPublisherConfig | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。不传就**没有**事务边界 ——
        #: 组合根必须显式接上，否则这个进程的写在 DB 意义上不是原子的。
        uow: Any = None,
    ) -> None:
        self.config = config or OutboxPublisherConfig()
        self.outbox = outbox
        self.delivery = delivery
        self.clock = clock or SystemClock()

        self.publisher = OutboxPublisher(
            outbox=outbox,
            publisher=publisher,
            delivery=delivery,
            owner=self.config.instance_id,
            batch_size=self.config.batch_size,
            lease=self.config.lease,
            max_attempts=self.config.max_attempts,
            clock=self.clock,
        )
        self.runtime = ProcessRuntime(
            name="outbox_publisher",
            signal=signal or ManualStop(),
            clock=self.clock,
            idle_sleep=self.config.idle_sleep,
            max_idle_sleep=self.config.max_idle_sleep,
            error_sleep=self.config.error_sleep,
            max_consecutive_failures=self.config.max_consecutive_failures,
            sleep=sleep or time.sleep,
            uow=uow,
            on_drain=self._release_territory,
        )

    def tick(self) -> int:
        return self.publisher.drain()

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        return self.runtime.health()

    @property
    def dead(self) -> Sequence[DeliveryRecord]:
        """PR-6：死信必须查得出来，而且带着原因。"""
        return self.delivery.dead()

    def _release_territory(self) -> int:
        """PR-2：退出前把还握在手里的领地显式归还。

        `drain()` 的正常路径自己会归还（mark_sent / mark_failed），
        所以这里通常是 0。留着它不是为了"通常"，而是为了崩在半路、
        没人来得及归还的那些情况 —— 沉默地带着领地消失，
        会让别的实例白等一个租约周期（PR-4），而且没人知道为什么慢。
        """
        return self.delivery.release(self.config.instance_id)
