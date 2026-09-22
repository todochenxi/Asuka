"""阶段 6：Redis Adapter 的语义测试。

这一阶段的测试重点**不是**"Redis 能不能存"，而是那句架构断言能不能被证明：

> Redis 只是快路径，不是事实来源。Redis 全丢只允许造成"变慢"，不允许造成"变错"。

所以每个能力都配一条"丢了之后会怎样"的用例：

    CancelSignal    信号丢了 → 从 PG 的 cancellation_requested 读回来，仍然取消得成
    LeaseIndex      索引丢了 → 从 PG 的 executions 重建，Recovery 结果不变
    Idempotency     缓存丢了 → get() 返回 UNKNOWN，调用方必须回查，不能盲重试
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import ExecutionStatus, Lease
from packages.execution_kernel.adapters.postgres import (
    PostgresExecutionRepository,
    PostgresOutboxStore,
)
from packages.execution_kernel.adapters.redis import (
    RedisCancelSignalStore,
    RedisIdempotencyStore,
    RedisLeaseIndex,
    rebuild_lease_index,
)
from packages.execution_kernel.cancellation import CancellationService
from packages.execution_kernel.inmemory import InMemoryOutbox, ManualClock
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.recovery_controller import RecoveryController

from .fake_redis import FakeRedis
from .helpers import make_task
from .sqlite_shim import connect

T0 = ManualClock().now()


class _Clock:
    """可控的 epoch 时间，用来测 TTL。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class CancelSignalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.client = FakeRedis(time_fn=self.clock)
        self.signals = RedisCancelSignalStore(self.client, default_ttl=60)

    def test_set_get_clear(self) -> None:
        self.assertFalse(self.signals.get("exe_1"))
        self.signals.set("exe_1")
        self.assertTrue(self.signals.get("exe_1"))
        self.signals.clear("exe_1")
        self.assertFalse(self.signals.get("exe_1"))

    def test_signal_expires_by_ttl(self) -> None:
        """信号是有时效的通知：过期即视为没发过（PG 才是事实来源）。"""
        self.signals.set("exe_1", ttl_seconds=60)
        self.assertTrue(self.signals.get("exe_1"))
        self.clock.advance(61)
        self.assertFalse(self.signals.get("exe_1"))

    def test_keys_are_namespaced(self) -> None:
        self.signals.set("exe_1")
        self.assertIn("agentos:v1:cancel:exe_1", self.client._strings)


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.client = FakeRedis(time_fn=self.clock)
        self.store = RedisIdempotencyStore(self.client, default_ttl=3600)

    def test_first_write_wins_and_value_roundtrips(self) -> None:
        self.assertTrue(self.store.put("k1", {"txn": "txn_1", "status": "ok"}))
        self.assertEqual(self.store.get("k1"), {"txn": "txn_1", "status": "ok"})
        # 第二次调用写不进去（NX），也不会覆盖首次结果
        self.assertFalse(self.store.put("k1", {"txn": "txn_2"}))
        self.assertEqual(self.store.get("k1")["txn"], "txn_1")

    def test_missing_means_unknown_not_never(self) -> None:
        """get() 返回 None = UNKNOWN。文档约定：必须回查，不能当"没执行过"。"""
        self.assertIsNone(self.store.get("never-seen"))
        self.client.flushall()                       # 缓存整个丢掉
        self.assertIsNone(self.store.get("k1"))      # 仍然只能得到 UNKNOWN

    def test_ttl_expiry_returns_unknown(self) -> None:
        self.store.put("k1", {"ok": True}, ttl_seconds=10)
        self.clock.advance(11)
        self.assertIsNone(self.store.get("k1"))

    def test_forget(self) -> None:
        self.store.put("k1", {"ok": True})
        self.store.forget("k1")
        self.assertIsNone(self.store.get("k1"))


class LeaseIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeRedis()
        self.index = RedisLeaseIndex(self.client)
        self.now = T0

    def _lease(self, execution_id: str, expires_in: float) -> Lease:
        return Lease(
            execution_id=execution_id,
            attempt_no=1,
            worker_id="w1",
            fencing_token=1,
            acquired_at=self.now,
            expires_at=self.now + timedelta(seconds=expires_in),
            heartbeat_at=self.now,
        )

    def test_track_and_due(self) -> None:
        self.index.track(self._lease("exe_1", 30))
        self.index.track(self._lease("exe_2", 120))

        self.assertEqual(self.index.due(self.now), [])
        self.assertEqual(self.index.due(self.now + timedelta(seconds=31)), ["exe_1"])
        self.assertEqual(
            self.index.due(self.now + timedelta(seconds=200)), ["exe_1", "exe_2"]
        )

    def test_track_is_idempotent_and_renewable(self) -> None:
        self.index.track(self._lease("exe_1", 30))
        self.index.track(self._lease("exe_1", 300))   # 续期：覆盖 score
        self.assertEqual(self.index.due(self.now + timedelta(seconds=60)), [])

    def test_forget_removes_entry(self) -> None:
        self.index.track(self._lease("exe_1", 30))
        self.index.forget("exe_1")
        self.assertEqual(self.index.due(self.now + timedelta(hours=1)), [])

    def test_limit_is_respected(self) -> None:
        for i in range(5):
            self.index.track(self._lease(f"exe_{i}", 10))
        self.assertEqual(len(self.index.due(self.now + timedelta(hours=1), limit=2)), 2)


class RedisLossTest(unittest.TestCase):
    """核心断言：Redis 全丢之后，系统只是变慢，不会变错。"""

    def setUp(self) -> None:
        self.conn = connect()
        self.clock = ManualClock()
        self.client = FakeRedis()
        self.repo = PostgresExecutionRepository(self.conn)
        self.cancel = RedisCancelSignalStore(self.client)
        self.index = RedisLeaseIndex(self.client)
        self.kernel = ExecutionKernel(
            repository=self.repo,
            outbox=PostgresOutboxStore(self.conn),
            clock=self.clock,
            lease_index=self.index,
            cancel_signals=self.cancel,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _running(self, ttl: int = 30) -> str:
        execution = self.kernel.submit(make_task())
        _, lease = self.kernel.claim(execution.execution_id, worker_id="w1",
                                     ttl=timedelta(seconds=ttl))
        self.last_lease = lease
        return execution.execution_id

    def test_lease_index_is_synced_from_pg(self) -> None:
        execution_id = self._running()
        self.assertEqual(self.index.due(self.clock.now()), [])
        self.clock.advance(timedelta(seconds=31))
        self.assertEqual(self.index.due(self.clock.now()), [execution_id])

    def test_index_is_rebuildable_after_total_loss(self) -> None:
        first = self._running(30)
        second = self._running(300)
        self.assertEqual(self.client.zcard(self.index.key), 2)

        self.client.flushall()                        # Redis 整个没了
        self.assertEqual(self.index.due(self.clock.now()), [])
        self.assertEqual(self.client.zcard(self.index.key), 0)

        self.clock.advance(timedelta(seconds=31))
        self.assertEqual(self.index.due(self.clock.now()), [])

        tracked = rebuild_lease_index(self.index, self.repo)
        self.assertEqual(tracked, 2)
        self.assertEqual(self.index.due(self.clock.now()), [first])
        self.assertEqual(
            self.index.due(self.clock.now() + timedelta(seconds=300)), [first, second]
        )

    def test_recovery_finds_same_targets_with_and_without_index(self) -> None:
        """索引只是加速器：走索引和全表扫 PG 必须得到同一个结果。"""
        execution_id = self._running(30)
        self.clock.advance(timedelta(seconds=31))

        via_index = RecoveryController(self.kernel).scan()
        self.assertEqual(via_index, [execution_id])

        # 回到 PENDING/RUNNING 再走一次 PG 全表扫
        self.kernel.recover(execution_id, ttl=timedelta(seconds=30))
        self.clock.advance(timedelta(seconds=31))
        self.kernel.lease_index = None                 # 强制退化
        via_pg = RecoveryController(self.kernel).scan()
        self.assertEqual(via_pg, [execution_id])

    def test_recovery_still_works_after_redis_loss(self) -> None:
        """Redis 丢了 → 索引扫不到 → PG 周期兜底扫接住。晚发现可以，漏掉不行。"""
        execution_id = self._running(30)
        self.clock.advance(timedelta(seconds=31))
        self.client.flushall()

        # 只走索引：扫不到（这正是"丢了会变慢"）
        self.assertEqual(RecoveryController(self.kernel).scan(), [])

        # run_once 的周期性 PG 兜底扫：仍然救得回来
        result = RecoveryController(self.kernel, sweep_every=1).run_once()
        self.assertEqual(result["stale"], [execution_id])
        self.assertEqual(result["recovered"], [execution_id])
        self.assertEqual(self.kernel.status_of(execution_id), ExecutionStatus.RUNNING)

    def test_sweep_is_independent_of_redis(self) -> None:
        """sweep() 是纯 PG 路径，Redis 在不在都一样。"""
        execution_id = self._running(30)
        self.clock.advance(timedelta(seconds=31))
        self.client.flushall()
        self.assertEqual(
            RecoveryController(self.kernel).sweep(), [execution_id]
        )

    def test_stale_index_entry_is_cleaned_up(self) -> None:
        execution_id = self._running(30)
        self.clock.advance(timedelta(seconds=31))
        # 索引里塞一个 PG 中根本不存在的脏条目
        ghost_acquired = self.clock.now() - timedelta(seconds=10)
        self.index.track(
            Lease(execution_id="exe_ghost", attempt_no=1, worker_id="w", fencing_token=1,
                  acquired_at=ghost_acquired, expires_at=ghost_acquired + timedelta(seconds=5),
                  heartbeat_at=ghost_acquired)
        )
        stale = RecoveryController(self.kernel).scan()
        self.assertEqual(stale, [execution_id])
        self.assertIsNone(self.client.zscore(self.index.key, "exe_ghost"))

    def test_cancellation_survives_signal_loss(self) -> None:
        """取消的事实来源是 PG，不是 Redis。"""
        execution_id = self._running(30)
        service = CancellationService(self.kernel)

        service.request(execution_id, reason="stop", by="alice")
        self.assertTrue(self.cancel.get(execution_id))          # 快路径命中

        self.client.flushall()                                  # 信号丢了
        self.assertTrue(service.is_requested(execution_id))      # 仍然成立（回落到 PG）

        persisted = self.repo.get(execution_id)
        assert persisted is not None
        self.assertTrue(persisted.cancellation_requested)

    def test_terminal_execution_is_dropped_from_index(self) -> None:
        execution_id = self._running(30)
        self.kernel.complete(execution_id, token=self.last_lease.fencing_token)
        # 完成后 Lease 释放 → 不该再出现在到期索引里
        self.assertEqual(self.index.due(self.clock.now() + timedelta(hours=1)), [])


class CancelSignalFallbackTest(unittest.TestCase):
    """没有 Redis 也能取消 —— 只是慢一点（每次回落到 PG）。"""

    def test_without_signal_store(self) -> None:
        outbox = InMemoryOutbox()
        kernel = ExecutionKernel(
            repository=_FakeRepo(),
            outbox=outbox,
            clock=ManualClock(),
        )
        execution = kernel.submit(make_task())
        service = CancellationService(kernel)
        service.request(execution.execution_id, reason="stop", by="alice")
        self.assertTrue(service.is_requested(execution.execution_id))


class _FakeRepo:
    """最小 ExecutionRepository：只为验证"没有 Redis 也能工作"。"""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def add(self, execution) -> None:
        self._data[execution.execution_id] = execution

    def get(self, execution_id: str):
        return self._data.get(execution_id)

    def get_by_task(self, task_id: str):
        for e in self._data.values():
            if e.task_id == task_id:
                return e
        return None

    def save(self, execution, expected_version: int | None = None) -> None:
        self._data[execution.execution_id] = execution

    def list_by_status(self, status, limit: int = 100):
        return [e for e in self._data.values() if e.status is status][:limit]

    def list_with_expired_lease(self, now, limit: int = 100):
        return []


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
