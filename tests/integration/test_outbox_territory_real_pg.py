"""真 PostgreSQL 上的 **outbox 投递领地**（M57 / PR-3 + PR-4）。

--------------------------------------------------------------------------
这一层要验什么（内存单测验不了的）

`tests/unit/test_outbox_publisher_app.py::TerritoryTest` 有 PR-3 的用例，
但跑在 `InMemoryOutboxDeliveryStore` 上。而源码注释写得很明白：

> PR-3：认领的原子性由**单条 SQL** 提供（UPDATE ... WHERE / INSERT ON CONFLICT）。
> 进程里"先 SELECT 看看有没有人占着，再决定要不要写"永远有窗口。

所以这是**第五条**同款保证 —— 与 S-4 / S-15 / A-11 / E-25 一样，
靠一条带条件的 UPDATE 判胜负（§94.6 记过这个模式）。

内存版是同步的，两个副本根本到不了同一时刻，
所以它那里"第二个副本一滴都没投"是一句空话。

后果很实在：认领失败 = 同一个事件被**投两次**。
Outbox 的语义是 at-least-once，但"至少一次"不该变成"只因为这个窗口就两次"。

--------------------------------------------------------------------------
"另一条连接" = 另一个副本

生产里 publisher 是多副本的（每个进程一份），各持一条连接。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from packages.execution_kernel import ManualClock
from packages.execution_kernel.adapters.postgres import (
    PostgresOutboxDeliveryStore,
    PostgresOutboxStore,
)

from ._pg import RealPostgresCase, real_pg


def _event(event_id: str, aggregate_id: str = "agg_1") -> Any:
    from packages.agent_domain.events.event import new_event

    return new_event(
        aggregate_type="compensation",
        aggregate_id=aggregate_id,
        event_type="compensation.recorded",
        payload={"event_id": event_id},
    )


class OutboxTerritoryOnRealPostgresTest(RealPostgresCase):
    """PR-3（两个副本只有一个拿到）与 PR-4（租约过期可接管）在真库上成立吗。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = ManualClock()
        self.outbox = PostgresOutboxStore(self.conn)
        #: 第二个副本：另一条连接、自己的仓库
        #
        # ⚠️ 必须 `fresh=False`：`real_pg()` 默认会重建库，
        # 那会把第一个副本刚写的东西全清掉，于是第二个副本因为
        # "队列本来就是空的"而投了 0 条 —— 断言通过，但什么都没验（假绿）。
        self.replica_b = real_pg(fresh=False)
        self.addCleanup(self.replica_b.close)
        self.delivery_a = PostgresOutboxDeliveryStore(self.conn)
        self.delivery_b = PostgresOutboxDeliveryStore(self.replica_b)

    def test_pr3_two_replicas_only_one_claims_the_same_event(self):
        """两个副本认领同一个事件 —— 只有一个成功。"""
        self.outbox.append([_event("evt_pr3")])
        ttl = timedelta(seconds=30)
        now = self.clock.now()

        self.assertTrue(
            self.delivery_a.claim("evt_pr3", "replica-a", ttl, now),
            "第一个副本必须拿到",
        )
        self.assertFalse(
            self.delivery_b.claim("evt_pr3", "replica-b", ttl, now),
            "第二个副本必须落空 —— 否则这个事件会被投两次",
        )

    def test_pr3_the_control_the_queue_is_not_empty(self):
        """控制组：队列里确实有东西。

        没有这条，"第二个副本拿到 0 条"可能只是因为队列本来就是空的。
        """
        self.outbox.append([_event("evt_ctrl")])
        pending = list(self.outbox.pending())
        self.assertEqual(len(pending), 1, "队列里必须有东西，否则上一条断言没意义")

    def test_pr4_an_unexpired_lease_is_still_held(self):
        """PR-4：租约没过期就不许被抢 —— 否则正在投递的事件会被别人接手。"""
        ttl = timedelta(seconds=30)
        now = self.clock.now()
        self.assertTrue(self.delivery_a.claim("evt_pr4", "a", ttl, now))
        self.assertFalse(
            self.delivery_b.claim("evt_pr4", "b", ttl, now), "没过期不许被抢"
        )

    def test_pr4_an_expired_lease_can_be_taken_over(self):
        """PR-4 的另一半：租约**过期**后必须能被接管。

        进程被 SIGKILL 时没有机会归还领地 —— 过期是唯一的安全网。
        两边都得成立：没过期不许抢（上一条），过期了必须能抢（这一条）。
        """
        ttl = timedelta(seconds=30)
        now = self.clock.now()
        self.assertTrue(self.delivery_a.claim("evt_pr4b", "a", ttl, now))

        later = now + timedelta(seconds=31)          # 超过 ttl
        self.assertTrue(
            self.delivery_b.claim("evt_pr4b", "b", ttl, later),
            "过期了就必须能被接管，否则这条事件永远没人投",
        )


if __name__ == "__main__":
    unittest.main()
