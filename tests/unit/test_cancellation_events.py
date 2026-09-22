"""M47 / 空洞 225（修正版）：run_cancellations 表上没有事件流。

--------------------------------------------------------------------------
与 M40 是同一族病

M40 给 `compensations` 表补了事件流（X-15），理由是：

    PG 里的事实改了，而事件流这条事实没有改 ——
    按 X-3 的判据，是"两件事实不同事务"的同一族错误。

`run_cancellations` 这张表现在一模一样：
一条取消意图被**请求**、被**结掉**（那条 Run 确实停了）、
被**放弃**（我们不等了）—— 下游（Read Model / 审计 / 看板）
一个字都看不到，因为 Kafka 那边从来没有对应的消息。

--------------------------------------------------------------------------
为什么是三个而不是一个 `cancellation.changed`

和 compensation 一样（见 event.py 里那段注释）：

    requested   有人要停它          → 取消通道收到一条新请求
    settled     那条 Run 确实停了   → 正常收尾，账清了
    abandoned   我们不知道它停没停   → 要人去看，账上记着"未知"

三种变化要的人不一样、要去做的事也不一样。
合成一个类型之后，这个区别只能在 payload 里找，
而没有人会去找。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.events.event import (
    CANCELLATION_REQUESTED,
    CANCELLATION_SETTLED,
    CANCELLATION_ABANDONED,
)
from packages.agent_runtime.cancellation import InMemoryRunCancellationStore


class _RecordingSink:
    """记下每次 append 传进来的事件，供断言。"""

    def __init__(self):
        self.events: list = []

    def append(self, events):
        self.events.extend(events)


class TestCancellationEvents(unittest.TestCase):
    """三条写路径都必须发出对应的事件。"""

    def test_request_emits_cancellation_requested(self):
        """request() → cancellation.requested。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        self.assertEqual(len(sink.events), 1)
        self.assertEqual(sink.events[0].event_type, CANCELLATION_REQUESTED)
        self.assertEqual(sink.events[0].aggregate_type, "cancellation")
        self.assertEqual(sink.events[0].aggregate_id, "run-1")

    def test_settle_emits_cancellation_settled(self):
        """settle() → cancellation.settled。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.settle("run-1")
        # request 1 条 + settle 1 条
        self.assertEqual(len(sink.events), 2)
        self.assertEqual(sink.events[1].event_type, CANCELLATION_SETTLED)

    def test_abandon_emits_cancellation_abandoned(self):
        """abandon() → cancellation.abandoned。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.abandon("run-1")
        # request 1 条 + abandon 1 条
        self.assertEqual(len(sink.events), 2)
        self.assertEqual(sink.events[1].event_type, CANCELLATION_ABANDONED)

    def test_no_events_when_sink_is_none(self):
        """不传 sink = 不发事件（显式选择，不是报错）。"""
        store = InMemoryRunCancellationStore(events=None)
        # 不抛就行
        store.request("run-1", reason="test", by="alice")
        store.settle("run-1")

    def test_duplicate_request_does_not_emit(self):
        """已 settled/abandoned 的意图再 request 不复活，也不发事件。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.settle("run-1")
        n = len(sink.events)
        # 再 request 一次 —— 走到 "已经结掉不复活" 那条路
        store.request("run-1", reason="again", by="bob")
        self.assertEqual(len(sink.events), n, "已结掉的意图再 request 不该发事件")

    def test_settle_idempotent_no_event(self):
        """对已经 settled 的意图再 settle 不发事件。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.settle("run-1")
        n = len(sink.events)
        ok = store.settle("run-1")
        self.assertFalse(ok)
        self.assertEqual(len(sink.events), n, "重复 settle 不该发事件")

    def test_abandon_idempotent_no_event(self):
        """对已经 abandoned 的意图再 abandon 不发事件。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.abandon("run-1")
        n = len(sink.events)
        ok = store.abandon("run-1")
        self.assertFalse(ok)
        self.assertEqual(len(sink.events), n, "重复 abandon 不该发事件")

    def test_event_payload_has_reason_and_by(self):
        """事件 payload 里必须带 reason / by —— 审计要能不看 PG 就知道。"""
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="user asked", by="alice")
        p = dict(sink.events[0].payload)
        self.assertEqual(p["reason"], "user asked")
        self.assertEqual(p["by"], "alice")
        self.assertEqual(p["run_id"], "run-1")

    def test_settle_after_abandon_emits_event(self):
        """R-14：放弃后如果确实停了，仍可 settle（两件独立的事）。

        settled_at 与 abandoned_at 可以同时有值，
        所以 settle 在 abandon 之后仍然应该成功并发事件。
        """
        sink = _RecordingSink()
        store = InMemoryRunCancellationStore(events=sink)
        store.request("run-1", reason="test", by="alice")
        store.abandon("run-1")
        # R-14：放弃之后仍然可以 settle
        ok = store.settle("run-1")
        self.assertTrue(ok)
        self.assertEqual(sink.events[-1].event_type, CANCELLATION_SETTLED)


if __name__ == "__main__":
    unittest.main()
