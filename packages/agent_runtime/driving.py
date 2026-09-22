"""把一条**已经不再被阻塞**的 Run 往前推（M42 / 空洞 217）。

--------------------------------------------------------------------------
起因：解开阻塞 ≠ 往前走

M30 接通了"派得出去、认得回来"，M38 接通了"等不到的时候谁来解开"。
两条路径在解开父 Run 的地方都停住了：

    ChildRunWaker.wake()         交付结果 → 落新快照 → 标记交付 → return
    ChildRunWaitExpirer.expire() 关闸门   → 落新快照 → 标记到期 → return

父 Run 从"在等那条子 Run"变成"**可以被推一步**"。然后就没有然后了 ——
`child_wait.py` 自己那句注释把这件事写得明明白白：

    父 Run 从此可以被推一步 … 把下一步留给 step()

"留给 step()"就是**留给调用方**。而在跨进程部署里那个调用方不存在：
没有任何一个进程的 tick 会去问"有哪些 Run 刚刚被解开"。

于是真实世界里发生的是：

    子 Run 跑完 → 事件来了 → 唤醒器重建父 Run → 交付 → 落快照 → 扔掉
                                                              ↑
                                              这个 stack 从此没人再碰

界面上仍然显示"运行中"（快照里 status 还是 running），
而它再也不会前进一步 —— 与空洞 229（父 Run 永远挂着）是同一个现象，
只是那一次的病因是"没人解开"，这一次是"解开了没人推"。

--------------------------------------------------------------------------
D-27：谁解开，谁推进

> **D-27：解开一个 Run 的阻塞的人，必须把它推进到下一个阻塞点或终态。**
>
> "现在可以被推一步"不是一种状态 —— 它是把一件必须做的事
> 推给了一个**不存在的调用方**。

为什么是"解开它的人"而不是"另起一个驱动进程"：

    · 它手上**已经有**重建好的 RuntimeStack（`rebuild()` 刚跑过）。
      让别人再重建一次，等于多一次快照读、多一次反序列化，
      而这两者之间可能已经有人改过那一行。
    · 更重要的是**责任**：解开与推进一旦分家，
      "解开了但还没被推进"就变成一个**需要新所有者**的中间态 ——
      那就要再有一张队列表、再有一个扫队进程、再有第二套判据。
      而它本来不该存在。

--------------------------------------------------------------------------
D-28：推进之后必须落一个新的可恢复点

`AgentLoop` 只在**挂起**时才自己落快照（等审批、等子 Run、被叫停）。
跑到终态那条路径（`_finish()` / `_declare_terminal()`）**不落**。

于是一个跑完的 Run 在 `run_snapshots` 里的最新一条仍然是挂起时那份 ——
`snapshot.is_terminal` 是 False。后果不止"看不见它跑完了"：

    下一次有子 Run 的结果要交回来
      → ChildRunWaker.rebuild() 读最新快照 → 不是终态 → 重建成功
      → 一条**已经结束**的 Run 被重新装载出来继续走（R-3 被绕开）

所以推进之后必须落一份，而且这份快照的 `reason` 要写明它是推进的产物 ——
终态快照是"这个 Run 结束了"这件事在存储里的**唯一**证据。

--------------------------------------------------------------------------
D-29：推进要排在"标记处置完成"**之前**

见 `InProcessRunDriver` 上方的顺序说明。简单说：
推进是这条链路上**最容易崩**的一步（它在调模型、调工具、动外部世界），
把它排在一次"我已经处理完了"的标记之后，崩溃就意味着
兜底扫再也不会碰它 —— 而那正是空洞 229/217 的形状。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from packages.agent_domain.errors import IllegalTransition

from .recovery import RunRecovery

__all__ = [
    "DriveOutcome",
    "DriveResult",
    "InProcessRunDriver",
    "RunDriver",
    "snapshot_reason",
]


class DriveOutcome(str, Enum):
    """一次推进的结果。刻意不做成 bool —— "没推进"有几种，处置不同。"""

    ADVANCED = "advanced"
    """真的往前走了：停在下一次阻塞点，或者进了终态。"""

    TERMINAL = "terminal"
    """重建时发现它已经是终态（R-3）—— 没有可推的东西。

    这不是错误：它和"交付完成"是同一件事的两种说法。
    把它做成异常会让唤醒路径上多一条 `try`，而那条 `try` 里
    什么也做不了 —— 那正是 D-27 要消灭的"假装交给了谁"。
    """


@dataclass(frozen=True)
class DriveResult:
    """推进的产物。

    `last_outcome` 是 `StepOutcome` 的值 —— "停在哪"必须能说出来：
    "停了"这件事有三种（挂起 / 预算耗尽 / 完成），
    只返回一个"推进成功"会把它们并成一个数（PR-19，与 `ChildWakeOutcome` 同款）。
    """

    run_id: str
    outcome: DriveOutcome
    #: `StepOutcome.value`；TERMINAL / 还没 `start()` 的情况是 ""。
    last_outcome: str = ""
    #: 推进之后的预算计数（`AgentLoop.steps`，不是 Step 个数）。
    step_count: int = 0


class RunDriver(Protocol):
    """**推进一条 Run** 的端口（四边界里的 Runtime 那一格）。

    Runtime 驱动 Agent 的循环 —— 所以它不是 Kernel 的事（Kernel 只管
    把一条 Task 可靠做完），也不是契约层的事（契约层只问"在不在"）。

    做成端口而不是让 waker 直接 `loop.run()`，是因为"怎么推"
    必须只有一个定义（B-7）：唤醒路径、到期路径、以及将来任何一条
    "解开某个阻塞"的路径都要用同一份。
    """

    def drive(self, run_id: str) -> DriveResult: ...


@dataclass
class InProcessRunDriver:
    """在同一个进程里把 Run 推到下一个阻塞点或终态。

    ------------------------------------------------------------------
    为什么 `run()` 而不是"调几次 `step()`"

    `AgentLoop.run()` 的停止集合是"什么时候该停"的**唯一**定义
    （等审批 / 等子 Run / 完成 / 预算耗尽 / 被叫停 / 一直被拒）。
    由驱动方自己数着调几次 `step()`，等于重新实现一遍那个集合 ——
    于是"一个 Run 什么时候停下来"有了第二个答案（B-7）。

    ------------------------------------------------------------------
    为什么**不**在这里加 `max_steps`

    `drive_run()` 那段已经写过一次：上界只有一个，它在
    `Goal.budget.max_steps`（Intelligence 决定），
    `AgentLoop.run()` 认的就是它。在这里再加一道，
    三者不一致时没人说得清以哪个为准。

    ------------------------------------------------------------------
    为什么让异常**往外抛**（D-29）

    推进会调模型、调工具、动外部世界 —— 它是这条链路上最容易崩的一步。
    吞掉它的后果不是"这次没推成"，是**永远没人再推**：

        吞掉 → 正常返回 → 调用方 mark_delivered → 兜底扫再也不碰它
              → 父 Run 停在半路，界面显示"运行中"

    所以 `drive()` 抛，`ChildRunWaker` 不接 —— 异常一路传到
    `ProcessRuntime`，它计一次失败并退避；而因为推进排在
    `mark_delivered` **之前**，那条子 Run 仍然留在 `undelivered()` 里，
    下一轮兜底扫会**再推一次**。崩溃变成"变慢"，不是"变错"（A-12）。
    """

    recovery: RunRecovery

    def drive(self, run_id: str) -> DriveResult:
        try:
            stack = self.recovery.rebuild(run_id)
        except IllegalTransition:
            # R-3：终态不可变。它不需要被推 —— 它已经结束了。
            # 唤醒路径上这条分支意味着"结果无处可交"（D-13 那一支），
            # 那里负责记孤儿，这里只负责说"没得推"。
            return DriveResult(run_id=run_id, outcome=DriveOutcome.TERMINAL)

        loop = stack.loop
        loop.run()
        # D-28：推进之后必须落一个新的可恢复点 —— 终态那条路径
        # （`_declare_terminal`）自己**不落**快照，少了这一句，
        # "这个 Run 跑完了"在存储里根本不存在。
        self.recovery.snapshots.save(
            loop.capture(reason=self._reason(loop.last_outcome))
        )
        return DriveResult(
            run_id=run_id,
            outcome=DriveOutcome.ADVANCED,
            last_outcome=_outcome_value(loop.last_outcome),
            step_count=loop.steps,
        )

    @staticmethod
    def _reason(last_outcome: Any) -> str:
        """快照的 `reason`：必须说出**停在哪**，不能只说"推过了"。

        运维查"为什么这条 Run 停在这"时看的就是这一列。
        写成 `driven` 等于什么都没说 —— 那正是 PR-19 那一类。
        """
        return snapshot_reason(last_outcome)


def _outcome_value(outcome: Any) -> str:
    value = getattr(outcome, "value", None)
    return str(value) if value is not None else (str(outcome) if outcome else "")


def snapshot_reason(last_outcome: Any) -> str:
    """推进之后那个快照的 `reason`（M73：提到这里是为了只留一处定义）。

    原本它只是 `RunDriver._reason` 一个私有静态方法，够用 ——
    直到 HTTP 推进路径（`packages.agent_api.service`）也需要落快照。

    那时面前有两条路：在那边照抄一遍这个格式串，或者把它拎出来共用。
    抄一遍的代价是"driven: run stopped at ..."这句话有了两个作者 ——
    改了一个忘了另一个，运维在 `run_snapshots.reason` 里就会看到
    两种写法的同一件事，而它们看起来像两种不同的推进来源（B-7）。
    """
    return f"driven: run stopped at {_outcome_value(last_outcome) or 'unknown'}"
