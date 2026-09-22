"""`python -m apps.run_cancellation_sweeper` 跑的就是它（M34 / 空洞 222）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)


@dataclass
class RunCancellationSweeperConfig:
    """比 Execution 级那个更急一点：用户按了"停止"却一直显示 RUNNING
    是最难解释的事故之一。默认 3s 是折中，不是算出来的。
    """

    idle_sleep: float = 3.0
    max_idle_sleep: float = 30.0
    error_sleep: float = 2.0
    max_consecutive_failures: int = 5


class RunCancellationSweeperApp:
    """把"取消意图"推进到"终态"。

    `sweep()` 的判据在 `RunCancellationService`（R-7 / R-8 / R-9 / R-10）；
    这里只负责节奏与事务边界，**不复制**任何一条判定。
    """

    def __init__(
        self,
        *,
        service: Any,
        config: RunCancellationSweeperConfig | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。组合根必须显式接上，
        #: 否则"这条 Run 被判死"与"那条意图被结掉"不在同一个事务里 ——
        #: 崩在中间就留下一条永远会被重新叫停的意图。
        uow: Any = None,
    ) -> None:
        self.config = config or RunCancellationSweeperConfig()
        self.clock = clock or SystemClock()
        self.service = service
        #: 上一轮的成绩。`health()` 要把它拆成 settled / abandoned 两个数报出去。
        self.last_result: Any = None
        self.runtime = ProcessRuntime(
            name="run_cancellation_sweeper",
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
        """一轮。返回"有进展"的条数 —— 判空闲 / 退避用的就是这个数。

        两种结局都算进展（R-11 / R-13）：认领了一条是真的停了，
        放弃了一条是**让出了槽位**。少了后者，
        `pending()` 会攒满永不结算的僵尸，而 `tick()` 一直返回 0 ——
        进程看起来很闲，通道其实已经堵死。
        """
        self.last_result = self.service.sweep()
        return self.last_result.total

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        """`abandoned` 是**病态信号**，刻意和 `settled` 分开报。

        一个每轮 `settled=3` 的系统是健康的；
        一个每轮 `abandoned=3` 的系统在告诉我们：
        有 Run 被叫停之后再也没有回音（进程没了），
        而且它们的副作用已经记进补偿账本等着人去看。
        把两者加成一个数字，会把第二种说成第一种（PR-19）。
        """
        report = self.runtime.health()
        result = self.last_result
        report["settled_total"] = len(result.settled) if result else 0
        report["abandoned_total"] = len(result.abandoned) if result else 0
        return report
