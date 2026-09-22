"""Kafka Adapter（阶段 7）。

    Producer：Outbox → Kafka（至少一次，由 OutboxPublisher 驱动）
    Consumer：Kafka → 业务侧（按 event_id 去重，见 consumers.py）

两个必须做对的点：

1. **Key = aggregate_id**
   同一个 Execution / Attempt 的事件必须进同一个分区，否则分区之间不保序，
   消费者会看到 `execution.completed` 早于 `execution.running` —— Read Model 直接错乱。

2. **event_id 必须在消息体里**（不能只放 header）
   header 会在跨系统转发 / 重放时丢；去重键必须跟着 payload 走。

客户端是 duck-typed 的 confluent-kafka / kafka-python 兼容对象；本模块不 import 任何 Kafka 库。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from packages.agent_domain.events.event import Event

DEFAULT_TOPIC = "agentos.events"
DEFAULT_PREFIX = "agentos"


def encode_event(event: Event) -> bytes:
    """事件信封：event_id 与 payload 同级，去重键绝不只放在 header 里。"""
    envelope = {
        "event_id": event.event_id,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "event_type": event.event_type,
        "aggregate_version": event.aggregate_version,
        "occurred_at": _iso(event.occurred_at),
        "payload": dict(event.payload),
    }
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def decode_event(raw: bytes | str) -> Event:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    data = json.loads(raw)
    occurred_at = data.get("occurred_at")
    return Event(
        event_id=data["event_id"],
        aggregate_type=data["aggregate_type"],
        aggregate_id=data["aggregate_id"],
        event_type=data["event_type"],
        payload=data.get("payload") or {},
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(),
        aggregate_version=data.get("aggregate_version", 1),
    )


def _iso(value: datetime) -> str:
    return value.isoformat()


class KafkaEventConsumer:
    """Kafka → `Event`（M30 / 空洞 209）。

    ------------------------------------------------------------------
    为什么**不**在这里 commit offset

    `poll()` 只负责取出并解码，`commit()` 是单独一次调用，由调用方在
    **自己的事务提交之后**再调（`ProcessRuntime.on_commit`）。

    顺序反过来的后果不是"多处理一次"，是**永远不处理**：
        offset 先提交 → PG 事务随后回滚 → 那条消息再也不会被投递
        Kafka 侧：已消费。PG 侧：从没发生过。
    两边都没有报错，而对账时才发现少了一条 ——
    这正是 A-12 那条判据里"变错"的那一半。

    ------------------------------------------------------------------
    客户端是 duck-typed 的

    confluent-kafka 与 kafka-python 的取消息签名不同（`consume(n, timeout=)`
    与 `poll(timeout=, max_records=)`），这里两种都认，本模块不 import 任何 Kafka 库。
    """

    def __init__(
        self,
        consumer: Any,
        *,
        topics: Sequence[str] = (DEFAULT_TOPIC,),
        batch_size: int = 32,
        poll_timeout: float = 1.0,
    ) -> None:
        self.consumer = consumer
        self.topics = tuple(topics)
        self.batch_size = batch_size
        self.poll_timeout = poll_timeout

    def subscribe(self, topics: Sequence[str] | None = None) -> None:
        wanted = tuple(topics or self.topics)
        self.consumer.subscribe(list(wanted))

    def poll(self, timeout: float | None = None) -> list[Event]:
        """取一批消息并解码。**不提交 offset**。

        单条解码失败会**抛出**，不做"跳过坏消息"的兜底：
        一个解不开的信封意味着生产者与消费者对信封格式的理解已经不一致，
        静默跳过会让"为什么这条事件消失了"没有答案。
        """
        return [decode_event(_value(m)) for m in self._messages(timeout)]

    def commit(self) -> None:
        """事务提交之后才许调。见类上方那段。"""
        self.consumer.commit()

    def close(self) -> None:
        close = getattr(self.consumer, "close", None)
        if close is not None:
            close()

    def _messages(self, timeout: float | None) -> list[Any]:
        wait = self.poll_timeout if timeout is None else timeout
        if hasattr(self.consumer, "consume"):
            # confluent-kafka：`consume(n, timeout)` 的 timeout 是**秒**；
            # 没有消息时返回 None。
            return list(self.consumer.consume(self.batch_size, wait) or ())
        # kafka-python：`poll(timeout_ms=, max_records=)` 的 timeout 是**毫秒** ——
        # 同一个 `poll` 名字，两种单位。这里做换算，别处一律用秒。
        polled = (
            self.consumer.poll(int(wait * 1000), max_records=self.batch_size) or {}
        )
        return [m for records in polled.values() for m in records]


def _value(message: Any) -> Any:
    """两种客户端的消息对象都认：confluent 的 `.value()` 与 kafka-python 的 `.value`。"""
    value = getattr(message, "value", None)
    return value() if callable(value) else value


class KafkaEventPublisher:
    """`ports.EventPublisher` 的 Kafka 实现。"""

    def __init__(
        self,
        producer: Any,
        *,
        topic_prefix: str = DEFAULT_PREFIX,
        default_topic: str = DEFAULT_TOPIC,
        topic_map: Mapping[str, str] | None = None,
    ) -> None:
        self.producer = producer
        self.topic_prefix = topic_prefix
        self.default_topic = default_topic
        self.topic_map = dict(topic_map or {})

    def topic_for(self, event: Event) -> str:
        """按 event_type 分流；未知类型进默认 topic（不能因为没配就丢事件）。"""
        if event.event_type in self.topic_map:
            return self.topic_map[event.event_type]
        domain = event.event_type.split(".", 1)[0]
        return f"{self.topic_prefix}.{domain}.events"

    def key_for(self, event: Event) -> str:
        """同聚合同分区 → 同聚合内保序。"""
        return f"{event.aggregate_type}:{event.aggregate_id}"

    def publish(self, events: Sequence[Event]) -> int:
        for event in events:
            self.producer.produce(
                topic=self.topic_for(event),
                key=self.key_for(event),
                value=encode_event(event),
                headers={
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                },
            )
        # 只在批次末尾 flush 一次：produce 是异步的，批次内 flush 会破坏顺序
        self.producer.flush()
        return len(events)
