"""Redis 的最小内存替身（只实现 Adapter 真正用到的那几条命令）。

目的和 `sqlite_shim.py` 一样：在没有真实 Redis 的机器上验证**语义**，
尤其是"Redis 丢了之后系统还对不对"这类不变量。

实现了：SET（NX / EX）、GET、DELETE、ZADD、ZRANGEBYSCORE、ZREM、FLUSHALL。
TTL 在读取时惰性判定，时间来源可注入。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping


class FakeRedis:
    def __init__(self, *, time_fn: Callable[[], float] = time.time) -> None:
        self._strings: dict[str, tuple[str, float | None]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._time_fn = time_fn
        self.commands: list[str] = []

    # ------------------------------------------------------------------ string
    def set(
        self,
        name: str,
        value: Any,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        self.commands.append("SET")
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode()
        alive = self._get(name)
        if nx and alive is not None:
            return False                      # 已存在 → 抢不到首次写入权
        expire_at = (self._time_fn() + ex) if ex else None
        self._strings[name] = (str(value), expire_at)
        return True

    def _get(self, name: str) -> str | None:
        item = self._strings.get(name)
        if item is None:
            return None
        value, expire_at = item
        if expire_at is not None and self._time_fn() >= expire_at:
            del self._strings[name]           # 惰性过期
            return None
        return value

    def get(self, name: str) -> str | None:
        self.commands.append("GET")
        return self._get(name)

    def delete(self, *names: str) -> int:
        self.commands.append("DEL")
        removed = 0
        for name in names:
            if self._strings.pop(name, None) is not None:
                removed += 1
            if self._zsets.pop(name, None) is not None:
                removed += 1
        return removed

    def exists(self, name: str) -> int:
        return 1 if (self._get(name) is not None or name in self._zsets) else 0

    # -------------------------------------------------------------------- zset
    def zadd(self, name: str, mapping: Mapping[str, float]) -> int:
        self.commands.append("ZADD")
        zset = self._zsets.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = float(score)       # 按 member 覆盖 → 天然幂等
        return added

    def zrem(self, name: str, *members: str) -> int:
        self.commands.append("ZREM")
        zset = self._zsets.get(name)
        if zset is None:
            return 0
        removed = 0
        for member in members:
            if zset.pop(member, None) is not None:
                removed += 1
        return removed

    def zrangebyscore(
        self,
        name: str,
        min_score: Any,
        max_score: Any,
        *,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        self.commands.append("ZRANGEBYSCORE")
        zset = self._zsets.get(name)
        if not zset:
            return []
        low = float("-inf") if min_score == "-inf" else float(min_score)
        high = float("inf") if max_score == "+inf" else float(max_score)
        members = [m for m, s in sorted(zset.items(), key=lambda kv: (kv[1], kv[0])) if low <= s <= high]
        if start is not None:
            members = members[start:]
        if num is not None:
            members = members[:num]
        return members

    def zscore(self, name: str, member: str) -> float | None:
        return self._zsets.get(name, {}).get(member)

    def zcard(self, name: str) -> int:
        return len(self._zsets.get(name, {}))

    # ------------------------------------------------------------------- misc
    def flushall(self) -> None:
        """模拟 Redis 整个挂掉 / 数据被驱逐。"""
        self.commands.append("FLUSHALL")
        self._strings.clear()
        self._zsets.clear()

    def __len__(self) -> int:
        return len(self._strings) + len(self._zsets)
