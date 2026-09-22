"""进程探针（M68 / M11 部署编排第二块）。

--------------------------------------------------------------------------
它补的是什么空洞

`apps/_runtime.py` 里早就有 `health()`，`alive` 与 `ready` 分得清清楚楚（PR-8）。
但那是一个**进程内**的方法：编排层看不见它。

于是八个进程一旦放进 Kubernetes：

    api                  有 `/health`，而它自己写着"存活 ≠ 就绪，
                         这里只回答存活" —— 就绪没人答
    worker               什么都没有
    outbox_publisher     什么都没有
    cancellation_sweeper 什么都没有
    run_cancellation_sweeper / wakeup_controller / recovery_controller
    child_run_consumer   什么都没有

"什么都没有"的后果不是"少一个指标"，是**编排层对这七个进程是瞎的**：
一个 worker 卡在连库的半路上（进程在、PID 在、容器 Running），
K8s 会一直认为它健康，而队列在它背后安静地堆着。
这种失败没有红灯、没有 CrashLoop、没有事件 ——
它只在"为什么这批活没人干"被追问时才浮现，而那时现场已经没了。

--------------------------------------------------------------------------
两个命令，两句话

    live    你还在往前走吗？  —— 看心跳文件的新鲜度
    ready   你现在能干活吗？  —— 连一次库

分两句是 PR-8 的要求，而且在这个场景里分得特别实在：

    · 库挂了：ready 红、live 绿 → 摘流量，别杀（杀了也没用，起来还是连不上）
    · 进程卡了：live 红         → 杀掉重启（新进程能重新连库）

合成一个结果，这两种情况的处置就被迫相同 ——
而"一律重启"在库挂掉时会让八个进程一起进入 CrashLoopBackOff。

--------------------------------------------------------------------------
为什么 liveness 看心跳，不看"进程在不在"

`kubectl get pod` 说 Running，只说明 PID 1 没退出。
一个卡在 `socket.recv()` 上的进程，PID 1 活得好好的 ——
**"活着"和"在干活"之间的那道缝，正是这类事故藏身的地方。**

心跳文件由 `ProcessRuntime` 在每轮 tick 跑完后 touch。
一轮跑不完就不跳，于是"卡住了"在编排层看来与"死了"是同一件事 ——
因为对上线来说它们确实是同一件事：两者的修法都是重启它。
"""
from __future__ import annotations

from .app import (
    DEFAULT_LIVE_MAX_AGE,
    ProbeResult,
    check_live,
    check_ready,
)

__all__ = [
    "DEFAULT_LIVE_MAX_AGE",
    "ProbeResult",
    "check_live",
    "check_ready",
]
