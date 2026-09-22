"""Harness 自己的端口。

**为什么不复用 `execution_kernel.ports.Clock`？**

因为那会在 Harness 与 Kernel 之间拉出一条 import 依赖，
而基线 §2 钉死的是「Harness 不侵入 Execution Kernel」。
依赖一个 Protocol 虽然不等于侵入，但会让这条边界在代码里**看不出来**。

这里定义的 `Clock` 是结构化类型（Protocol），
`execution_kernel` 的 `SystemClock` / `ManualClock` 天然满足它，
所以运行时可以混用，**编译期却没有任何耦合**。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """时间可推进的时钟 —— 审批超时 / 预算窗口这类测试全靠它。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> datetime:
        self.current = self.current + delta
        return self.current
