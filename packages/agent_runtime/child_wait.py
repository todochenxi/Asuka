"""一次派生的**等待到期**（M38 / 空洞 229）。

--------------------------------------------------------------------------
这个洞的形状：挂着的那一方没有能力自己超时

    AgentLoop.step()  ──D-5──▶  一见 pending_child 就返回 WAITING_CHILD
                                       │
                                       ▼
                              父 Run 挂起，落快照，什么都不做了

于是"我等了多久"这件事**没有任何人在算**：

    · 父 Run 自己不会算 —— 它连 `step()` 都走不到能判时间的地方
    · `ChildRunWaker.sweep()` 不算 —— 它扫的是 `completed_at IS NOT NULL`，
      而这条子 Run **没有终态**（进程彻底没了）
    · `undelivered()` 也不算 —— 同一个谓词

所以一条死掉的子 Run 会让它的父 Run **永远挂在 WAITING_CHILD 上**，
界面显示"在等子 Agent"，没有任何报错、没有任何计数器会动。

它比空洞 226 那一侧更静：226 至少还有一条每 tick 被捞起、被 `continue`
掉的请求（看得见的空转）；这一侧连空转都没有。

--------------------------------------------------------------------------
为什么必须有人**从外面**来判

超时这件事挂在父 Run 身上是没有用的 —— 判超时的代码要跑起来才判得了，
而父 Run 正等着，它不跑。所以判据必须落在一个**旁观者**身上：
它读登记处（`overdue()`），把父 Run **装载回来**，
交给父 Run 自己走完这一步（`child_wait_expired`），再拍一份快照。

这就是 D-21 的边界：

    旁观者决定的是"**不再等**"
    父 Run 决定的是"**不再等之后干什么**"

旁观者替父 Run 写终态，就是把"策略失败之后怎么办"从 Intelligence 手里拿走。

--------------------------------------------------------------------------
为什么 `mark_wait_expired` 不是"结掉这次派生"

`delivered_at` 说的是"结果交回去了"，`completed_at` 说的是"它跑完了"。
到期这一刻两件事都**没有发生** —— 发生的是"我们不再等了"。

所以这一格是独立的（`wait_expired_at`），并且它**不**把这条派生
从 `undelivered()` 里摘出去：那条子 Run 万一路回来，
唤醒路径照样认它（`undelivered()` 的谓词里没有这一列）。

缺了这一格会怎样（R-13 的同款）：
`overdue()` 是 `ORDER BY wait_until LIMIT %s`，
一条处置过却没退出的行，`wait_until` 永远最小 ——
于是它永久占着队首，攒够 LIMIT 条之后新到期的一个也进不来。

--------------------------------------------------------------------------
为什么这一支**不写** `failed`

见 `loop.child_wait_expired` 的注释。一句话版本：
"等不到结果"和"它失败了"是两件事，写成后者会让排障的人去查
一个可能压根没发生的失败（PR-19）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from packages.agent_domain.errors import IllegalTransition, InvariantViolation

from .orphans import record_child_orphan
from .recovery import RunRecovery


class WaitExpiryOutcome(str, Enum):
    """一次到期处置的结果。刻意不做成 bool —— "没处置"有几种，且含义不同。"""

    EXPIRED = "expired"
    """父 Run 不再等这条子 Run 了，且已经落了一份新的可恢复点。"""

    ALREADY_EXPIRED = "already_expired"
    """处置过了（重复扫 / 两个进程撞上），或者父 Run 本来就已经不等它了。
    正常情况，不是错误。"""

    PARENT_TERMINAL = "parent_terminal"
    """父 Run 已经终态 —— 结果永远无处可交（B-3），但这条子 Run 的
    副作用得有人记账（D-13），所以它照样要进账本。"""


@dataclass(frozen=True)
class WaitExpirySweepResult:
    """一轮兜底扫的结果。**刻意不返回一个数字**（PR-19）。

    `EXPIRED` 是"父 Run 被解开可以继续走"，
    `PARENT_TERMINAL` 是"没人会再要这个结果了，我们记了一笔孤儿"。
    把两者相加，一个满是孤儿的系统看起来和一切正常的系统一模一样 ——
    而"看得见"正是这一整轮要买的东西。
    """

    expired: tuple[str, ...] = ()
    parent_terminal: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.expired) + len(self.parent_terminal)


@dataclass
class ChildRunWaitExpirer:
    """把"等到上限也没有结果"这件事交回父 Run。

    三个协作者**都没有默认值**：

      registry  派生登记处（D-18 的队首住在这里）
      recovery  把父 Run 装载回来的唯一入口（R-3 / R-1）
      saga      D-13 孤儿的落点

    给 `saga` 一个默认值就意味着"没接上就静默跳过"，
    而那正是空洞 212~214 与 R-12 反复修过的那个形状：
    漏接不报错，只是那笔副作用从账本里消失。
    """

    registry: Any  #: `ChildRunRegistryPort`
    recovery: RunRecovery
    saga: Any  #: `SagaCoordinator`
    #: D-27：解开阻塞的人必须把父 Run **往前推**。刻意**不给默认值** ——
    #: M38 在 `expire()` 里留了一句"把下一步留给 `step()`"就走了，
    #: 而那个 `step()` 在跨进程部署里没有任何进程会来调（空洞 217）。
    #: 给默认值等于让"没接上"静默退化成"解开但不推"。
    driver: Any  #: `RunDriver`
    #: 上一轮的结果。给 `health()` 读的 —— 运维看板要能问
    #: "上一轮到底解开了几条挂起"。
    last_result: WaitExpirySweepResult = field(
        default_factory=WaitExpirySweepResult
    )

    def expire(
        self, child_run_id: str, *, now: datetime | None = None
    ) -> WaitExpiryOutcome:
        """处置一条到期的派生。

        `now` 是**这个 tick 的时刻**（PR-13）：判"有没有到期"和写
        "什么时候处置的"必须是同一个数，否则同一轮里会出现
        "判它到期用的是 09:51:00、写进去的处置时刻是 09:50:59" 这种
        处置早于到期的行 —— 而 `child_runs_wait_expired_after_deadline`
        那条 CHECK 会把它挡下来（正是这条 CHECK 抓到它的）。
        """
        handle = self.registry.for_child(child_run_id)
        if handle is None:
            raise LookupError(
                f"D-2: no registered child run {child_run_id!r}; its wait cannot "
                f"expire — only a derivation can be waited on"
            )
        if handle.is_wait_expired:
            return WaitExpiryOutcome.ALREADY_EXPIRED
        if handle.is_finished:
            # D-20：结果已经产生了。这一支该走唤醒路径（D-7），
            # 走到期路径会把"有结果"当成"没结果"处置（PR-19）。
            raise InvariantViolation(
                f"D-20: child run {child_run_id!r} already produced a result "
                f"({handle.status!r}); deliver it instead of declaring the wait over"
            )

        try:
            stack = self.recovery.rebuild(handle.parent_run_id)
        except IllegalTransition:
            # R-3：父 Run 已终态，结果永远不会被需要。
            #
            # 但"不会被需要"不等于"可以不管" —— 与唤醒路径同一条理由（D-13）：
            # 这条子 Run 可能已经在外部世界留下东西，而父 Run 已终态
            # 意味着没有任何一步会去管那笔副作用。
            #
            # 这里比唤醒路径更糟一层：唤醒路径至少**知道**结局，
            # 这里连"它还在不在"都不知道（D-19）。
            self._record_orphan(handle)
            self.registry.mark_wait_expired(child_run_id, expired_at=now)
            return WaitExpiryOutcome.PARENT_TERMINAL

        loop = stack.loop
        pending = loop.pending_child
        if pending is None or pending.child_run_id != child_run_id:
            # 父 Run 已经不等它了 —— 只有"结果交回过"或"闸门关过"才会走到这。
            # 与唤醒路径那道判据同款（那里是重复投递的第二道防线）：
            # 这里它是"已经解开过"的证据，不是错误。
            #
            # D-27 / D-29：仍然**推一把**。上一次推进可能崩在半路
            # （那时 `mark_wait_expired` 还没写），于是这条派生还留在
            # `overdue()` 里等着被再扫一次。推一把是幂等的。
            self.driver.drive(handle.parent_run_id)
            self.registry.mark_wait_expired(child_run_id, expired_at=now)
            return WaitExpiryOutcome.ALREADY_EXPIRED

        loop.child_wait_expired(child_run_id, reason=self._reason(handle))
        # D-8：交回（哪怕交回的是"什么都没有"）之后必须落一个新的可恢复点。
        # 不落的话，重建出来的父 Run 还是从挂起时那份快照装载 ——
        # 那份快照里写着 pending_child_id，于是它又回到"在等"（child_wake.py
        # 文件头那段"不落会怎样"，这里是它的第二个副本）。
        self.recovery.snapshots.save(
            loop.capture(reason=f"child run {child_run_id} wait expired")
        )
        # D-27：关掉闸门只是把 `pending_child` 清掉 —— 父 Run 停在
        # "可以被推一步"那一格。M38 到这里就 return 了，于是界面上
        # 那条 Run 永远显示"运行中"（空洞 217）。
        #
        # D-29：推进排在 `mark_wait_expired` **之前** —— 崩在这一步时，
        # 这条派生还留在 `overdue()` 里，下一轮扫会再推一次。
        self.driver.drive(handle.parent_run_id)
        # PR-33：先落库（上面的快照 + loop 内部的关闸门），
        # 后说"这次等待处置完了"。反过来会让这条派生永远留在队首。
        self.registry.mark_wait_expired(child_run_id, expired_at=now)
        return WaitExpiryOutcome.EXPIRED

    def sweep(self, now: datetime, limit: int = 64) -> WaitExpirySweepResult:
        """D-18 的兜底扫：扫"等到上限还没结果"的派生，逐个交回父 Run。

        `now` 由调用方传进来（PR-13）：一个 tick 只认一个"现在"，
        否则同一次扫描里前半批和后半批用的是两个不同的时刻。
        """
        expired: list[str] = []
        orphaned: list[str] = []
        for handle in self.registry.overdue(now, limit):
            if handle.is_finished:
                # 扫到它和处置它之间，结果来了。那不是"等不到" ——
                # 交给唤醒路径（`undelivered()` 同样扫得到它），
                # 这里不碰它，也不把它从任何队列里摘走。
                continue
            outcome = self.expire(handle.child_run_id, now=now)
            if outcome is WaitExpiryOutcome.EXPIRED:
                expired.append(handle.child_run_id)
            elif outcome is WaitExpiryOutcome.PARENT_TERMINAL:
                orphaned.append(handle.child_run_id)
        self.last_result = WaitExpirySweepResult(
            expired=tuple(expired), parent_terminal=tuple(orphaned)
        )
        return self.last_result

    def _record_orphan(self, handle: Any) -> None:
        record_child_orphan(self.saga, handle, headline=self._orphan_headline(handle))

    @staticmethod
    def _orphan_headline(handle: Any) -> str:
        """这一支独有的一半：**不知道**结局，且父 Run 已经终态。

        唤醒路径那一半写的是 "ended {status}"（知道结局），
        R-12（取消等不到回音）那一半写的是 "asked to stop ... produced no outcome"，
        这一半连"叫停"都没有 —— 它只是被等过，然后没人再等了。
        三种"没人负责的副作用"在账本上要能被一眼分开。
        """
        return (
            f"D-13/D-19: child run {handle.child_run_id} never reported any "
            f"result and its wait expired at {handle.wait_until}, but its parent "
            f"run {handle.parent_run_id!r} is already terminal, so nobody will "
            f"ever consume whatever it did — and WE DO NOT KNOW whether it is "
            f"still running or already dead, so this is recorded as UNRESOLVED, "
            f"not as a failure"
        )

    @staticmethod
    def _reason(handle: Any) -> str:
        return (
            f"child run {handle.kind.value} {handle.child_run_id} produced no "
            f"result before its wait deadline ({handle.wait_until})"
        )


__all__ = [
    "ChildRunWaitExpirer",
    "WaitExpiryOutcome",
    "WaitExpirySweepResult",
]
