"""子 Run 的结果**回传**父 Run（M30 / 空洞 209）。

--------------------------------------------------------------------------
这条链路断在哪

M25 让父 Run 派得出子 Run（D-1 保证不多派），M26 让派生登记活过重启。
但结果回来的那一半一直只有**测试**走过：

    AgentLoop.child_completed()   ← 只有 tests/ 直接调它
                                    生产路径上没有任何人来叫

于是委派的结果永远回不来：父 Execution 会一直 SUSPENDED，
界面上显示"在等子 Agent"，看起来完全正常。

补齐的这一段是：

    子 Run 进终态  ──X-3──▶  outbox: child_run.completed
                              │
                        (outbox_publisher → Kafka)
                              │
    apps/child_run_consumer  ◀┘  → ChildRunWaker.wake()  → 父 Run 恢复并继续

--------------------------------------------------------------------------
为什么结果从**登记处**取，不从事件里取（X-5）

事件是 Kafka 里的消息，有 retention；结果是"这条子 Run 干了什么"的
**事实**，属于 PG。唤醒路径读登记处，于是它**不依赖 Kafka**：
事件丢了，兜底扫（`sweep()`）照样拿得到结果，只是晚一点（A-12：变慢不变错）。

--------------------------------------------------------------------------
D-8：交回结果之后必须落一个新的可恢复点

不落会怎样：`AgentLoop.step()` 一见 `pending_child` 就返回 `WAITING_CHILD`
（loop.py 的那道闸门），而重建出来的父 Run 是从**挂起时**那份快照装载的 ——
那份快照里写着 `pending_child_id`。于是：

    子 Run 早就跑完了、结果也交回去了
      → 父 Run 每次被重建都还是"在等那条子 Run"
      → step() 永远 WAITING_CHILD
      → 一个已经拿到结果却永远走不下去的 Run

而且它不报错 —— 界面上是"等待子 Agent"，一切正常。
所以交付之后必须**重新拍一份快照**，把"我不再等它了"变成可恢复的事实。

--------------------------------------------------------------------------
重复投递（at-least-once）怎么不重复交付

`wake()` 有两道判据，都不靠"先读一下"：

  1. `handle.is_delivered` —— 登记处里那一行自己说了算
  2. 重建出来的父 Run 已经不在等它 —— 只有交回过结果才会清掉 `pending_child`

第 2 道是给"交付成功了、`mark_delivered` 还没写进去就崩"这一瞬间准备的。
两道都不成立才真的交付，所以重复投递最多多做一次 `rebuild()`，不会有第二次副作用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from packages.agent_domain.business.run import AgentRunStatus
from packages.agent_domain.errors import IllegalTransition, InvariantViolation

from .delegation import ChildRunHandle, ChildRunRegistryPort
from .orphans import reconcile_late_result, record_child_orphan
from .recovery import RunRecovery


class ChildWakeOutcome(str, Enum):
    """一次唤醒尝试的结果。刻意不做成 bool —— "没交付"有三种，且处置不同。"""

    DELIVERED = "delivered"
    """这次真的把结果交回父 Run 了。"""

    ALREADY_DELIVERED = "already_delivered"
    """交过了。重复投递 / 兜底扫撞上事件路径，正常情况，不是错误。"""

    PARENT_TERMINAL = "parent_terminal"
    """父 Run 已经是终态 —— 结果无处可交，且再也不会有人要（B-3）。
    照样登记交付：不登记的话兜底扫会每轮撞同一个 R-3。"""

    LATE = "late"
    """**迟到的结果**（D-22 / 空洞 231）：等待已经声明结束，它才回来。

    它和 `ALREADY_DELIVERED` 的区别不是时序，是**有没有人接过**：

        ALREADY_DELIVERED  父 Run 收到过这个结果
        LATE               从来没有人收到过它 —— 等它的人早就不等了

    并成一个值之后，看板上"交付完成"那个数会把"结果无人接收"
    也算进去，于是一个正在持续丢结果的系统看起来一切正常（PR-19）。
    """


@dataclass(frozen=True)
class ChildWakeSweepResult:
    """一轮兜底扫的结果。**刻意不返回一个数字**（PR-19，与 `WaitExpirySweepResult` 同款）。

    `delivered` 是"结果交到了等它的人手上"；
    `late` 是"结果到了，但等它的人早就不等了"；
    `parent_terminal` 是"没人会再要这个结果，我们记了一笔孤儿"。

    三者相加，一个正在持续丢结果的系统看起来和一切正常的系统一模一样 ——
    而"看得见"正是这一整条链路要买的东西（空洞 227）。
    """

    delivered: tuple[str, ...] = ()
    late: tuple[str, ...] = ()
    parent_terminal: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.delivered) + len(self.late) + len(self.parent_terminal)


@dataclass
class ChildRunWaker:
    """把一条子 Run 的结果交回它的父 Run。

    它是**幂等**的（上面那两道判据），所以事件路径与兜底扫可以共用同一个方法 ——
    两条路径各写一份"怎么交回结果"就会出现第二个定义（B-7 同款理由）。
    """

    registry: Any  #: `ChildRunRegistryPort`
    recovery: RunRecovery
    #: D-13：孤儿副作用的**唯一**落点。刻意**不给默认值**：
    #: 缺省就意味着"没接上就静默跳过"，而那正是空洞 212~214 那个形状 ——
    #: 漏接不报错，只是那笔副作用从账本里消失。
    saga: Any  #: `SagaCoordinator`
    #: D-27：解开阻塞的人必须把父 Run **往前推**。同样**不给默认值** ——
    #: 缺省就是"解开了但没人推"，而那正是空洞 217 的形状：
    #: 不报错，只是父 Run 停在"可以被推一步"那一格，永远不动。
    driver: Any  #: `RunDriver`

    def wake(self, child_run_id: str) -> ChildWakeOutcome:
        handle = self.registry.for_child(child_run_id)
        if handle is None:
            # 事件只会由"登记过的子 Run"发出（`AgentLoop.child_identity` 不为 None
            # ⟹ 它被 `spawn()` 绑进过登记处）。查不到说明登记处与事件流不一致，
            # 那是**事实层面**的矛盾，不是"可以等下一次"的暂态。
            raise LookupError(
                f"D-2: no registered child run {child_run_id!r}; a completion was "
                f"announced for a child run that was never bound — the registry and "
                f"the event log disagree"
            )
        if not handle.is_finished:
            raise InvariantViolation(
                f"D-6: child run {child_run_id!r} has no result yet; the completion "
                f"event cannot be handled before mark_finished() — if it can, the "
                f"result write and the event write are not in one transaction (X-3)"
            )
        if handle.is_delivered:
            return ChildWakeOutcome.ALREADY_DELIVERED
        if handle.is_wait_expired:
            # D-22：结果来了，但等它的人早就不等了。
            #
            # ----------------------------------------------------------
            # 为什么**不交付**
            #
            # D-7 的"把结果交回去"只对**还在等**的那个人成立。
            # 父 Run 已经被 `ChildRunWaitExpirer` 解开过（`wait_expired_at`
            # 非空 ⟹ 它不再挂在这条子 Run 上），它可能已经换了个目标重新派了一次。
            # 这时把旧结果插回去，等于让一个已经往前走过的人收到一条
            # 他不再期待的消息 —— 而 D-1 的派生键决定他不会认这条。
            #
            # ----------------------------------------------------------
            # 为什么仍然要 `mark_delivered`
            #
            # 不登记交付，这一行就**永久占着 `undelivered()` 的队首** ——
            # 与 R-13 同一个形状：`undelivered()` 也是 `ORDER BY ... LIMIT`，
            # 一条永远不动的行会把后面所有迟到的结果全挡住。
            #
            # ----------------------------------------------------------
            # 为什么不能返回 `ALREADY_DELIVERED`
            #
            # 那名字说的是"已经交过了"。而真实发生的是
            # "**从来没有人接过**" —— 一句谎话（PR-19）。
            self._reconcile_late(handle)
            self.registry.mark_delivered(child_run_id)
            return ChildWakeOutcome.LATE

        try:
            stack = self.recovery.rebuild(handle.parent_run_id)
        except IllegalTransition:
            # R-3：父 Run 已终态。结果永远不会被需要了。
            #
            # D-13：但"不会被需要"不等于"可以静默丢掉" ——
            # 这条子 Run 已经跑过了，它可能已经在外部世界留下东西，
            # 而父 Run 已终态意味着**没有任何一步**会去管那笔副作用。
            # 所以它必须进账本（见 `_record_orphan`）。
            #
            # ----------------------------------------------------------
            # D-30：**终态不等于"没接过"** —— COMPLETED 那一种是例外
            #
            # 父 Run 是 `COMPLETED` 时，这个结果**已经被接过了**：
            #
            #   · D-5：挂着 `pending_child` 的 Run 走不出 `WAITING_CHILD`，
            #     所以它不可能"一边等着这条子 Run、一边自己跑完"
            #   · 等待被声明过到期的那一支在上面就走了（D-22 / `LATE`）
            #   · 于是剩下的唯一路径是：结果交回去了 → 父 Run 被推进（D-27）
            #     → 跑完 → COMPLETED
            #   · 而 S-16 已经在那一刻把账本结案成 `NOT_NEEDED`
            #     （"成功 = 副作用按预期保留"）
            #
            # 此时再记一条 D-13 孤儿，账本上会同时写着
            # "不需要撤销"与"没人负责这笔副作用" —— 两句互相矛盾的话。
            #
            # 这个分支真正服务的窗口是 D-29 那一瞬间：推进跑完了、
            # `mark_delivered` 还没写进去就崩了。下一轮兜底扫会再撞上来，
            # 而那时**不许**把它说成孤儿。
            # ----------------------------------------------------------
            if self._parent_completed(handle.parent_run_id):
                self.registry.mark_delivered(child_run_id)
                return ChildWakeOutcome.ALREADY_DELIVERED
            self._record_orphan(handle)
            self.registry.mark_delivered(child_run_id)
            return ChildWakeOutcome.PARENT_TERMINAL

        loop = stack.loop
        pending = loop.pending_child
        if pending is None:
            # 已经交过了：只有 `child_completed` / `child_failed` 会清掉它。
            #
            # D-27：即便如此也要**推一把**。上一轮推进可能在半路崩了
            # （那时候 `mark_delivered` 还没写 —— 见下面 D-29 那段），
            # 于是这条子 Run 还留在 `undelivered()` 里等着被再扫一次。
            # 推一把是幂等的：已经推进过的 Run 要么已经在下一个阻塞点
            # （`run()` 立刻返回），要么是终态（R-3 → `TERMINAL`）。
            self.driver.drive(handle.parent_run_id)
            self.registry.mark_delivered(child_run_id)
            return ChildWakeOutcome.ALREADY_DELIVERED
        if pending.child_run_id != child_run_id:
            raise InvariantViolation(
                f"D-3: parent run {handle.parent_run_id!r} is waiting for child run "
                f"{pending.child_run_id!r}, not {child_run_id!r}"
            )

        # 三个终态，三个入口。刻意不做 `if completed: ... else: child_failed`：
        # 取消不是失败（S-15），并进一条路径就会把"到此为止"当成"没做成"（D-10），
        # 于是取消也被塞进可重试的失败分支 —— 那正是空洞 218 的形状。
        if handle.status == "completed":
            loop.child_completed(child_run_id, handle.result)
        elif handle.status == "cancelled":
            loop.child_cancelled(child_run_id, reason=self._reason(handle))
        else:
            loop.child_failed(child_run_id, reason=self._reason(handle))

        # D-8：交付之后立刻重拍快照。见文件头那段"不落会怎样"。
        self.recovery.snapshots.save(
            loop.capture(reason=f"child run {child_run_id} result delivered")
        )
        # D-27：解开阻塞 ≠ 往前走。交回结果只是把 `pending_child` 清掉，
        # 父 Run 此时停在"可以被推一步"那一格 —— 而跨进程部署里
        # **没有任何一个进程**会来推它（空洞 217）。
        #
        # D-29：推进**必须**排在 `mark_delivered` 之前。
        # 推进要调模型、调工具、动外部世界 —— 它是这条链路上最容易崩的一步。
        # 排反了的话，崩在这一步的后果是：
        #
        #     delivered_at 已经写了 → 兜底扫再也不会碰这条子 Run
        #     → 父 Run 停在半路，界面显示"运行中"，一切正常
        #
        # 排对了，崩溃的后果只是"下一轮兜底扫再推一次" ——
        # 变慢，不变错（A-12）。
        self.driver.drive(handle.parent_run_id)
        # consumers.py 的教条：**先 handler，再 mark**。
        # 反过来能做到"最多一次"，但会在崩溃时静默丢处理 ——
        # 对唤醒这种事，丢处理 = 父 Run 永远挂着，比重复处理难查得多。
        self.registry.mark_delivered(child_run_id)
        return ChildWakeOutcome.DELIVERED

    def sweep(self, limit: int = 64) -> ChildWakeSweepResult:
        """A-12 的兜底：扫 PG 里"已终态但还没交回"的那些，逐个唤醒。

        它让 Kafka 不再是唤醒路径的单点。事件丢了只是**变慢**
        （下一次 sweep 扫到），不会变成"父 Run 永远挂着"。
        与 `RecoveryController.sweep_every`（PG 全量兜底扫）是同一条思路的
        第二个副本：快路径可以丢，安全网不能没有。
        """
        delivered: list[str] = []
        late: list[str] = []
        orphaned: list[str] = []
        for handle in self.registry.undelivered(limit):
            outcome = self.wake(handle.child_run_id)
            if outcome is ChildWakeOutcome.DELIVERED:
                delivered.append(handle.child_run_id)
            elif outcome is ChildWakeOutcome.LATE:
                late.append(handle.child_run_id)
            elif outcome is ChildWakeOutcome.PARENT_TERMINAL:
                orphaned.append(handle.child_run_id)
        return ChildWakeSweepResult(
            delivered=tuple(delivered),
            late=tuple(late),
            parent_terminal=tuple(orphaned),
        )

    def _reconcile_late(self, handle: ChildRunHandle) -> None:
        """D-23 / D-25：真相到了，账本该变成什么样。

        真正的定义住在 `orphans.py`（`reconcile_late_result`）——
        "迟到的结果怎么落到账本上"只许有一个答案（B-7）。

        返回 `None`（没有这一行 / 这一行已经处置过了）都**不是错误**：

            · Action 没声明逆操作 —— 那就没有这笔账（S-1 的前提不成立）
            · 人工已经把它拉回 PENDING 并撤销掉了 —— 那是历史，不该改

        只有第一种情形下账本会留着"不知道"那句话，而那时它也确实
        无从可查：记这笔账的前提是"有可记的账"。

        ------------------------------------------------------------------
        为什么这里**不**只改一句话（空洞 233）

        D-23 收回的是"不知道"这三个字，没有收回"**撤销不了**"这个结论。
        子 Run 真的跑完了 ⟹ 副作用确实发生了 ⟹ 那笔账现在撤销得掉，
        而 `args` 还空着 —— 人工点"重试"会用**空参数**去调撤销工具
        （S-8 明文警告的后果）。所以 `completed` 那一支要升级（D-25），
        其余两支留在 UNRESOLVED 但必须说清挡着的是哪一样（D-26）。
        """
        reconcile_late_result(self.saga, handle)

    def _record_orphan(self, handle: ChildRunHandle) -> None:
        """D-13：父 Run 已终态时，把这条子 Run 的副作用记成"无人负责"。

        ------------------------------------------------------------------
        真正的定义住在 `orphans.py`

        空洞 226 带来第二个调用方（"叫停之后等不到回音"），
        两处必须写同一种记录。抄成两份就会有两个答案 ——
        所以这里只负责说清**这一支**的理由前半句，
        落笔那一半交给 `record_child_orphan`（B-7）。

        ------------------------------------------------------------------
        不记会怎样

        父 Run 已终态 ⟹ 没有任何一条 `step()` 会再走 ⟹
        委派那一步永远不会有 `child_completed` / `_finish_child`，
        于是补偿账本**永远缺这一条**。

        而子 Run 是**已经跑过**的：它可能建了工单、发了邮件、派生了它自己的子 Run。
        那些东西留在外部世界，账本里一行都没有 ——
        运维看板上这次委派像从未发生过。

        ------------------------------------------------------------------
        为什么 `step_id` 是空的，而且这是**如实**而非敷衍

        父 Run 已终态 ⟹ `rebuild()` 抛 `IllegalTransition`（R-3）⟹
        拿不到它的 Step 对象。所以这里**说不出**是哪一步。

        刻意不填一个假的：账本缺一格是可查的缺失，
        而一个填错的 step_id 会把排查引到错误的那一步去 ——
        那正是 PR-19 那一类错（报错说的和真实发生的不是同一件事）。
        `task_id` 与 `execution_id` 是**精确**的，S-1 要求的"哪条 Task 产生
        的副作用"说得清；理由里也写明了 step 为什么是空的。
        """
        record_child_orphan(self.saga, handle, headline=self._orphan_headline(handle))

    def _parent_completed(self, parent_run_id: str) -> bool:
        """D-30：父 Run 是不是**跑完**了，而不是停在别的终态上。

        ------------------------------------------------------------------
        为什么必须问存储，不能问 `rebuild()`

        R-3 把"终态 Run 不可恢复"做成一堵墙：终态一律抛 `IllegalTransition`，
        不问是哪一种。于是"它到底是 COMPLETED 还是 CANCELLED"这个答案
        只能从存储里读 —— 而存储里的权威是**最新一份快照**。

        ------------------------------------------------------------------
        为什么只有 COMPLETED 算"接过了"

        另外三个终态（`FAILED` / `CANCELLED` / `TIMED_OUT`）都是
        "**没等到就结束了**"：父 Run 要么是等待被声明到期后自己放弃了
        （那一支在上面走了 D-22 / `LATE`），要么是被叫停（B-8）。
        这两种情形下这条子 Run 的副作用真的没人认领 —— 记孤儿是对的。

        `COMPLETED` 反过来：它意味着父 Run **收到过结果并把它用掉了**
        （D-5 挡住了"一边等它一边自己跑完"）。所以那笔账已经结案（S-16），
        再记孤儿就是让账本自相矛盾。
        """
        snapshot = self.recovery.snapshots.latest(parent_run_id)
        if snapshot is None:
            # 走到这一支说明上面 `rebuild()` 抛了 `IllegalTransition`，
            # 而那只有"最新快照是终态"一种成因（R-3）—— 所以这里拿不到
            # 是**事实层面的矛盾**，不是"还没落快照"的暂态。
            # 如实按"没接过"处理：宁可多记一条孤儿，也不要把孤儿说成已交付。
            return False
        return snapshot.status == AgentRunStatus.COMPLETED.value

    @staticmethod
    def _orphan_headline(handle: ChildRunHandle) -> str:
        """这一支独有的一半：结果**已经产生**，只是无处可交。

        与放弃路径那一半的区别不是措辞：
        这里**知道**结局（"ended {status}"），放弃路径**不知道**。
        账本上这两行要能被一眼分开，所以各自写各自的。
        """
        return (
            f"D-13: child run {handle.child_run_id} ended {handle.status!r} but "
            f"its parent run {handle.parent_run_id!r} is already terminal, so no "
            f"step will ever consume this result — whatever the child run already "
            f"did stays in the outside world with nobody owning it."
        )

    def _reason(self, handle: ChildRunHandle) -> str:
        result = dict(handle.result or {})
        summary = str(result.get("summary") or "").strip()
        tail = f": {summary}" if summary else ""
        if handle.status == "cancelled":
            return f"child run cancelled{tail}"
        return f"child run failed{tail}"


__all__ = [
    "ChildRunWaker",
    "ChildWakeOutcome",
    "ChildWakeSweepResult",
]
