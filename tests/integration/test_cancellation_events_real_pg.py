"""真 PostgreSQL 上的 **取消意图事件**（M66 / 补 M47 的覆盖缺口）。

--------------------------------------------------------------------------
为什么补这一层

M47 给 `run_cancellations` 加了三个事件（requested / settled / abandoned），
并有 9 条单测 —— 但跑在 `InMemoryRunCancellationStore` 上。

按本会话的主线（S-4 / S-15 / A-11 / E-25 / PR-3 五条保证都在真库上验过），
这一条也不该只停在内存。真库上要验的是：

  1. 三次生命周期变化**真的在 outbox 里留下三条事件**
  2. `payload` 是**真 JSONB**，带归因（reason / by）—— 下游按它聚合
  3. 从**另一条连接**读得到（不是读到自己的内存）

--------------------------------------------------------------------------
为什么"另一条连接"是硬要求

同一个连接读自己刚写的，读到的是自己的内存 —— 那条断言什么也证明不了。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from packages.agent_runtime.adapters.postgres import (
    PostgresRunCancellationStore,
)
from packages.execution_kernel.adapters.postgres import PostgresOutboxStore

from ._pg import RealPostgresCase, real_pg

RUN = "run_cancel_evt"


class CancellationEventsOnRealPostgresTest(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        # grace=0：写入即到期，于是 abandon 那条路径也走得到
        self.store = PostgresRunCancellationStore(
            self.conn, grace=timedelta(0), events=PostgresOutboxStore(self.conn)
        )
        self.witness = real_pg(fresh=False)
        self.addCleanup(self.witness.close)

    def _events(self) -> list[Any]:
        return self.witness.execute(
            "SELECT event_type, aggregate_type, aggregate_id, payload"
            "  FROM outbox_events ORDER BY occurred_at, event_id"
        ).fetchall()

    def test_request_then_settle_leaves_two_events(self):
        """那条 Run 确实停了 → requested + settled。"""
        self.store.request(RUN, reason="user asked", by="alice")
        self.store.settle(RUN)
        self.assertEqual(
            [r["event_type"] for r in self._events()],
            ["cancellation.requested", "cancellation.settled"],
        )

    def test_request_then_abandon_leaves_two_events(self):
        """等到上限也没结局 → requested + abandoned。"""
        self.store.request(RUN, reason="user asked", by="alice")
        self.store.abandon(RUN)
        self.assertEqual(
            [r["event_type"] for r in self._events()],
            ["cancellation.requested", "cancellation.abandoned"],
        )

    def test_a_settled_intent_is_not_abandoned_afterwards(self):
        """R-8 / R-11：已经结掉的意图**不复活** —— 于是不该有 abandoned 那条。

        这条是写测试时从真库上学到的（我最初的期望写错了）：
        `settle` 之后再 `abandon`，abandon 返回 False，不发事件。
        """
        self.store.request(RUN, reason="r", by="b")
        self.store.settle(RUN)
        self.assertFalse(self.store.abandon(RUN), "已结掉就不该再被放弃")
        self.assertEqual(
            [r["event_type"] for r in self._events()],
            ["cancellation.requested", "cancellation.settled"],
        )

    def test_the_payload_carries_the_attribution(self):
        """B-8 的另一半：事件里要能看出**谁**要求停的、**为什么**。"""
        self.store.request(RUN, reason="user asked", by="alice")
        row = self._events()[0]
        payload = row["payload"]
        self.assertEqual(payload["reason"], "user asked")
        self.assertEqual(payload["by"], "alice")
        self.assertEqual(payload["run_id"], RUN)

    def test_aggregate_is_the_cancellation_not_the_run(self):
        """下游按 aggregate 聚合 —— 挂错 aggregate，事件就找不到了。"""
        self.store.request(RUN, reason="r", by="b")
        row = self._events()[0]
        self.assertEqual(row["aggregate_type"], "cancellation")
        self.assertEqual(row["aggregate_id"], RUN)

    def test_a_second_request_does_not_emit_again(self):
        """已经结掉的意图再请求：不复活（R-8），也不该再发一条事件。"""
        self.store.request(RUN, reason="r", by="b")
        self.store.settle(RUN)
        before = len(self._events())

        self.store.request(RUN, reason="again", by="c")
        self.assertEqual(len(self._events()), before, "不复活就不该再发事件")
