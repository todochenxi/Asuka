"""Redis Adapter（阶段 6）。

Redis 在 AgentOS 里**只有一个身份：快路径**。
它不是事实来源，也不是"第二份 Truth"。这句话必须能被代码证明，所以本模块遵循三条硬规则：

    1. 每个类都只做加速，不做裁决。裁决权永远在 PG。
    2. 每个类都必须能被**丢弃后重建**。Redis 全丢（重启 / 驱逐 / 网络分区）
       只允许造成"变慢"，不允许造成"变错"。
    3. 读不到 ≠ 没有。`get()` 返回空一律按 **UNKNOWN** 处理，回落到 PG 或回查下游。

对应三处加速：

    LeaseIndex          Recovery Controller 不用每轮全表扫 PG（ZSET 按到期时间排序）
    CancelSignalStore   取消意图的低延迟通知（事实来源是 PG 的 cancellation_requested）
    IdempotencyStore    外部副作用结果的缓存（真正的幂等保证在下游）

客户端是 duck-typed 的 redis-py 兼容对象；本模块不 import 任何 Redis 库。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from packages.agent_domain.execution import ExecutionStatus, Lease

DEFAULT_PREFIX = "agentos:v1"


def _epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class RedisCancelSignalStore:
    """取消意图的**快速信号**。

    X-11：这里写的不是"已取消"，只是"有人请求取消"。
    真正的取消由 Kernel 改 PG 里的状态；信号丢了 PG 还在（反过来不成立）。
    """

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        default_ttl: int = 3600,
    ) -> None:
        self.client = client
        self.prefix = prefix
        self.default_ttl = default_ttl

    def _key(self, execution_id: str) -> str:
        return f"{self.prefix}:cancel:{execution_id}"

    def set(self, execution_id: str, ttl_seconds: int | None = None) -> None:
        self.client.set(self._key(execution_id), "1", ex=ttl_seconds or self.default_ttl)

    def get(self, execution_id: str) -> bool:
        return self.client.get(self._key(execution_id)) is not None

    def clear(self, execution_id: str) -> None:
        self.client.delete(self._key(execution_id))


class RedisIdempotencyStore:
    """幂等结果缓存。

    `put` 用 `SET NX`：只认**第一次**写入，并发下后来者写不进去（也就不该覆盖首次结果）。
    `get` 返回 None = UNKNOWN，调用方必须回查，不能当成"没执行过"去重试。
    """

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        default_ttl: int = 86400,
    ) -> None:
        self.client = client
        self.prefix = prefix
        self.default_ttl = default_ttl

    def _key(self, key: str) -> str:
        return f"{self.prefix}:idem:{key}"

    def get(self, key: str) -> Mapping[str, Any] | None:
        raw = self.client.get(self._key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def put(self, key: str, value: Mapping[str, Any], ttl_seconds: int | None = None) -> bool:
        """返回是否抢到了首次写入权（False = 已存在，本次是重复调用）。"""
        ok = self.client.set(
            self._key(key),
            json.dumps(value, ensure_ascii=False),
            nx=True,
            ex=ttl_seconds or self.default_ttl,
        )
        return bool(ok)

    def forget(self, key: str) -> None:
        self.client.delete(self._key(key))


class RedisLeaseIndex:
    """Lease 到期索引（ZSET：member = execution_id，score = expires_at 的 epoch 秒）。

    它回答"谁的 Lease 快到期了"，**不回答**"谁持有 Lease" —— 后者只有 PG 说了算。
    所以 Recovery Controller 拿到 due 列表后，仍然要回 PG 复核一次再 mark_stale。
    """

    def __init__(self, client: Any, *, prefix: str = DEFAULT_PREFIX) -> None:
        self.client = client
        self.prefix = prefix

    @property
    def key(self) -> str:
        return f"{self.prefix}:lease:expiry"

    def track(self, lease: Lease) -> None:
        """登记 / 续期：ZADD 按 member 覆盖，天然幂等。"""
        self.client.zadd(self.key, {lease.execution_id: _epoch(lease.expires_at)})

    def forget(self, execution_id: str) -> None:
        self.client.zrem(self.key, execution_id)

    def due(self, now: datetime, limit: int = 100) -> Sequence[str]:
        raw = self.client.zrangebyscore(self.key, "-inf", _epoch(now), start=0, num=limit)
        out: list[str] = []
        for member in raw or []:
            out.append(member.decode() if isinstance(member, bytes) else str(member))
        return out

    def clear(self) -> None:
        self.client.delete(self.key)


def rebuild_lease_index(
    index: RedisLeaseIndex,
    repository: Any,
    *,
    limit: int = 1000,
) -> int:
    """从 PG 重建 Redis 索引 —— Redis 全丢也不影响正确性，这就是证据。

    只有 RUNNING 且带 Lease 的 Execution 需要进索引。
    """
    index.clear()
    tracked = 0
    for execution in repository.list_by_status(ExecutionStatus.RUNNING, limit=limit):
        if execution.lease is not None:
            index.track(execution.lease)
            tracked += 1
    return tracked


def now_epoch() -> float:
    """显式暴露时间来源，方便测试注入 —— 不在适配层偷偷取系统时间。"""
    return time.time()
