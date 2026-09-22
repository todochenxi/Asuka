"""outbox_publisher：Outbox → Kafka 的投递进程（M21）。

    PG (outbox_events) --claim--> outbox_publisher --publish--> Kafka

多副本可以并行跑：认领是原子的（PR-3），所以 N 个副本是在**分摊**同一批活，
不是把同一批活做 N 遍。这也是为什么 `OutboxPublisher.delivery` 是必填的 ——
没有共享领地，"多副本安全"就无从谈起。
"""
from .app import OutboxPublisherApp, OutboxPublisherConfig

__all__ = ["OutboxPublisherApp", "OutboxPublisherConfig"]
