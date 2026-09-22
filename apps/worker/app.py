"""worker 进程（M22）。

--------------------------------------------------------------------------
这是整个系统里唯一"产出"的进程

    outbox_publisher    把已经发生的事说出去
    recovery_controller 把跑挂了的救回来
    wakeup_controller   把挂起的叫醒
    cancellation_sweeper 把取消收敛到终态
    worker              真正把 Task 跑完  ← 只有它制造上面四者的打扫对象

--------------------------------------------------------------------------
PR-17：Worker 不另设领地表 —— Lease 本身就是领地

`apps/outbox_publisher` 需要 `006_outbox_delivery.sql`，而 worker 不需要第二张表。
差别不在"谁更重要"，而在 PR-12 那条判据：**这个动作重复做会不会改变结果。**

    Worker.claim    PG 里 `executions` 行上就带着 Lease（含 fencing_token），
                    Atomic Claim 保证一个 Execution 同时只有一个持有者
                    → 第二个 worker 来抢会失败，不需要额外记账

    Outbox 投递     `outbox_events` 由**业务事务**写入，回答"发生了什么"，
                    它身上没有"谁正在投"这一列，也不该有
                    （投递进程不能去改业务事务写的表）
                    → 只能在旁边另开一张领地表

同一句"要不要领地"，Outbox 要、Worker 不要 —— 因为前者的领地无处安放。

--------------------------------------------------------------------------
PR-18：这里没有 `apps/scheduler`

`Worker.run_once()` 内部调用 `Scheduler.dispatch()`，派活是本进程 tick 内的
一次 Atomic Claim。§45 的树里画了一个 `apps/scheduler/`，那是不对的：

    · 独立 scheduler = 一个中心。它挂了，所有 worker 都拿不到活。
    · 它要"通知 worker 去干活"，就必须走消息 —— 那正是 §36 明令禁止的
      "把 Kafka 当任务队列"（消息传到 ≠ 拿到执行权）。

    §36：调度与派活一律走 PG + Lease + Atomic Claim。
    Scheduler 是 Kernel 的一项能力，不是一个部署单元。
--------------------------------------------------------------------------
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)

if TYPE_CHECKING:  # pragma: no cover
    from packages.execution_kernel.worker import Worker


@dataclass
class WorkerProcessConfig:
    instance_id: str = field(
        default_factory=lambda: f"worker-{os.getpid()}"
    )
    poll_limit: int = 1
    """一个 tick 最多认领几个 Execution。

    认领了就得在 Lease 内跑完 —— 认领超过自己跑得完的数量，
    等于自己制造一批待 recovery 的 STALE。
    这里刻意不做"poll_limit 必须 ≤ free_slots"的校验：顺序执行的 worker
    认领多个是合法的，硬校验会挡住正当用法；代价由 Lease 自己承担。
    """
    idle_sleep: float = 0.05
    max_idle_sleep: float = 2.0
    error_sleep: float = 0.5
    max_consecutive_failures: int = 5


class WorkerApp:
    """`python -m apps.worker` 跑的就是它。

    Worker（拉活 + 执行 + 心跳 + 回写）全在 `packages/execution_kernel/worker.py`。
    这里只回答"还要不要跑下一轮"。
    """

    def __init__(
        self,
        *,
        worker: "Worker",
        config: WorkerProcessConfig | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。
        uow: Any = None,
    ) -> None:
        self.config = config or WorkerProcessConfig()
        self.clock = clock or SystemClock()
        self.worker = worker
        self.runtime = ProcessRuntime(
            name="worker",
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
        """派发 → 执行。返回这一轮**处理了几个** Execution。

        注意返回值不是"成功了几个"：失败也是处理过，不能当成空闲去退避，
        否则一个持续失败的 Task 会让 worker 越睡越久。
        """
        return len(self.worker.run_once(limit=self.config.poll_limit))

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        return self.runtime.health()
