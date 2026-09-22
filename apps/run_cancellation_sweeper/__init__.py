"""run_cancellation_sweeper 进程（M34 / 空洞 222）。

**Run 级**取消意图 → 终态收敛。与 `apps/cancellation_sweeper` 是相邻的两层：

    apps.cancellation_sweeper      Execution 级（`cancellation_requested` 挂在 Execution 上）
    apps.run_cancellation_sweeper  Run 级（`run_cancellations` 表）

为什么必须有两层而不是复用下面那层：一条**子 Run** 不是一条 Execution。
它有它自己的一堆 Execution，而且它在等孙 Run 的时候**手上根本没有活的
Execution** —— 那条因为闸门而 SUSPENDED 的 Execution 是"它在等"的证据，
不是"它在跑"的证据。往它上面写取消信号，等于取消一次等待。

这个进程存在的唯一理由，是**协作式取消兜不住的那一部分**：

    一条停在 WAITING_CHILD / WAITING_APPROVAL 的 Run 不会调 `step()`。
    它这一辈子可能再没有第二个安全点，于是没人会去读那条意图。

它**不需要领地**，判据与 `cancellation_sweeper` 一字不差（PR-12）：

    `cancel()` 的动作是**收敛** —— 走状态机，已经终态就是 no-op。
    多副本并发扫同一批，结果完全一样。
    给一个收敛动作加领地，只会在"租约过期"那一刻多出一条静默卡住的路径。
"""
from .app import RunCancellationSweeperApp, RunCancellationSweeperConfig

__all__ = ["RunCancellationSweeperApp", "RunCancellationSweeperConfig"]
