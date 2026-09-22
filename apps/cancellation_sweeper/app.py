"""cancellation_sweeper 进程（M21）。

这个进程是 **PR-12 最干净的样本**：它**没有领地，也不需要领地**。

`apps/outbox_publisher` 必须认领，因为"投一次"本身就是副作用 ——
三个副本各投一遍，Kafka 里就躺着三份。而 `sweep()` 的动作是**收敛**：

    kernel.cancel(execution_id)   →  走状态机，已终态就是 no-op

多副本并发扫同一批，结果完全一样，不会多取消一次，也不会少取消一次。
所以给它加领地只会平白引入一个租约要调、一个归还路径要测，
换不来任何正确性。

判据不是"要不要多副本部署"，而是：

    这个动作**重复做**会不会改变结果？
        会 → 需要领地（投递）
        不会 → 不需要（收敛）

把这条写反的代价是两个方向的：该认领的没认领 → 副作用放大 N 倍；
不该认领的认领了 → 多一个租约过期就多一条静默卡住的路径。

另一个要说的点是**节奏**：协作式取消优先，Sweeper 只兜底。
所以它比 recovery_controller 更慢一点也没关系 —— 但也不能太慢，
"用户点了取消却一直显示 RUNNING"是最难解释的事故之一。
默认 5s 一轮是折中，不是算出来的。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from packages.execution_kernel.cancellation import CancellationService
from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)

if TYPE_CHECKING:  # pragma: no cover
    from packages.execution_kernel.kernel import ExecutionKernel


@dataclass
class CancellationSweeperConfig:
    idle_sleep: float = 5.0
    max_idle_sleep: float = 30.0
    error_sleep: float = 2.0
    max_consecutive_failures: int = 5


class CancellationSweeperApp:
    """`python -m apps.cancellation_sweeper` 跑的就是它。"""

    def __init__(
        self,
        *,
        kernel: "ExecutionKernel",
        config: CancellationSweeperConfig | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。不传就**没有**事务边界 ——
        #: 组合根必须显式接上，否则这个进程的写在 DB 意义上不是原子的。
        uow: Any = None,
    ) -> None:
        self.config = config or CancellationSweeperConfig()
        self.clock = clock or SystemClock()
        self.service = CancellationService(kernel=kernel)
        self.runtime = ProcessRuntime(
            name="cancellation_sweeper",
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
        return len(self.service.sweep())

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        return self.runtime.health()
