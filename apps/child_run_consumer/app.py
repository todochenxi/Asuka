"""child_run_consumer 进程（M30 / 空洞 209）。

--------------------------------------------------------------------------
它是谁的对应物

    outbox_publisher     事实 → Kafka        （"说出去"）
    child_run_consumer   Kafka → 事实的后果  （"叫醒父 Run"）  ← 本进程

`OutboxPublisher` 负责把 `child_run.completed` 投到 Kafka；
本进程负责消费它，并把结果**交回**父 Run（`ChildRunWaker`）。

--------------------------------------------------------------------------
三条不能写错的顺序

1. **先 handler，再 mark**（`consumers.py` 的教条）
   反过来的"最多一次"会在崩溃时静默丢处理 —— 而丢一次唤醒
   = 父 Run 永远挂着，界面上显示"在等子 Agent"，完全正常。

2. **先 PG 事务提交，再 commit offset**（`ProcessRuntime.on_commit`）
   反过来是**丢处理而不是重复处理**：Kafka 说消费完了，PG 说从没发生过。

3. **先 `mark_finished`，再发事件**（`AgentLoop._emit_child_run_outcome`）
   那一条在生产侧，不在本进程，但它是本进程能读到结果的前提（X-5）。

--------------------------------------------------------------------------
为什么还有一条 `sweep()`（PR-33）

事件路径是快路径，它是**唯一**的入口吗？不是 —— 也不能是。
`recovery_controller` 身上那句"Redis 全丢只允许变慢，不允许变错"
在这里是同一个判据的第二个副本：

    Kafka 丢了这条事件
      → 快路径失效
      → 每 N 轮扫一次 PG（`child_runs.delivered_at IS NULL`）
      → 最多晚 N 轮被叫醒        ← 变慢
    没有 sweep
      → 父 Run 永远 SUSPENDED   ← 变错

所以 `sweep_every` 不是性能参数，是**这条不变量的兑现节奏**。
和 `RecoveryController` 一样，控制器实例（这里是本 app 自己）必须跨 tick 存活，
否则 `ticks % N` 永远是 1 % N，安全网一次都不会触发（PR-11 同款陷阱）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from packages.agent_domain.events.event import (
    CHILD_RUN_CANCELLED,
    CHILD_RUN_COMPLETED,
    CHILD_RUN_FAILED,
)
from packages.execution_kernel.consumers import IdempotentConsumer
from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock

from .._runtime import (
    ManualStop,
    ProcessReport,
    ProcessRuntime,
    StopSignal,
)

#: 本进程**只**认这三种事件。
#:
#: 刻意白名单而不是"全都交给 waker"：Kafka 的一个 topic 里会混着
#: `execution.*` / `attempt.*` 等几十种事件（见 `KafkaEventPublisher.topic_for`），
#: 交给 waker 之后它只能靠 payload 里有没有 `child_run_id` 去猜 ——
#: 猜错的表现是"某个事件被静默忽略了"，而且没有任何地方会记录它被忽略过。
CHILD_RUN_EVENTS = frozenset(
    {CHILD_RUN_COMPLETED, CHILD_RUN_FAILED, CHILD_RUN_CANCELLED}
)


@dataclass
class ChildRunConsumerConfig:
    instance_id: str = field(
        default_factory=lambda: f"child-run-consumer-{os.getpid()}"
    )
    topics: tuple[str, ...] = ("agentos.child_run.events",)
    batch_size: int = 32
    poll_timeout: float = 1.0
    sweep_every: int = 10
    """每 N 轮扫一次 PG 兜底。见文件头"为什么还有一条 sweep()"。"""
    sweep_limit: int = 64
    idle_sleep: float = 0.5
    max_idle_sleep: float = 5.0
    error_sleep: float = 2.0
    max_consecutive_failures: int = 5


class ChildRunConsumerApp:
    """`python -m apps.child_run_consumer` 跑的就是它。"""

    def __init__(
        self,
        *,
        consumer: Any,
        waker: Any,
        processed: Any,
        #: D-18 / 空洞 229：等待到期的处置者。**刻意没有默认值** ——
        #: 给默认值就等于"没接上就静默跳过"，而那正是这个洞本身的形状：
        #: 没有任何一行代码每 tick 会去看那些挂死的父 Run 一眼，
        #: 于是漏接不报错，只是父 Run 永远挂着。
        expirer: Any,
        config: ChildRunConsumerConfig | None = None,
        clock: Clock | None = None,
        signal: StopSignal | None = None,
        sleep: Callable[[float], None] | None = None,
        #: PR-30：一个 tick = 一个事务（X-3）。
        uow: Any = None,
    ) -> None:
        self.config = config or ChildRunConsumerConfig()
        self.consumer = consumer
        self.waker = waker
        self.expirer = expirer
        self.clock = clock or SystemClock()
        self.processed = processed
        #: D-18：上一轮到期扫的结果，给 `health()` 读。
        self.last_expiry: Any = None
        #: 上一轮唤醒扫的成绩（`health()` 要分开报 delivered / late / 孤儿）
        self.last_wake: Any = None
        self.idempotent = IdempotentConsumer(
            store=processed, handler=self._handle
        )
        self.runtime = ProcessRuntime(
            name="child_run_consumer",
            signal=signal or ManualStop(),
            clock=self.clock,
            idle_sleep=self.config.idle_sleep,
            max_idle_sleep=self.config.max_idle_sleep,
            error_sleep=self.config.error_sleep,
            max_consecutive_failures=self.config.max_consecutive_failures,
            sleep=sleep or time.sleep,
            uow=uow,
            # 顺序见文件头第 2 条：PG 提交之后才许说"我消费完了"
            on_commit=self._commit_offsets,
        )

    def subscribe(self, topics: Sequence[str] | None = None) -> None:
        self.consumer.subscribe(list(topics or self.config.topics))

    def tick(self) -> int:
        """一个周期：消费一批 +（每 N 轮）扫一次 PG。

        返回这一轮**真的**唤醒了几个父 Run。重复事件、非本进程关心的事件
        都不计入 —— 计入的话 `work` 就不再是"干了多少活"，
        而 `ProcessRuntime` 的空闲退避正是拿它当判据的（PR-7）。
        """
        events = self.consumer.poll(self.config.poll_timeout)
        done, _skipped = self.idempotent.consume_batch(
            e for e in events if e.event_type in CHILD_RUN_EVENTS
        )
        # PR-11 同款陷阱：计数必须活在跨 tick 的对象上（这里是 self.runtime）
        if self.runtime.ticks % self.config.sweep_every == 0:
            self.last_wake = self.waker.sweep(self.config.sweep_limit)
            done += self.last_wake.total
            # 空洞 229：第二支队列。唤醒扫的是"有结果没交回"，
            # 到期扫的是"没结果且等不到了" —— 只扫第一支，
            # 死掉的子 Run 永远不会有人来解它的父 Run。
            #
            # `now` 每轮现取（PR-13）：一个 tick 只认一个时刻。
            self.last_expiry = self.expirer.sweep(
                self.clock.now(), self.config.sweep_limit
            )
            done += self.last_expiry.total
        return done

    def run(self, *, max_ticks: int | None = None) -> ProcessReport:
        return self.runtime.run(self.tick, max_ticks=max_ticks)

    def health(self) -> dict:
        report = self.runtime.health()
        # PR-19：这几个数**分开**报。加起来会让"一堆父 Run 挂在死子 Run 上"
        # 和"一切正常"给出同一个数字。
        if self.last_wake is not None:
            # `late` 是"结果到了但没人接"（D-22）——
            # 把它并进 `delivered` 等于宣称"一切都交回去了"。
            report["delivered_total"] = len(self.last_wake.delivered)
            report["late_total"] = len(self.last_wake.late)
            report["orphan_total"] = len(self.last_wake.parent_terminal)
        if self.last_expiry is not None:
            report["expired_total"] = len(self.last_expiry.expired)
            report["parent_terminal_total"] = len(self.last_expiry.parent_terminal)
        return report

    # ------------------------------------------------------------ 内部
    def _handle(self, event: Any) -> None:
        child_run_id = str(event.payload.get("child_run_id") or "")
        if not child_run_id:
            # 一条 `child_run.completed` 却说不出是哪条子 Run ——
            # 它唯一能做的事（唤醒）根本无法开始。
            # 静默跳过 = 父 Run 永远挂着，而且事件登记表上写着"已处理"。
            raise ValueError(
                f"child run outcome event {event.event_id!r} "
                f"({event.event_type}) carries no child_run_id; it cannot wake "
                f"anything and must not be marked processed"
            )
        self.waker.wake(child_run_id)

    def _commit_offsets(self) -> None:
        """只在事务提交之后被调用 —— 见 `ProcessRuntime.on_commit`。"""
        self.consumer.commit()


__all__ = [
    "CHILD_RUN_EVENTS",
    "ChildRunConsumerApp",
    "ChildRunConsumerConfig",
]
