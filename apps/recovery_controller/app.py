"""recovery_controller 进程（M21）。

这个进程身上唯一值得单独写一段话的，是**控制器实例必须跨 tick 存活**（PR-11）。

`RecoveryController.sweep_every` 的节奏计数在控制器对象里：每 N 轮额外做一次
PG 全量兜底扫。如果进程每轮新建一个控制器，`ticks` 永远是 1，`ticks % N`
永远不是 0 —— **PG 安全网一次都不会触发**。

而那张安全网正是"Redis 全丢也只允许变慢、不允许变错"的兑现方式：
索引没了，STALE 最晚在第 N 轮被 PG 扫出来；安全网没了，STALE 可能永远
躺在库里没人管。所以这不是"性能调优"，是**把兜底路径做成了死代码**。

    ❌ 每轮重建：  tick 1 → ticks=1 → 1 % 10 ≠ 0 → 不扫
    ✅ 跨轮持有：  tick 10 → ticks=10 → 10 % 10 = 0 → 扫

同理，这类进程**不需要领地**（PR-12）：`mark_stale` / `recover` 都走状态机，
多副本重复调用不会二次生效。领地是为"投一次本身就是副作用"的场合准备的
（见 `apps/outbox_publisher`），不是所有后台进程的标配。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock
from packages.execution_kernel.recovery_controller import RecoveryController

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)

if TYPE_CHECKING:  # pragma: no cover
    from packages.execution_kernel.kernel import ExecutionKernel


@dataclass
class RecoveryControllerConfig:
    worker_id: str = field(
        default_factory=lambda: f"recovery-controller-{os.getpid()}"
    )
    sweep_every: int = 10
    """每 N 轮做一次 PG 全量兜底扫（Redis 是快路径，PG 是安全网）。"""
    idle_sleep: float = 1.0
    max_idle_sleep: float = 10.0
    error_sleep: float = 2.0
    max_consecutive_failures: int = 5


class RecoveryControllerApp:
    """`python -m apps.recovery_controller` 跑的就是它。"""

    def __init__(
        self,
        *,
        kernel: "ExecutionKernel",
        config: RecoveryControllerConfig | None = None,
        controller: RecoveryController | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。不传就**没有**事务边界 ——
        #: 组合根必须显式接上，否则这个进程的写在 DB 意义上不是原子的。
        uow: Any = None,
    ) -> None:
        self.config = config or RecoveryControllerConfig()
        self.clock = clock or SystemClock()
        # PR-11：控制器在构造时建好，之后每一轮复用同一个对象。
        self.controller = controller or RecoveryController(
            kernel=kernel,
            worker_id=self.config.worker_id,
            sweep_every=self.config.sweep_every,
        )
        self.runtime = ProcessRuntime(
            name="recovery_controller",
            signal=signal or ManualStop(),
            clock=self.clock,
            idle_sleep=self.config.idle_sleep,
            max_idle_sleep=self.config.max_idle_sleep,
            error_sleep=self.config.error_sleep,
            max_consecutive_failures=self.config.max_consecutive_failures,
            sleep=sleep or time.sleep,
            uow=uow,
        )

    def tick(self) -> int:
        result = self.controller.run_once()
        return len(result["stale"]) + len(result["recovered"])

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        return self.runtime.health()
