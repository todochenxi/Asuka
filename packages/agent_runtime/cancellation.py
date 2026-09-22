"""**Run 级**取消（M34 / 空洞 222）。

--------------------------------------------------------------------------
为什么 Execution 级的取消不够

Kernel 有一套完整的取消（`packages/execution_kernel/cancellation.py`）：
Durable Intent（PG）+ Fast Signal（Redis）+ Worker 在安全点检查 Token。
但它挂的是 **Execution**。

一条子 Run 不是一条 Execution：

* 它有**自己**的一堆 Execution（它也要调模型、调工具、派生孙 Run）；
* 它在等孙 Run 的时候，**手上根本没有活的 Execution** ——
  那条因为闸门而 SUSPENDED 的 Execution 是"它在等"的证据，
  不是"它在跑"的证据。往它上面写 `cancellation_requested`
  等于取消一次等待，而等待结束之后它还会继续往下走。

所以 M33 补的 Run 级取消在**跨进程**下只走通了一半：
父 Run 能把登记处那一行判成 `cancelled`，但跑在另一个进程里的那条 Run
没有任何人告诉它。两件事实同时成立，而它们说的是相反的东西：

    父侧：这条子 Run 的结果我不要了
    子侧：我还活着，我还在跑

--------------------------------------------------------------------------
三段式（与 Kernel 同构）

    Durable Intent   PostgreSQL `run_cancellations`（唯一事实来源）
    Safe Point       `AgentLoop._step()` 顶部：协作式，读到就自己停
    Sweeper          系统级：兜住所有"根本走不到安全点"的 Run

**Safe Point 兜不住的那些才是这套东西存在的理由。**
一条停在 `WAITING_CHILD` 的 Run 不会调 `step()` —— 它在等别人。
它这一辈子可能再没有第二个安全点。

--------------------------------------------------------------------------
    R-7  取消**意图**必须先于取消**宣告**落库
    R-8  被叫停的 Run 认领意图之后必须把它结掉（`settle`），
         否则 Sweeper 会一遍又一遍地叫停同一条 Run
    R-9  Sweeper 遇到"已经终态"的 Run 不是失败，是**已经完成** ——
         它要结掉那条意图，而不是报错
    R-10 `settled_at` 是"这条 Run **确实停了**"的证据，
         不是"取消被请求过"的证据。结掉一条还没停的 Run 的意图，
         等于在系统里写下一句"我取消了它"而它还活着（PR-19）

--------------------------------------------------------------------------
R-10 有一个**前提**，而它在 M34 里没被写出来（空洞 226）

    "不结，让它继续 pending" 只在**它迟早会到达一个安全点**时才成立。

一条正在往前跑的 Run 会调 `step()`（读到意图）；
一条挂起的 Run 会有快照（下一轮 `rebuild()` 拿得到）。
两条路都通向"这条意图终将被认领"。

可如果那条 Run 的**进程彻底没了**，两条路**都不通**：
它永远不会有下一个安全点，也永远不会再拍一次快照。
那条意图就永远留在 pending 里。

M34 当时只算了"一条"僵尸意图的代价，然后按 A-12 判成
"变慢且看得见，比变错且没人知道好"。**这个判断漏了一件事**：

    `pending()` 是 `WHERE settled_at IS NULL ORDER BY requested_at LIMIT %s`

—— 它有 `ORDER BY`，也有 `LIMIT`。
僵尸意图的 `requested_at` 最老，于是它**永久占据队首**：
每轮捞出来、撞 R-10、`continue`、下一轮再捞出来。
攒够 `LIMIT` 条之后，**新提交的取消请求一条都进不了扫描窗口**，
而 `sweep()` 每轮返回 0 —— 界面上是"没有待处理的取消"。

那不是变慢，是**跨进程取消通道停止服务**，而且它什么都不喊。

--------------------------------------------------------------------------
所以 M37 补上另一半：等待必须有**上限**（R-11）

    R-11  每一条取消意图都带一个等待上限（`abandon_after`）。
          到点仍无结局 ⟹ 系统**放弃等待**（`abandoned_at`）。
          放弃是"**我们不知道**它停没停"，不是"它停了"，
          所以放弃**不得**写终态、`settled_at`（D-14 / PR-19）。
    R-12  放弃一条**子 Run** 的等待时，若其父 Run 已终态，
          必须补记一条 D-13 孤儿，理由必须点名"结局未知"。
          —— 那笔副作用从此再没有别的入口会记它。
    R-13  放弃过的意图必须**退出 pending 队列**。
          否则"让路"这件事根本没发生：它照样排队首、照样占槽位。
          索引的谓词是这件事**唯一**的落点。
    R-14  放弃**不是撤回**。那条 Run 后来若撞上安全点，仍应停下来 ——
          用户按的"停止"不因为我们等累了就作废。
          所以 `settled_at` 与 `abandoned_at` 允许同时有值：
          前者是"它停了"（事实），后者是"我们不等了"（决策），两件独立的事。

--------------------------------------------------------------------------
为什么"等不到回音"只能靠上限，不能靠**检测死亡**

系统里最接近活性证据的是 Kernel 的租约（`LEASE_EXPIRED`）——
但它在 Kernel 的语义里是**可重试**的，意思是
"这个 worker 不续约了，换一个 worker 接着来"，不是"这条 Run 没了"。
拿它当死亡证明会误杀一条正在被 Recovery 救活的 Run。

而一条 **Run** 除此之外没有任何活性证据：没有心跳列，
快照只在挂起时拍（R-1），正在往前跑的 Run 在 PG 里什么都不写。

所以：**没有死亡检测器，只有等待上限。**
这不是凑合，前提是这个上限到期后的动作必须被诚实地记成"不知道"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Sequence

from packages.agent_domain.errors import IllegalTransition

from .cancellation_events import emit as _emit_cancellation_event
from .orphans import record_child_orphan


#: 一条取消意图最多等这么久还没有回音，系统就**放弃等待**（R-11）。
#:
#: 这个数不是算出来的，是这两条之间的折中：
#:   · 健康的系统里，Sweeper 3 秒一轮、安全点协作式 —— 秒级就该有结局。
#:     15 分钟宽松到几乎不可能误伤一条正常的 Run。
#:   · 而一条僵尸意图每多留一刻，就多占一个 `pending()` 的槽位 ——
#:     堵满 64 个槽位之后整条通道停止服务（R-13 那段）。
#:     15 分钟足够让运维在通道堵死之前看见并介入。
#:
#: ⚠️ 迁移 `014_cancellation_grace.sql` 里回填历史行用的也是 15 分钟。
#:    两处必须一致 —— 有一条测试盯着它们（`test_the_grace_in_sql_and_in_python_agree`）。
DEFAULT_CANCELLATION_GRACE = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunCancellation:
    """一条 Run 级取消意图。

    `reason` 与 `by` 是**构造期**必填，不是可选：
    一条查不到是谁、说不出为什么的 `cancelled` 等于取消这件事没有发生过
    （B-8 / A-8 同款）。
    """

    run_id: str = ""
    reason: str = ""
    by: str = ""
    requested_at: datetime = field(default_factory=_utcnow)
    settled_at: datetime | None = None

    #: R-11：等到什么时候为止。**在 request 那一刻**固化 ——
    #: 后来把策略调长，不该追溯地改变一条已经提交的老意图：
    #: "这条请求当初承诺过多久"是审计的一部分。
    #:
    #: 类型是 `| None` 只为了迁就"还没迁移的历史行"；
    #: 写入路径上它**永远有值**，`run_cancellations_deadline_required` 兜底（PR-26）。
    abandon_after: datetime | None = None

    #: R-11：**我们**不再等了的那一刻。与 `settled_at` 可以同时有值（R-14）。
    abandoned_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("RunCancellation.run_id is required")
        if not self.reason:
            raise ValueError("B-8: RunCancellation.reason is required")
        if not self.by:
            raise ValueError("B-8: RunCancellation.by is required")

    @property
    def is_settled(self) -> bool:
        """那条 Run **确实停了**（R-10 的证据）。"""
        return self.settled_at is not None

    @property
    def is_abandoned(self) -> bool:
        """**我们**不再等了（R-11）。

        刻意不叫 `is_expired` —— 到点（`is_expired`）是**该**放弃，
        放弃（`is_abandoned`）是**已经**放弃。中间隔着一次 Sweeper，
        而崩在中间正是"让路没发生"的那种残局。
        """
        return self.abandoned_at is not None

    @property
    def is_pending(self) -> bool:
        """还在扫描窗口里等着被推进（R-13）。

        `not is_settled` **不等于** pending：放弃过但还没停的意图
        既不是 settled 也不在队列里。混淆这两件事，
        就是 011 那个部分索引当初漏掉 `abandoned_at IS NULL` 的原因。
        """
        return self.settled_at is None and self.abandoned_at is None

    def is_expired(self, now: datetime) -> bool:
        """到点了吗？（`abandon_after` 已过，且还没有任何结局。）

        没有 `abandon_after` 的历史行**永不**到期 ——
        那不是"放宽"，是"我们没有资格替一条不知道承诺过多久的请求决定放弃"。
        """
        if not self.is_pending:
            return False
        if self.abandon_after is None:
            return False
        return now >= self.abandon_after


class RunCancellationStore(Protocol):
    """Run 级取消意图的存储。**必须持久**（A-12）。

    放内存里等于没有：跨进程取消的全部意义就是"写到另一个进程看得到的地方"。
    """

    def request(self, run_id: str, *, reason: str, by: str) -> RunCancellation: ...

    def for_run(self, run_id: str) -> RunCancellation | None: ...

    #: 还在扫描窗口里的那些（Sweeper 的主入口）。按 `requested_at` 升序。
    #:
    #: R-13：**放弃过的必须不在里面**。这是"让路"唯一的落点 ——
    #: 别处不管谁进扫描窗口。
    def pending(self, limit: int = 64) -> Sequence[RunCancellation]: ...

    #: 到点了还没有任何结局的那些（放弃路径的入口）。按 `abandon_after` 升序 ——
    #: 最该被放弃的是**最早到点**的那条，不是最早请求的那条（R-11）。
    def expiring(self, now: datetime, limit: int = 64) -> Sequence[RunCancellation]: ...

    def settle(self, run_id: str) -> bool: ...

    #: R-11：记下"我们不再等了"。返回是否真的写了（判胜负靠 rowcount）。
    def abandon(self, run_id: str) -> bool: ...


@dataclass
class InMemoryRunCancellationStore:
    """测试用。生产必须换成 PG 版 —— 见端口上方的理由。

    `grace` 只在**写入**那一刻用到（`request()` 把上限固化进记录里）。
    放在 store 上而不是 service 上，是因为"这条意图当初承诺过多久"
    只有**写它的那个人**知道 —— service 事后改 grace 不该追溯地
    改写一条已经提交的老意图（B-7：一个事实一处定义）。
    """

    _by_run: dict[str, RunCancellation] = field(default_factory=dict, repr=False)
    grace: timedelta = DEFAULT_CANCELLATION_GRACE
    #: X-3 / X-15（M47）：事件落点。`None` = 不发（显式选择）。
    events: Any = None

    def request(self, run_id: str, *, reason: str, by: str) -> RunCancellation:
        existing = self._by_run.get(run_id)
        if existing is not None and (existing.is_settled or existing.is_abandoned):
            # R-8：已经结掉的意图不复活；R-11：已经放弃的也不复活。
            #
            # 后者为什么是同一条判据：放弃的时候**已经落过一笔账**
            # （R-12 那条孤儿，理由写着"我们不再等了"）。
            # 让它重新进 pending，就会出现同一件事两个答案 ——
            # 账本说"不等了"，队列说"还在等"（B-7）。
            # M47：不复活 = 不发事件（这件事没有变化，只是原样返回）。
            return existing
        now = _utcnow()
        request = RunCancellation(
            run_id=run_id,
            reason=reason,
            by=by,
            requested_at=now,
            abandon_after=now + self.grace,
        )
        self._by_run[run_id] = request
        _emit_cancellation_event(self.events, request, event_type="cancellation.requested")
        return request

    def for_run(self, run_id: str) -> RunCancellation | None:
        return self._by_run.get(run_id)

    def pending(self, limit: int = 64) -> Sequence[RunCancellation]:
        # R-13：`is_pending` 而不是 `not is_settled` —— 放弃过的必须不在这里。
        out = [r for r in self._by_run.values() if r.is_pending]
        out.sort(key=lambda r: r.requested_at)
        return out[:limit]

    def expiring(self, now: datetime, limit: int = 64) -> Sequence[RunCancellation]:
        out = [r for r in self._by_run.values() if r.is_expired(now)]
        out.sort(key=lambda r: r.abandon_after or r.requested_at)
        return out[:limit]

    def settle(self, run_id: str) -> bool:
        existing = self._by_run.get(run_id)
        if existing is None or existing.is_settled:
            return False
        updated = RunCancellation(
            run_id=existing.run_id,
            reason=existing.reason,
            by=existing.by,
            requested_at=existing.requested_at,
            settled_at=_utcnow(),
            abandon_after=existing.abandon_after,
            # R-14：放弃过的事实**不**因为后来停了就被抹掉。
            abandoned_at=existing.abandoned_at,
        )
        self._by_run[run_id] = updated
        _emit_cancellation_event(self.events, updated, event_type="cancellation.settled")
        return True

    def abandon(self, run_id: str) -> bool:
        existing = self._by_run.get(run_id)
        if existing is None or not existing.is_pending:
            return False
        updated = RunCancellation(
            run_id=existing.run_id,
            reason=existing.reason,
            by=existing.by,
            requested_at=existing.requested_at,
            settled_at=existing.settled_at,
            abandon_after=existing.abandon_after,
            abandoned_at=_utcnow(),
        )
        self._by_run[run_id] = updated
        _emit_cancellation_event(self.events, updated, event_type="cancellation.abandoned")
        return True


@dataclass(frozen=True)
class CancellationSweepResult:
    """一轮 sweep 的成绩。**刻意不做成一个 list**（`ChildWakeOutcome` 同款理由）。

    两种结局的处置完全不同，混成"扫过 N 条"就把它们压成了一件事：

        `settled`   那条 Run **确实停了** —— 正常收尾，账清了
        `abandoned` 我们**不知道**它停没停 —— 要人去看，账上记着"未知"

    一个每轮 `abandoned=3` 的系统和一个每轮 `settled=3` 的系统
    是两种完全不同的健康状态，而把它们加在一起
    会得到一个看起来很健康的数字（PR-19）。
    """

    settled: tuple[str, ...] = ()
    abandoned: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.settled) + len(self.abandoned)


class RunCancellationService:
    """把"意图"推进到"终态"（R-7 / R-8 / R-9 / R-11 · R-12 · R-13）。

    `recovery` 只在 `sweep()` 时用得到。不给它，`sweep()` 就如实说
    "我推进不了" —— 而不是静默返回 0。

    `child_registry` 与 `saga` 只在**放弃**时用得到（R-12），同样是**不给就喊**：
    放弃等待却不记账，等于让那条子 Run 的副作用从账本里静默消失 ——
    那正是空洞 226 要治的东西。一个"治了但没记账"的修复
    比不治更难发现：它把队列腾干净了，看起来一切正常。
    """

    def __init__(
        self,
        store: RunCancellationStore | None = None,
        *,
        recovery: Any = None,
        child_registry: Any = None,
        saga: Any = None,
    ) -> None:
        self.store = store if store is not None else InMemoryRunCancellationStore()
        self.recovery = recovery
        self.child_registry = child_registry
        self.saga = saga

    # ------------------------------------------------------------ 意图
    def request(self, run_id: str, *, reason: str, by: str) -> RunCancellation:
        return self.store.request(run_id, reason=reason, by=by)

    def for_run(self, run_id: str) -> RunCancellation | None:
        return self.store.for_run(run_id)

    def is_requested(self, run_id: str) -> bool:
        request = self.store.for_run(run_id)
        return request is not None and not request.is_settled

    def settle(self, run_id: str) -> bool:
        """R-8：认领完了就结掉。

        不结的话 Sweeper 每轮都会再叫停它一次 ——
        第一次是真的取消，之后每一次都是撞 B-10（终态不可取消）的抛。
        一个会一直抛的后台进程比一个不干活的后台进程更难发现。

        R-10：只有**那条 Run 真的停了**才允许调它。
        所以入口只有两个：那条 Run 自己（`AgentLoop.cancel()` 的末尾）
        与 Sweeper 确认它已经终态之后（R-9）。
        """
        return self.store.settle(run_id)

    # ------------------------------------------------------------ 推进
    def sweep(self, limit: int = 64) -> list[str]:
        """把"取消意图"推进到"终态"。

        ------------------------------------------------------------------
        为什么必须存在（与 Kernel 的 `CancellationService.sweep` 同构）

        协作式取消只对**正在往前走**的 Run 有意义。
        一条停在 `WAITING_CHILD` / `WAITING_APPROVAL` 的 Run 不会调 `step()`，
        于是它的安全点永远不会到来 —— 而它恰恰是最该被叫停的那一条：
        它在等一个因为父 Run 被取消而**永远不会再有价值**的结果。

        ------------------------------------------------------------------
        R-9：已经终态不是失败

        `RunRecovery.rebuild()` 对终态 Run 抛 `IllegalTransition`（R-3）。
        在这里那不是错误 —— 那正是我们**想要**的结局：
        这条 Run 已经停了，只是不是被这条意图停的
        （可能它自己跑完了，可能另一个副本已经处理过这条意图）。
        所以：结掉它，继续下一条。

        ------------------------------------------------------------------
        R-10：重建不出来 ≠ 已经停了

        `LookupError` 的意思只是"没有快照"。而快照只在**挂起**时拍（R-1），
        所以一条正在往前跑、从没挂起过的 Run 也没有快照 —— 它活得好好的。
        这时候结掉意图，等于在系统里写下"我取消了它"而它还在跑（PR-19），
        而且从此**再没有人**会去叫停它：意图已经不在 pending 里了。

        所以这里**什么都不做**，让它继续 pending。它会被下面两条路接住：
        正在跑 → 它自己下一个安全点读到（`AgentLoop._step()`）；
        挂起了 → 快照出现，下一轮 sweep 就能重建。

        ------------------------------------------------------------------
        ⚠️ 但"什么都不做"只在这些路**真的存在**时才成立（空洞 226）

        上面那两条路的共同前提是：那条 Run 还会有下一个安全点、
        或者还会再拍一次快照。进程彻底没了的时候**两条都不通**。

        这时"什么都不做"不再是等待，是**永久占着队首**：
        `pending()` 有 `ORDER BY requested_at` 也有 `LIMIT`，
        老意图排在前面每轮都被捞出来又放回去，
        攒够 `LIMIT` 条之后新的取消请求**一条都进不来**，
        而 `sweep()` 每轮返回 0 —— 界面上是"没有待处理的取消"。

        所以这一轮先**放弃**掉那些到点的（R-11），再推进剩下的。
        顺序不能反：先让路，同一个 tick 里腾出来的槽位立刻就能用上。
        """
        if self.recovery is None:
            raise RuntimeError(
                "RunCancellationService.sweep() requires a RunRecovery; "
                "without one the intent can never be adopted, and a cancellation "
                "that is never adopted is not a cancellation"
            )

        abandoned = self._abandon_expired(limit)
        settled = self._adopt_pending(limit)
        return CancellationSweepResult(
            settled=tuple(settled), abandoned=tuple(abandoned)
        )

    # ------------------------------------------------------------ 让路（R-11）
    def _abandon_expired(self, limit: int) -> list[str]:
        """到点了还没有任何结局的那些：**放弃等待**（R-11 / R-13）。"""
        due = list(self.store.expiring(_utcnow(), limit))
        if not due:
            return []

        if self.child_registry is None or self.saga is None:
            raise RuntimeError(
                "R-12: RunCancellationService needs a ChildRunRegistryPort and a "
                "SagaCoordinator before it can abandon a wait; abandoning without "
                "booking the orphan lets a child run's side effects vanish from the "
                "compensation ledger in silence — which is exactly what 226 is "
                "about. A queue that drains while the ledger stays empty is worse "
                "than a queue that is blocked, because it looks healthy"
            )

        out: list[str] = []
        for request in due:
            # PR-33：先落账，再说"我让路了"。
            #
            # 反过来（先 abandon 后记账）崩在中间，那条意图已经退出扫描窗口，
            # **再也不会有人**回来补这一笔 —— 孤儿从此永久消失。
            self._book_abandonment(request)
            if self.store.abandon(request.run_id):
                out.append(request.run_id)
        return out

    def _book_abandonment(self, request: RunCancellation) -> None:
        """R-12：放弃一条**子 Run** 的等待时，把它的副作用补进账本。

        ------------------------------------------------------------------
        只处理"它是一条被派生的 Run，而且它的父 Run 已经终态"

        三条提前返回，每条都是一种**不同**的局面，不是同一个判断写了三遍：

        1. 登记处查不到 ⟹ 它是一条根 Run，没有"父"来承接这笔账。
           放弃照样发生（让路不能因为没账可记就不做），只是没得记。
        2. 父 Run 还没终态 ⟹ 那不是孤儿，那是**父还在等一个不会来的结果**
           —— 另一件事（空洞 229），本轮不治，但**不能**在这里冒充孤儿记一笔：
           D-13 的前提是"父已终态、没有任何一步会再去管"，这里不成立。

        ------------------------------------------------------------------
        为什么理由里必须写"我们不知道"

        这条记录是**唯一**会留下来的东西。把它写成 `cancelled`，
        运维看到的是"已取消，无副作用" —— 而那条 Run 可能已经把工单建好了。
        一张说谎的账本比一张写着"不知道"的账本坏得多（PR-19）。
        """
        assert self.child_registry is not None
        assert self.saga is not None
        handle = self.child_registry.for_child(request.run_id)
        if handle is None:
            return
        if not self._parent_is_terminal(handle.parent_run_id):
            return
        record_child_orphan(
            self.saga,
            handle,
            headline=(
                f"R-12: child run {handle.child_run_id} was asked to stop "
                f"(reason {request.reason!r}, by {request.by!r}) but produced no "
                f"outcome before the wait expired at "
                f"{request.abandon_after.isoformat() if request.abandon_after else '?'}"
                f"; its parent run {handle.parent_run_id!r} is already terminal, so "
                f"nobody will ever consume whatever it already did — and WE DO NOT "
                f"KNOW whether it stopped or finished, so this is recorded as "
                f"UNRESOLVED, not as a cancellation"
            ),
        )

    def _parent_is_terminal(self, parent_run_id: str) -> bool:
        """父 Run 是不是已经终态？判据与 `ChildRunWaker.wake()` 那一支**同源**。

        `IllegalTransition` = R-3 = 已终态；`LookupError` = 没有快照 = 还在跑。
        刻意复用 `rebuild()` 的异常类型而不是另写一份 `status == ...` 的判断：
        "终态"这件事只有 Run 自己说得准（B-7 / R-3）。
        """
        assert self.recovery is not None
        try:
            self.recovery.rebuild(parent_run_id)
        except IllegalTransition:
            return True
        except LookupError:
            return False
        return False

    # ------------------------------------------------------------ 认领（R-9）
    def _adopt_pending(self, limit: int) -> list[str]:
        """还在扫描窗口里的那些：推进到终态。"""
        settled: list[str] = []
        for request in self.store.pending(limit):
            try:
                stack = self.recovery.rebuild(request.run_id)
            except LookupError:
                # R-10：还在跑（或从来没有过）。不结，等下一个安全点。
                continue
            except IllegalTransition:
                # R-9：已经终态。这就是要的结果，不是错误。
                self.store.settle(request.run_id)
                settled.append(request.run_id)
                continue
            stack.loop.cancel(reason=request.reason, by=request.by)
            # `cancel()` 内部会 settle —— 这里不再做一次（B-7）。
            settled.append(request.run_id)
        return settled


__all__ = [
    "DEFAULT_CANCELLATION_GRACE",
    "CancellationSweepResult",
    "InMemoryRunCancellationStore",
    "RunCancellation",
    "RunCancellationService",
    "RunCancellationStore",
]
