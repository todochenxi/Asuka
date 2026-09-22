"""M21：apps/ 进程层 · outbox_publisher。

覆盖的不变量（§59）：

    PR-1   生命周期两阶段：停止信号只在 tick 与 tick 之间被检查
    PR-2   退出时把手上的领地显式归还
    PR-3   认领必须原子（多副本不重复持有同一条）
    PR-4   领地有租约：进程被 SIGKILL 时，租约过期是唯一的安全网
    PR-5   毒消息必须让位，且不能把同批的好事件一起拖死
    PR-6   进死信必须带 last_error，而且查得出来
    PR-7   空闲退避必须有上限
    PR-8   活着 ≠ 在干活；连续失败达阈值必须退出，而不是假装健康
    PR-9   投递顺序按 occurred_at，不由认领竞争决定
    PR-10  死信不许自动复活（只能 reopen）

每条不变量都配一个**控制组**：证明那条断言不是因为机制压根没生效才通过的。

PG 部分跑在 sqlite 上的 PG 方言替身，schema 直接读 `infrastructure/postgres/` 原文。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from apps._runtime import (
    ProcessRuntime,
    ProcessState,
    StopAfter,
)
from apps.outbox_publisher import OutboxPublisherApp, OutboxPublisherConfig
from packages.agent_domain.events.event import EXECUTION_CREATED, Event
from packages.execution_kernel.adapters.postgres import (
    PostgresOutboxDeliveryStore,
    PostgresOutboxStore,
)
from packages.execution_kernel.inmemory import (
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.outbox_delivery import InMemoryOutboxDeliveryStore
from packages.execution_kernel.ports import PartialPublishError
from packages.execution_kernel.publisher import OutboxPublisher

from .sqlite_shim import connect, load_schema_sql

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SCHEMA = ("001_kernel.sql", "006_outbox_delivery.sql")


def make_event(
    event_id: str,
    *,
    occurred_at: datetime = T0,
    event_type: str = EXECUTION_CREATED,
    aggregate_id: str = "exe_1",
) -> Event:
    return Event(
        event_id=event_id,
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        event_type=event_type,
        occurred_at=occurred_at,
    )


class FakeSink:
    """投递替身：broker 收下批次，但拒掉 `poison` 里的那几条。

    这是**毒消息**那一类失败 —— broker 能指名道姓，所以 publisher 可以就地判死，
    不该把进程拖下水。（真实的 Kafka producer 也是这样回来的：
    per-record 错误带着 record 本身。）
    """

    def __init__(self, poison: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.poison = poison or set()

    def publish(self, events) -> int:
        rejected: dict[str, str] = {}
        for event in events:
            if event.event_id in self.poison:
                rejected[event.event_id] = f"broker rejected {event.event_id}"
            else:
                self.sent.append(event.event_id)
        if rejected:
            raise PartialPublishError(rejected)
        return len(events)


class FlushFailsSink:
    """整批都落不了地（flush 超时），但 broker 本身是通的。

    对应**系统性故障**的灰色地带：publisher 指认不出是哪一条，
    只能退回逐条隔离重试 —— 好事件会被救回来，坏事件才判死。
    """

    def __init__(self, poison: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.poison = poison or set()
        self.batch_calls = 0

    def publish(self, events) -> int:
        self.batch_calls += 1
        if len(events) > 1:
            raise RuntimeError("flush timeout")
        event = events[0]
        if event.event_id in self.poison:
            raise PartialPublishError({event.event_id: "record too large"})
        self.sent.append(event.event_id)
        return 1


class SinkDown:
    """broker 彻底不可用 —— 连一条都发不出去。"""

    def publish(self, events) -> int:
        raise RuntimeError("broker unreachable")


class FlakyBroker:
    """前 N 次调用全挂，之后恢复 —— 模拟 broker 短暂不可用。"""

    def __init__(self, down_calls: int) -> None:
        self.down_calls = down_calls
        self.calls = 0
        self.sent: list[str] = []

    def publish(self, events) -> int:
        self.calls += 1
        if self.calls <= self.down_calls:
            raise RuntimeError("broker unreachable")
        self.sent.extend(e.event_id for e in events)
        return len(events)


def build_app(
    outbox,
    sink,
    delivery,
    *,
    stop=None,
    config: OutboxPublisherConfig | None = None,
):
    sleeps: list[float] = []
    app = OutboxPublisherApp(
        outbox=outbox,
        publisher=sink,
        delivery=delivery,
        config=config,
        signal=stop,
        sleep=sleeps.append,
    )
    return app, sleeps


# ---------------------------------------------------------------------------
# PR-3 / PR-4：领地
# ---------------------------------------------------------------------------


class TerritoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.outbox = InMemoryOutbox()
        self.delivery = InMemoryOutboxDeliveryStore()
        self.sink = FakeSink()

    def _replica(self, owner: str) -> OutboxPublisher:
        return OutboxPublisher(
            outbox=self.outbox, publisher=self.sink, delivery=self.delivery,
            owner=owner,
        )

    def test_pr3_two_replicas_do_not_both_take_the_same_event(self) -> None:
        self.outbox.append([make_event("evt_1")])
        self.assertEqual(self._replica("replica-a").drain(), 1)
        self.assertEqual(self._replica("replica-b").drain(), 0)
        self.assertEqual(self.sink.sent, ["evt_1"])

    def test_pr3_the_control_both_replicas_scan_the_same_batch(self) -> None:
        """控制组：不认领的话，两个副本扫到的就是同一批。

        所以上一条"第二个副本一滴都没投"不是因为队列本来就是空的。
        """
        self.outbox.append([make_event("evt_1"), make_event("evt_2")])
        ttl = timedelta(seconds=30)
        now = ManualClock().now()

        scanned_by_a = [e.event_id for e in self.outbox.pending()]
        scanned_by_b = [e.event_id for e in self.outbox.pending()]
        self.assertEqual(scanned_by_a, scanned_by_b)          # 谁都还没 mark

        self.assertTrue(self.delivery.claim("evt_1", "replica-a", ttl, now))
        self.assertFalse(self.delivery.claim("evt_1", "replica-b", ttl, now))

    def test_pr4_expired_lease_can_be_taken_over(self) -> None:
        """进程被 SIGKILL 时没有机会归还领地 —— 租约过期是唯一的安全网。"""
        clock = ManualClock()
        ttl = timedelta(seconds=30)
        self.assertTrue(self.delivery.claim("evt_1", "a", ttl, clock.now()))
        self.assertFalse(self.delivery.claim("evt_1", "b", ttl, clock.now()))

        later = clock.advance(timedelta(seconds=31))
        self.assertTrue(self.delivery.claim("evt_1", "b", ttl, later))

    def test_pr4_the_control_unexpired_lease_is_still_held(self) -> None:
        clock = ManualClock()
        ttl = timedelta(seconds=30)
        self.assertTrue(self.delivery.claim("evt_1", "a", ttl, clock.now()))
        clock.advance(timedelta(seconds=29))
        self.assertFalse(self.delivery.claim("evt_1", "b", ttl, clock.now()))
        self.assertEqual(self.delivery.held("a", clock.now()), ["evt_1"])


# ---------------------------------------------------------------------------
# PR-5 / PR-6 / PR-10：毒消息
# ---------------------------------------------------------------------------


class PoisonTest(unittest.TestCase):
    def _publisher(self, *, poison: set[str], max_attempts: int = 3):
        outbox = InMemoryOutbox()
        outbox.append([
            make_event("good_1"),
            make_event("bad"),
            make_event("good_2"),
        ])
        sink = FakeSink(poison=poison)
        delivery = InMemoryOutboxDeliveryStore()
        publisher = OutboxPublisher(
            outbox=outbox, publisher=sink, delivery=delivery,
            max_attempts=max_attempts,
        )
        return publisher, sink, delivery

    def test_pr5_one_bad_event_does_not_hold_up_the_rest(self) -> None:
        publisher, sink, _ = self._publisher(poison={"bad"})
        self.assertEqual(publisher.drain(), 2)
        self.assertEqual(sink.sent, ["good_1", "good_2"])
        self.assertIn("bad", publisher.last_failed)

    def test_pr5_a_flush_failure_still_rescues_the_good_events(self) -> None:
        """整批 flush 失败时，逐条隔离会把好事件救回来。

        这条才是 PR-5 的真正价值所在：broker 指认不出是哪一条坏的场合，
        不是"整批判死"，而是"逐条重试，只死真坏的那条"。
        """
        outbox = InMemoryOutbox()
        outbox.append([
            make_event("good_1"),
            make_event("bad"),
            make_event("good_2"),
        ])
        sink = FlushFailsSink(poison={"bad"})
        publisher = OutboxPublisher(
            outbox=outbox,
            publisher=sink,
            delivery=InMemoryOutboxDeliveryStore(),
        )
        self.assertEqual(publisher.drain(), 2)
        self.assertEqual(sink.sent, ["good_1", "good_2"])
        self.assertEqual(list(publisher.last_failed), ["bad"])

    def test_pr5_the_control_dead_lettering_actually_works(self) -> None:
        """控制组：判死机制本身是有效的。

        所以"只有坏的那条死了"不是因为 dead-lettering 压根没生效。
        """
        delivery = InMemoryOutboxDeliveryStore()
        now = ManualClock().now()
        delivery.claim("good_1", "a", timedelta(seconds=30), now)

        died = False
        for _ in range(3):
            died = delivery.mark_failed("good_1", "boom", max_attempts=3, now=now)
        self.assertTrue(died)
        self.assertEqual(delivery.dead_ids(), ["good_1"])

    def test_pr6_poison_is_dead_lettered_with_a_reason(self) -> None:
        publisher, _, delivery = self._publisher(poison={"bad"}, max_attempts=3)
        for _ in range(3):
            publisher.drain()

        dead = delivery.dead()
        self.assertEqual([r.event_id for r in dead], ["bad"])
        self.assertIn("broker rejected bad", dead[0].last_error or "")
        self.assertEqual(dead[0].attempts, 3)

    def test_pr6_dead_letters_do_not_stall_the_queue(self) -> None:
        """死信被排除出候选集 —— 否则它会一直占着 limit 的位置。"""
        publisher, sink, delivery = self._publisher(poison={"bad"}, max_attempts=1)
        publisher.drain()
        self.assertEqual(delivery.dead_ids(), ["bad"])

        publisher.outbox.append([make_event("brand_new")])
        self.assertEqual(publisher.drain(), 1)
        self.assertIn("brand_new", sink.sent)

    def test_pr10_a_dead_letter_does_not_come_back_on_its_own(self) -> None:
        delivery = InMemoryOutboxDeliveryStore()
        now = ManualClock().now()
        delivery.claim("bad", "a", timedelta(seconds=30), now)
        delivery.mark_failed("bad", "boom", max_attempts=1, now=now)

        self.assertFalse(delivery.claim("bad", "b", timedelta(seconds=30), now))
        self.assertTrue(delivery.reopen("bad"))                # 唯一的复活通道
        self.assertTrue(delivery.claim("bad", "b", timedelta(seconds=30), now))


# ---------------------------------------------------------------------------
# PR-1 / PR-2：生命周期
# ---------------------------------------------------------------------------


class LifecycleTest(unittest.TestCase):
    def test_pr1_stop_signal_is_checked_between_ticks(self) -> None:
        """一个 tick 是原子的：信号只在 tick 与 tick 之间被检查。"""
        outbox = InMemoryOutbox()
        outbox.append([make_event(f"evt_{i}") for i in range(6)])
        app, _ = build_app(
            outbox, FakeSink(), InMemoryOutboxDeliveryStore(),
            stop=StopAfter(2),
            config=OutboxPublisherConfig(batch_size=2),
        )
        report = app.run()
        self.assertEqual(report.reason, "signal")
        self.assertEqual(report.state, ProcessState.STOPPED)
        self.assertEqual(report.ticks, 2)
        self.assertEqual(report.work, 4)          # 两轮 × 2 条，没有半轮

    def test_pr2_shutdown_releases_the_territory_it_still_holds(self) -> None:
        delivery = InMemoryOutboxDeliveryStore()
        now = ManualClock().now()
        delivery.claim("evt_1", "me", timedelta(minutes=10), now)

        app, _ = build_app(
            InMemoryOutbox(), FakeSink(), delivery,
            config=OutboxPublisherConfig(instance_id="me"),
        )
        report = app.run(max_ticks=1)

        self.assertEqual(report.drained, 1)
        self.assertEqual(delivery.held("me", now), [])

    def test_pr2_the_control_without_the_drain_hook_it_stays_held(self) -> None:
        """控制组：不归还的话，领地确实还在 —— 所以上一条不是白测的。"""
        delivery = InMemoryOutboxDeliveryStore()
        now = ManualClock().now()
        delivery.claim("evt_1", "me", timedelta(minutes=10), now)
        self.assertEqual(delivery.held("me", now), ["evt_1"])


# ---------------------------------------------------------------------------
# PR-7：空闲退避
# ---------------------------------------------------------------------------


class BackoffTest(unittest.TestCase):
    def test_pr7_idle_backoff_is_capped(self) -> None:
        app, sleeps = build_app(
            InMemoryOutbox(), FakeSink(), InMemoryOutboxDeliveryStore(),
            config=OutboxPublisherConfig(idle_sleep=0.1, max_idle_sleep=0.5),
        )
        app.run(max_ticks=10)
        self.assertTrue(sleeps)
        self.assertLessEqual(max(sleeps), 0.5)
        self.assertEqual(max(sleeps), 0.5)        # 确实顶到了上限，不是无限涨

    def test_pr7_the_control_a_busy_process_does_not_sleep(self) -> None:
        """控制组：有活的时候一次都不睡 —— 所以上一条不是"反正都会睡"。"""
        outbox = InMemoryOutbox()
        outbox.append([make_event(f"evt_{i}") for i in range(6)])
        app, sleeps = build_app(
            outbox, FakeSink(), InMemoryOutboxDeliveryStore(),
            config=OutboxPublisherConfig(batch_size=2),
        )
        app.run(max_ticks=3)
        self.assertEqual(sleeps, [])
        self.assertEqual(app.runtime.work, 6)


# ---------------------------------------------------------------------------
# PR-8：活着 ≠ 在干活
# ---------------------------------------------------------------------------


class FailureTest(unittest.TestCase):
    def test_pr8_a_dead_broker_takes_the_process_down(self) -> None:
        outbox = InMemoryOutbox()
        outbox.append([make_event("evt_1")])
        app, _ = build_app(
            outbox, SinkDown(), InMemoryOutboxDeliveryStore(),
            config=OutboxPublisherConfig(max_consecutive_failures=3),
        )
        report = app.run(max_ticks=100)
        self.assertEqual(report.state, ProcessState.FAILED)
        self.assertEqual(report.reason, "too_many_failures")
        self.assertEqual(report.consecutive_failures, 3)

    def test_pr8_the_control_a_transient_blip_does_not_take_it_down(self) -> None:
        """控制组：失败计数会被一次成功重置 —— 上一条不是"只要出错就死"。"""
        outbox = InMemoryOutbox()
        outbox.append([make_event("evt_1")])
        app, _ = build_app(
            outbox, FlakyBroker(down_calls=2), InMemoryOutboxDeliveryStore(),
            config=OutboxPublisherConfig(max_consecutive_failures=3),
        )
        report = app.run(max_ticks=6)
        self.assertEqual(report.state, ProcessState.STOPPED)
        self.assertEqual(report.reason, "max_ticks")
        self.assertEqual(report.work, 1)          # 抖完之后还是投出去了

    def test_pr8_health_separates_alive_from_ready(self) -> None:
        runtime = ProcessRuntime(
            name="t", max_consecutive_failures=5, sleep=lambda _s: None
        )
        captured: dict = {}
        calls: list[int] = []

        def tick() -> int:
            calls.append(1)
            if len(calls) == 2:
                captured["during"] = runtime.health()
            raise RuntimeError("broker down")

        runtime.run(tick, max_ticks=2)
        self.assertTrue(captured["during"]["alive"])      # 进程还活着
        self.assertFalse(captured["during"]["ready"])     # 但没有能力干活

    def test_pr8_the_control_a_healthy_process_is_ready(self) -> None:
        runtime = ProcessRuntime(name="t", sleep=lambda _s: None)
        captured: dict = {}

        def tick() -> int:
            captured["during"] = runtime.health()
            return 1

        runtime.run(tick, max_ticks=1)
        self.assertTrue(captured["during"]["alive"])
        self.assertTrue(captured["during"]["ready"])


# ---------------------------------------------------------------------------
# PR-9：顺序
# ---------------------------------------------------------------------------


class OrderTest(unittest.TestCase):
    def test_pr9_order_follows_occurred_at_not_insertion(self) -> None:
        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        outbox = PostgresOutboxStore(conn)
        outbox.append([
            make_event("c", occurred_at=T0 + timedelta(seconds=30)),
            make_event("a", occurred_at=T0),
            make_event("b", occurred_at=T0 + timedelta(seconds=10)),
        ])
        self.assertEqual([e.event_id for e in outbox.pending()], ["a", "b", "c"])

    def test_pr9_order_survives_claim_contention(self) -> None:
        """别人先抢走 'a'，剩下两条的相对顺序仍然是 occurred_at —— 运行时不重排。

        注意机制在哪一层：`pending()` 不知道谁持有什么（它只知道死信），
        "跳过别人持有的"发生在**认领**那一步。所以断言要看投递结果，
        而不是看 `pending()` 返回了什么 —— 那会把机制记错层。
        """
        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        outbox = PostgresOutboxStore(conn)
        delivery = PostgresOutboxDeliveryStore(conn)
        outbox.append([
            make_event("c", occurred_at=T0 + timedelta(seconds=30)),
            make_event("a", occurred_at=T0),
            make_event("b", occurred_at=T0 + timedelta(seconds=10)),
        ])
        delivery.claim("a", "someone-else", timedelta(seconds=30), T0)

        sink = FakeSink()
        publisher = OutboxPublisher(
            outbox=outbox, publisher=sink, delivery=delivery, owner="me",
            clock=ManualClock(),          # 停在 T0：别人的租约还没过期
        )
        self.assertEqual(publisher.drain(), 2)
        self.assertEqual(sink.sent, ["b", "c"])   # a 被跳过，剩下的顺序不变

    def test_pr9_the_control_pending_does_not_know_who_holds_what(self) -> None:
        """控制组：`pending()` 照样把 'a' 返回来。

        所以上一条里 'a' 缺席是因为认领失败被跳过了，不是被 SQL 排除了 ——
        "排序"和"过滤"发生在两个不同的层，认错层就会写出错误的优化。
        """
        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        outbox = PostgresOutboxStore(conn)
        delivery = PostgresOutboxDeliveryStore(conn)
        outbox.append([
            make_event("c", occurred_at=T0 + timedelta(seconds=30)),
            make_event("a", occurred_at=T0),
            make_event("b", occurred_at=T0 + timedelta(seconds=10)),
        ])
        delivery.claim("a", "someone-else", timedelta(seconds=30), T0)
        self.assertEqual(
            [e.event_id for e in outbox.pending()], ["a", "b", "c"]
        )


# ---------------------------------------------------------------------------
# PG 实现
# ---------------------------------------------------------------------------


class PostgresOutboxDeliveryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(self.conn.close)
        self.store = PostgresOutboxDeliveryStore(self.conn)
        self.now = T0

    def test_the_schema_comes_from_the_migration_file(self) -> None:
        """schema 直接读 `infrastructure/postgres/006_outbox_delivery.sql` 原文。"""
        cur = self.conn.cursor()
        cur.execute("SELECT event_id FROM outbox_delivery LIMIT 1")
        self.assertEqual(cur.fetchall(), [])

    def test_claim_is_atomic_two_instances(self) -> None:
        ttl = timedelta(seconds=30)
        self.assertTrue(self.store.claim("evt_1", "a", ttl, self.now))
        self.assertFalse(self.store.claim("evt_1", "b", ttl, self.now))
        self.assertEqual(self.store.held("a", self.now), ["evt_1"])

    def test_expired_lease_can_be_taken_over(self) -> None:
        ttl = timedelta(seconds=30)
        self.assertTrue(self.store.claim("evt_1", "a", ttl, self.now))
        later = self.now + timedelta(seconds=31)
        self.assertTrue(self.store.claim("evt_1", "b", ttl, later))
        self.assertEqual(self.store.held("b", later), ["evt_1"])

    def test_pr6_a_dead_letter_must_carry_a_reason(self) -> None:
        """PR-6 钉在 DB 里：dead_at 非空 ⇒ last_error 必须非空。"""
        cur = self.conn.cursor()
        with self.assertRaises(Exception):
            cur.execute(
                "INSERT INTO outbox_delivery (event_id, dead_at) VALUES (%s, %s)",
                ("evt_no_reason", self.now),
            )

    def test_mark_sent_returns_the_territory(self) -> None:
        ttl = timedelta(seconds=30)
        self.store.claim("evt_1", "a", ttl, self.now)
        self.store.mark_sent(["evt_1"])
        self.assertIsNone(self.store.get("evt_1"))       # 表里只装在途的

    def test_mark_failed_counts_attempts_then_kills(self) -> None:
        ttl = timedelta(seconds=30)
        self.store.claim("evt_1", "a", ttl, self.now)
        self.assertFalse(
            self.store.mark_failed("evt_1", "boom", max_attempts=2, now=self.now)
        )
        self.store.claim("evt_1", "a", ttl, self.now)
        self.assertTrue(
            self.store.mark_failed("evt_1", "boom", max_attempts=2, now=self.now)
        )
        self.assertEqual(self.store.dead_ids(), ["evt_1"])
        self.assertEqual(self.store.dead()[0].last_error, "boom")

    def test_reopen_clears_the_dead_marker(self) -> None:
        ttl = timedelta(seconds=30)
        self.store.claim("evt_1", "a", ttl, self.now)
        self.store.mark_failed("evt_1", "boom", max_attempts=1, now=self.now)
        self.assertTrue(self.store.reopen("evt_1"))
        self.assertEqual(self.store.dead_ids(), [])
        self.assertFalse(self.store.reopen("never_existed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
