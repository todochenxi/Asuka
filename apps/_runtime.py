"""ProcessRuntime：把"可调用的类"变成"一个能被编排层管起来的进程"（M21 / §59）。

--------------------------------------------------------------------------
为什么需要这一层

`packages/` 里已经写好了 `drain()` / `sweep()` / `run_once()` ——
但它们是**方法**，不是**进程**。一个类有 `run()` 不等于它是个进程：

    · 谁告诉它该停了？（SIGTERM 到了不会自己看）
    · 停的时候手上那批活怎么办？（硬杀 = 半批状态）
    · 空转的时候多久扫一次？（不控就是每轮全表扫）
    · 一直失败的时候该怎么办？（原地重试 = 对外假装健康）
    · 编排层怎么知道它是"活着"还是"在干活"？

这五件事，每一件都属于**进程**，不属于库。库只该负责"一个周期里做什么"。
--------------------------------------------------------------------------

不变量：

    PR-1  生命周期两阶段：RUNNING → **DRAINING** → STOPPED。
          停止请求先切断"接新活"，再等当前 tick 跑完 —— 不允许在 tick 中间硬杀，
          那会让"一个周期"不再原子，半批状态无从解释。
    PR-2  退出只许重复、不许丢：所以退出前必须把手上的领地**显式归还**
          （`on_drain`）。做完和放弃都算数，沉默地带着领地消失不算。
    PR-7  空闲必须退避，且退避**有上限**。指数退避不加顶会一路睡到几分钟，
          新事件进来要等几分钟才被发现 —— 队列深度指标会先炸。
    PR-8  活着 ≠ 在干活。liveness 与 readiness 必须分开；
          连续失败达阈值必须**退出**（让编排层重启 / 报警），
          而不是原地无限重试、对外汇报"我还活着"。
    PR-9  顺序由存储层决定（ORDER BY occurred_at），不由认领竞争决定 ——
          本文件不重排任何东西。
"""
from __future__ import annotations

import signal as _signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Protocol

from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.ports import Clock

#: 退避指数的硬上限。`2 ** 32` 已经远超任何合理的 `max_idle_sleep`，
#: 再往上只是等着溢出（见 `ProcessRuntime._backoff`）。
_MAX_BACKOFF_STEPS = 32


class ProcessState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class StopSignal(Protocol):
    """停止信号。抽成接口是为了让"该停了"这件事可以被注入、被测试。"""

    def should_stop(self) -> bool: ...


class NeverStop:
    def should_stop(self) -> bool:
        return False


class ManualStop:
    """测试与嵌入场景：由代码显式请求停止。"""

    def __init__(self) -> None:
        self.stopped = False
        self.asked = 0

    def should_stop(self) -> bool:
        self.asked += 1
        return self.stopped

    def request(self) -> None:
        self.stopped = True


class _NullTransaction:
    """没有 UnitOfWork 时的空边界。

    它的存在是**诚实的缺省**而不是兜底：进程骨架照样写成 `with ...:`，
    于是不管有没有事务，"边界在哪"这个问题只有一个答案。
    """

    def __enter__(self) -> "_NullTransaction":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class StopAfter:
    """第 N 次询问之后返回 True。用来测"信号在 tick 之间到达"这件事。"""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.asked = 0

    def should_stop(self) -> bool:
        self.asked += 1
        return self.asked > self.ticks


class SignalStop:
    """真实部署：SIGTERM / SIGINT → should_stop()。

    显式构造，**不在 import 时注册** —— 库层不该有全局副作用，
    而且测试进程里注册信号处理器只会带来麻烦。
    """

    def __init__(self, signals: tuple[int, ...] = (_signal.SIGTERM, _signal.SIGINT)) -> None:
        self.stopped = False
        for sig in signals:
            _signal.signal(sig, self._handle)

    def _handle(self, signum: int, frame: Any) -> None:  # pragma: no cover
        self.stopped = True

    def should_stop(self) -> bool:
        return self.stopped


@dataclass
class ProcessReport:
    name: str
    state: ProcessState
    ticks: int = 0
    work: int = 0
    idle_ticks: int = 0
    errors: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    drained: int = 0
    reason: str = "max_ticks"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "ticks": self.ticks,
            "work": self.work,
            "idle_ticks": self.idle_ticks,
            "errors": self.errors,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "drained": self.drained,
            "reason": self.reason,
        }


@dataclass
class ProcessRuntime:
    """一个进程的主循环骨架。

    `tick` 是"一个调度周期该做的事"，返回这一轮干了多少活（0 = 空转）。
    tick **抛异常**代表基础设施不可用（broker 挂了 / 连不上库），
    不代表"这批里有一条坏数据" —— 后者应该在 tick 内部消化掉。
    """

    name: str
    signal: StopSignal = field(default_factory=ManualStop)
    clock: Clock = field(default_factory=SystemClock)
    idle_sleep: float = 0.05
    max_idle_sleep: float = 5.0
    error_sleep: float = 0.25
    max_consecutive_failures: int = 5
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    on_drain: Callable[[], int] | None = None
    #: 提交**之后**才许做的事（M30）。
    #:
    #: 典型是 Kafka offset 提交。它和 `on_drain` 不是一回事：
    #:   on_drain  = 退出时归还领地（做没做完都算数）
    #:   on_commit = 这一轮真的落库了，才可以把"我处理过了"说出去
    #:
    #: 顺序反了是什么后果：offset 先提交、PG 随后回滚 ——
    #: 那条消息再也不会被投递，而它引起的写**从未生效**。
    #: 这不是"重复处理"，是**丢处理**，而且在两侧都看不出来：
    #: Kafka 说消费完了，PG 说从没发生过。
    on_commit: Callable[[], None] | None = None
    #: PR-30：一个 tick = 一个事务。没有它就**不开事务** ——
    #: 一个不知道事务边界的进程不该假装自己写过东西（X-3）。
    uow: Any | None = None
    #: M68：心跳文件。设了它，每跑完一轮 tick 就 touch 一次。
    #:
    #: 为什么是"文件"而不是"又一个 HTTP 端口"：八个后台进程
    #: （worker / sweeper / controller / consumer）**都没有** HTTP，
    #: 给它们各开一个端口只为探活，等于给每个进程加一张对外网卡。
    #: 而编排层真正要问的那句话很短 —— "你还在往前走吗"。
    #:
    #: 为什么放在这里：这是八个进程**共用**的主循环骨架。
    #: 写在一处，八个进程就都开始跳；写八遍，漏掉的那个不报错，
    #: 它只是安静地在 K8s 里被当成健康容器一直跑着。
    heartbeat_path: str = ""

    state: ProcessState = field(default=ProcessState.NEW, init=False)
    ticks: int = field(default=0, init=False)
    work: int = field(default=0, init=False)
    idle_ticks: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)
    idle_streak: int = field(default=0, init=False)
    consecutive_failures: int = field(default=0, init=False)
    last_error: str | None = field(default=None, init=False)
    last_progress_at: datetime | None = field(default=None, init=False)
    drained: int = field(default=0, init=False)
    reason: str = field(default="max_ticks", init=False)

    def run(self, tick: Callable[[], int], *, max_ticks: int | None = None) -> ProcessReport:
        """PR-1：只在 tick 与 tick 之间检查停止信号，保证一个周期是原子的。"""
        self.state = ProcessState.RUNNING
        # M68：起来就先跳一次。于是"文件不存在"只可能是"它从未起来过"，
        # 而不是"刚起来还没跑到第一轮" —— 后者会让探活把每个刚启动的
        # 进程都当成死的（启动即被杀，且重启多少次都是一样的结局）。
        self._beat()
        while max_ticks is None or self.ticks < max_ticks:
            if self.signal.should_stop():
                return self._shutdown(ProcessState.STOPPED, reason="signal")
            self._tick_once(tick)
            if self.state is ProcessState.FAILED:
                return self._shutdown(ProcessState.FAILED, reason="too_many_failures")
        return self._shutdown(ProcessState.STOPPED, reason="max_ticks")

    def _tick_once(self, tick: Callable[[], int]) -> None:
        """PR-30：一个 tick = 一个事务。

        PR-1 已经说过"一个 tick 是原子的"—— 但那只是**调度**意义上的原子
        （不在 tick 中间硬杀）。DB 意义上的原子此前**没有任何人负责**：
        `packages/` 里没有一处 `commit()`，于是 tick 里发出的每一条 SQL
        都各自为政，中间崩了就留下一半。

        X-3 要的是"状态写 + 事件写一起生效或一起消失"。
        放在这里而不是放进每个 app 的 tick 里，是因为五个进程共用这个骨架 ——
        **写在一处，五个进程就都对了**；写五遍，漏掉的那一个不报错。
        """
        self.ticks += 1
        try:
            with self._transaction():
                work = int(tick())
            # 只有上面那个 `with` 正常退出（= 提交成功）才轮到它。
            # 放进 `with` 里面就等于"还没落库就说处理过了"。
            if self.on_commit is not None:
                self.on_commit()
        except Exception as exc:
            self.errors += 1
            self.consecutive_failures += 1
            self.last_error = str(exc) or repr(exc)
            if self.consecutive_failures >= self.max_consecutive_failures:
                # PR-8：不假装健康。退出，让编排层看见并重启。
                self.state = ProcessState.FAILED
                return
            self.sleep(self.error_sleep)
            return

        self.consecutive_failures = 0
        self._beat()
        self.work += work
        if work:
            self.idle_streak = 0
            self.last_progress_at = self.clock.now()
        else:
            self.idle_streak += 1
            self.idle_ticks += 1
            self.sleep(self._backoff())

    def _beat(self) -> None:
        """M68：记一次心跳 —— "刚刚完整跑完一轮"。

        刻意**只**在 tick 没抛异常时跳：
        一轮跑不完（连库挂在半路是最典型的形态）就不算在往前走。
        于是"进程还在、但已经不动了"这个最难被发现的状态，
        在编排层看来和进程死了一样 —— 而这两件事**对上线来说是一样的**：
        两种情况的修法都是重启它。

        心跳写不进去时**不**让进程死：观测设施不该有能力拖垮业务进程。
        它烂掉的结果是"探活判死、进程被重启" —— 这是一个会立刻被
        看见的结果，好过让进程因为写不下一个文件而退出。
        """
        if not self.heartbeat_path:
            return
        try:
            import os

            with open(self.heartbeat_path, "a"):
                pass
            os.utime(self.heartbeat_path, None)
        except OSError:
            pass

    def _transaction(self) -> Any:
        """有 UnitOfWork 就开事务，没有就给一个什么都不做的边界。

        刻意**不**在没有 uow 时抛错：进程骨架同样被纯内存的测试用到，
        逼它们造一个假事务只会得到一批"替身里通过"的证据（PR-28）。
        真正该断言"这个进程有没有事务边界"的地方是组合根，不是这里。
        """
        if self.uow is not None:
            return self.uow
        return _NullTransaction()

    def _backoff(self) -> float:
        """PR-7：指数退避，但必须有顶 —— 而且**顶要截在指数之前**。

        写成 `min(idle_sleep * 2 ** (streak - 1), max_idle_sleep)` 是错的：
        上限在**算出**那个数之后才生效，而空队列跑一晚上 streak 能到几万，
        `2 ** 几万` 先抛 `OverflowError: int too large to convert to float`。

        也就是说"退避有上限"这条，只在 streak 很小的时候成立 ——
        进程会在**最闲的时候**崩，而且崩在一个跟业务毫无关系的地方。
        """
        steps = min(max(0, self.idle_streak - 1), _MAX_BACKOFF_STEPS)
        return min(self.idle_sleep * (2 ** steps), self.max_idle_sleep)

    def _shutdown(self, terminal: ProcessState, *, reason: str) -> ProcessReport:
        """PR-1 / PR-2：先切断接新活，归还领地，再落到终态。"""
        self.state = ProcessState.DRAINING
        self.reason = reason
        if self.on_drain is not None:
            self.drained = self.on_drain() or 0
        self.state = terminal
        return self._report()

    def _report(self) -> ProcessReport:
        return ProcessReport(
            name=self.name,
            state=self.state,
            ticks=self.ticks,
            work=self.work,
            idle_ticks=self.idle_ticks,
            errors=self.errors,
            consecutive_failures=self.consecutive_failures,
            last_error=self.last_error,
            drained=self.drained,
            reason=self.reason,
        )

    def health(self) -> dict[str, Any]:
        """PR-8：alive = 进程没死；ready = 还有能力干活。两者不能混。"""
        return {
            "name": self.name,
            "state": self.state.value,
            "alive": self.state in (ProcessState.RUNNING, ProcessState.DRAINING),
            "ready": self.state is ProcessState.RUNNING
            and self.consecutive_failures == 0,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_progress_at": (
                self.last_progress_at.isoformat() if self.last_progress_at else None
            ),
            "work": self.work,
        }
