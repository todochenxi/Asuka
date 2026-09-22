"""wakeup_controller 进程（M21）。

这个进程的价值不在"循环"上，而在**唤醒谓词每轮现取**（PR-13）。

两条唤醒条件的时间尺度完全不同：

    TIMER          时间流过就满足 —— 每轮的 `now` 本来就不同，天然新鲜
    HUMAN_APPROVAL 审批结果到达才满足 —— 结果在**存储**里，不在内存里

第二条是 A-10 在进程层的落点：审批结果必须读 `ApprovalStore`，不能读
某个内存对象。在这里最容易被写错的形式是**构造时快照一次**：

    ❌ approval_satisfied(self._approvals())   放在 __init__ 里
       → 进程活着的这段时间里，谁审批通过了都唤不醒，
         而且现象是"审批通过了却一直挂着"，没人会想到是谓词被快照了。

    ✅ 每轮 tick 重新调用 self._approvals()
       → 审批落到存储里的下一轮就会被扫到。

同理，一个 tick 只取**一次** `now`：timer 判定必须基于同一个时刻，
否则同一次扫描里前半批和后半批用的是两个不同的"现在"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock
from packages.execution_kernel.wakeup_controller import (
    WakeupController,
    approval_satisfied,
    timer_satisfied,
)

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)

if TYPE_CHECKING:  # pragma: no cover
    from packages.agent_domain.execution import Suspension
    from packages.execution_kernel.kernel import ExecutionKernel

#: 审批结果快照：`approver -> 决策`。每轮重新取。
ApprovalSource = Callable[[], Mapping[str, str]]

#: 额外的唤醒条件（事件到达、外部回调等）。由组合根注入，这里不碰事件总线。
WakePredicate = Callable[["Suspension", Mapping], bool]


@dataclass
class WakeupControllerConfig:
    idle_sleep: float = 0.5
    max_idle_sleep: float = 5.0
    error_sleep: float = 2.0
    max_consecutive_failures: int = 5
    extra_predicates: Sequence[WakePredicate] = field(default_factory=tuple)


class WakeupControllerApp:
    """`python -m apps.wakeup_controller` 跑的就是它。"""

    def __init__(
        self,
        *,
        kernel: "ExecutionKernel",
        approvals: ApprovalSource | None = None,
        config: WakeupControllerConfig | None = None,
        controller: WakeupController | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。不传就**没有**事务边界 ——
        #: 组合根必须显式接上，否则这个进程的写在 DB 意义上不是原子的。
        uow: Any = None,
    ) -> None:
        self.config = config or WakeupControllerConfig()
        self.clock = clock or SystemClock()
        self.approvals = approvals
        self.controller = controller or WakeupController(kernel=kernel)
        self.runtime = ProcessRuntime(
            name="wakeup_controller",
            signal=signal or ManualStop(),
            clock=self.clock,
            idle_sleep=self.config.idle_sleep,
            max_idle_sleep=self.config.max_idle_sleep,
            error_sleep=self.config.error_sleep,
            max_consecutive_failures=self.config.max_consecutive_failures,
            sleep=sleep or time.sleep,
            uow=uow,
        )

    def predicate(self):
        """PR-13：每轮现场组装 —— `now` 与审批结果都不许跨轮缓存。"""
        now = self.clock.now()                     # 一个 tick 只认一个 now
        timer = timer_satisfied(lambda: now)
        approvals = (
            approval_satisfied(self.approvals())
            if self.approvals is not None
            else None
        )
        extra = tuple(self.config.extra_predicates)

        def is_satisfied(suspension, ctx) -> bool:
            if timer(suspension, ctx):
                return True
            if approvals is not None and approvals(suspension, ctx):
                return True
            return any(predicate(suspension, ctx) for predicate in extra)

        return is_satisfied

    def tick(self) -> int:
        return len(self.controller.run_once(self.predicate()))

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        return self.runtime.health()
