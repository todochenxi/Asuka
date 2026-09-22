"""阶段 7（上）：Outbox Publisher → Kafka → 消费者去重。

核心断言：**投递是至少一次的，所以去重是正确性的一部分，不是优化。**

    publisher 先 publish 后 mark → 崩溃在中间 → 重投
        ↓
    消费者必须按 event_id 去重，否则 Read Model / 审计会重复计数
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.events.event import (
    EXECUTION_CREATED,
    EXECUTION_RUNNING,
    Event,
    new_event,
)
from packages.execution_kernel.adapters.kafka import (
    KafkaEventPublisher,
    decode_event,
    encode_event,
)
from packages.execution_kernel.adapters.postgres import (
    PostgresOutboxStore,
    PostgresProcessedEventStore,
)
from packages.execution_kernel.consumers import (
    IdempotentConsumer,
    InMemoryProcessedEventStore,
)
from packages.execution_kernel.inmemory import ManualClock, RecordingEventPublisher
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.outbox_delivery import InMemoryOutboxDeliveryStore
from packages.execution_kernel.publisher import OutboxPublisher

from .helpers import make_task
from .sqlite_shim import connect, load_schema_sql

T0 = ManualClock().now()


class FakeKafkaProducer:
    """记录 produce 调用的最小替身。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.flushes: int = 0

    def produce(self, *, topic: str, key: str, value: bytes, headers: dict) -> None:
        self.messages.append({"topic": topic, "key": key, "value": value, "headers": headers})

    def flush(self) -> None:
        self.flushes += 1


class CrashingPublisher:
    """投递成功但随后崩溃 —— 用来复现"publish 了却没 mark"。"""

    def __init__(self, sink: list[Event]) -> None:
        self.sink = sink

    def publish(self, events) -> int:
        self.sink.extend(events)
        raise RuntimeError("process died right after producing")


class EnvelopeTest(unittest.TestCase):
    def test_roundtrip_preserves_event_id(self) -> None:
        event = new_event(
            aggregate_type="execution",
            aggregate_id="exe_1",
            event_type=EXECUTION_CREATED,
            payload={"status": "PENDING"},
            aggregate_version=1,
        )
        restored = decode_event(encode_event(event))
        self.assertEqual(restored.event_id, event.event_id)
        self.assertEqual(restored.event_type, event.event_type)
        self.assertEqual(restored.payload, {"status": "PENDING"})
        self.assertEqual(restored.aggregate_id, "exe_1")

    def test_event_id_is_in_body_not_only_header(self) -> None:
        """header 会在跨系统转发时丢，去重键必须跟着 payload 走。"""
        event = new_event(
            aggregate_type="execution", aggregate_id="exe_1",
            event_type=EXECUTION_CREATED, aggregate_version=1,
        )
        import json

        body = json.loads(encode_event(event).decode())
        self.assertEqual(body["event_id"], event.event_id)


class KafkaPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.producer = FakeKafkaProducer()
        self.publisher = KafkaEventPublisher(self.producer)

    def _event(self, event_type: str, aggregate_id: str, aggregate_type="execution") -> Event:
        return new_event(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            aggregate_version=1,
        )

    def test_topic_is_derived_from_event_type(self) -> None:
        self.publisher.publish([self._event("execution.created", "exe_1")])
        self.assertEqual(self.producer.messages[0]["topic"], "agentos.execution.events")

    def test_topic_map_overrides(self) -> None:
        publisher = KafkaEventPublisher(
            self.producer, topic_map={"execution.created": "agentos.audit"}
        )
        publisher.publish([self._event("execution.created", "exe_1")])
        self.assertEqual(self.producer.messages[0]["topic"], "agentos.audit")

    def test_key_is_aggregate_scoped_for_ordering(self) -> None:
        """同聚合同分区 → 消费者不会先看到 completed 再看到 running。"""
        self.publisher.publish(
            [
                self._event(EXECUTION_RUNNING, "exe_1"),
                self._event(EXECUTION_CREATED, "exe_1"),
                self._event(EXECUTION_CREATED, "exe_2"),
            ]
        )
        keys = [m["key"] for m in self.producer.messages]
        self.assertEqual(keys[:2], ["execution:exe_1", "execution:exe_1"])
        self.assertEqual(keys[2], "execution:exe_2")

    def test_flush_once_per_batch(self) -> None:
        """批次内多次 flush 会破坏顺序 —— 只在末尾 flush 一次。"""
        self.publisher.publish([self._event(EXECUTION_CREATED, f"exe_{i}") for i in range(5)])
        self.assertEqual(self.producer.flushes, 1)
        self.assertEqual(len(self.producer.messages), 5)


class OutboxPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        from packages.execution_kernel.inmemory import InMemoryOutbox

        self.outbox = InMemoryOutbox()
        self.sink: list[Event] = []
        self.publisher = OutboxPublisher(
            outbox=self.outbox,
            publisher=RecordingEventPublisher(self.outbox),
            delivery=InMemoryOutboxDeliveryStore(),
        )

    def test_drain_publishes_then_marks(self) -> None:
        events = [
            new_event(aggregate_type="execution", aggregate_id="exe_1",
                      event_type=EXECUTION_CREATED, aggregate_version=1),
            new_event(aggregate_type="execution", aggregate_id="exe_1",
                      event_type=EXECUTION_RUNNING, aggregate_version=2),
        ]
        self.outbox.append(events)
        self.assertEqual(self.publisher.drain(), 2)
        self.assertEqual(self.outbox.pending(), [])           # 已标记
        self.assertEqual(self.publisher.drain(), 0)           # 第二轮没有待发

    def test_crash_after_publish_leaves_event_pending(self) -> None:
        """publish 成功但没 mark → 事件仍在 pending（这是 at-least-once 的来源）。

        M21 之后这里会有**两次**投递记录：批次那一次 + 逐条隔离那一次。
        批次失败时无法知道 broker 到底收到了几条，只能逐条重试 ——
        重复的代价由消费者按 event_id 去重承担。
        """
        delivered: list[Event] = []
        crashing = OutboxPublisher(
            outbox=self.outbox,
            publisher=CrashingPublisher(delivered),
            delivery=InMemoryOutboxDeliveryStore(),
        )
        self.outbox.append([
            new_event(aggregate_type="execution", aggregate_id="exe_1",
                      event_type=EXECUTION_CREATED, aggregate_version=1)
        ])

        with self.assertRaises(RuntimeError):
            crashing.drain()

        self.assertEqual({e.event_id for e in delivered},
                         {self.outbox.all()[0].event_id})     # 已经投出去了
        self.assertEqual(len(self.outbox.pending()), 1)       # 但没标记 → 会重投

    def test_redelivery_is_deduplicated_by_consumer(self) -> None:
        """投出去 N 次，业务只跑一次 —— 去重是正确性的一部分，不是优化。"""
        raw: list[Event] = []
        crashing = OutboxPublisher(
            outbox=self.outbox,
            publisher=CrashingPublisher(raw),
            delivery=InMemoryOutboxDeliveryStore(),
        )
        event = new_event(aggregate_type="execution", aggregate_id="exe_1",
                          event_type=EXECUTION_CREATED, aggregate_version=1)
        self.outbox.append([event])

        handled: list[str] = []
        store = InMemoryProcessedEventStore()
        consumer = IdempotentConsumer(store=store, handler=lambda e: handled.append(e.event_id))

        with self.assertRaises(RuntimeError):
            crashing.drain()
        # 批次一次 + 逐条隔离一次：同一条事件到了 Kafka 两次
        self.assertEqual(len(raw), 2)
        self.assertEqual({e.event_id for e in raw}, {event.event_id})
        processing, skipped = consumer.consume_batch(list(raw))
        self.assertEqual((processing, skipped), (1, 1))

        self.publisher.drain()                                      # 正常重投
        processing2, _ = consumer.consume_batch(list(raw))
        self.assertEqual(processing2, 0)                            # 一条都没被重复处理
        self.assertEqual(handled, [event.event_id])                 # 业务只跑了一次


class ConsumerDedupeTest(unittest.TestCase):
    def test_pg_store_is_durable_across_instances(self) -> None:
        """去重记录必须跨进程可见 —— 所以它落在 PG，不是 Redis。"""
        conn = connect(load_schema_sql("002_outbox_consumer.sql"))
        self.addCleanup(conn.close)

        first = PostgresProcessedEventStore(conn)
        self.assertTrue(first.mark("evt_1", EXECUTION_CREATED))
        self.assertFalse(first.mark("evt_1", EXECUTION_CREATED))   # 第二次登记失败

        other = PostgresProcessedEventStore(conn)                  # 另一个进程
        self.assertTrue(other.seen("evt_1"))
        self.assertFalse(other.seen("evt_never"))


class EndToEndTest(unittest.TestCase):
    """Kernel 写 Outbox → Publisher 投 Kafka → 消费者去重。"""

    def test_events_reach_consumer_exactly_once_despite_redelivery(self) -> None:
        conn = connect(load_schema_sql("002_outbox_consumer.sql"))
        self.addCleanup(conn.close)
        outbox = PostgresOutboxStore(conn)
        kernel = ExecutionKernel(
            repository=_MemoryRepo(), outbox=outbox, clock=ManualClock()
        )
        execution = kernel.submit(make_task())
        kernel.claim(execution.execution_id, worker_id="w1")

        producer = FakeKafkaProducer()
        outbox_publisher = OutboxPublisher(
            outbox=outbox,
            publisher=KafkaEventPublisher(producer),
            delivery=InMemoryOutboxDeliveryStore(),
        )
        self.assertGreater(outbox_publisher.drain(), 0)

        handled: list[Event] = []
        consumer = IdempotentConsumer(
            store=PostgresProcessedEventStore(conn),
            handler=lambda e: handled.append(e),
        )

        delivered = [decode_event(m["value"]) for m in producer.messages]
        first = consumer.consume_batch(delivered)
        # Kafka 重投同一批（消费者重启 / rebalance）
        second = consumer.consume_batch(delivered)

        self.assertEqual(first[1], 0)                 # 第一次：全部处理
        self.assertEqual(second[0], 0)                # 第二次：全部跳过
        self.assertEqual(len(handled), len(delivered))
        # 同一 Execution 的事件按投递顺序到达（同 key → 同分区）
        self.assertTrue(all(e.aggregate_id == execution.execution_id
                            for e in handled if e.aggregate_type == "execution"))


class _MemoryRepo:
    """只为端到端串一遍链路，不测 PG 本身。"""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def add(self, execution) -> None:
        self._data[execution.execution_id] = execution

    def get(self, execution_id):
        return self._data.get(execution_id)

    def get_by_task(self, task_id):
        for e in self._data.values():
            if e.task_id == task_id:
                return e
        return None

    def save(self, execution, expected_version=None) -> None:
        self._data[execution.execution_id] = execution

    def list_by_status(self, status, limit=100):
        return [e for e in self._data.values() if e.status is status][:limit]

    def list_with_expired_lease(self, now, limit=100):
        return []


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
