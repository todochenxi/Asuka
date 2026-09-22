"""Saga / Compensation —— M10（基线 §42：建立在 M15 Kernel 之上）。

**S-1：补偿是一等 Execution，不新开执行通道。**
撤销动作就是一条普通的 Task → Execution，Retry / Lease / Attempt / Idempotency
全部复用现成的 Kernel 能力。这里只回答三个问题：

    登记什么    `record()`   —— 正向执行留下了副作用，记一笔"待撤销"
    按什么顺序  LIFO         —— 逆序撤销（S-3）
    撤销不掉怎么办 UNRESOLVED —— 不静默、不阻断其余（S-5 / S-6）

归属（四边界）：

    Runtime（本文件）  决定**要不要撤销**、按什么顺序、撤销不掉怎么收尾
    Kernel             负责把撤销动作**可靠地跑完**（它不知道这是撤销）
    Harness            在正向动作提出时**连同逆操作一起**过策略（S-9，见 harness.py）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from packages.agent_domain.business.compensation import (
    CompensationRecord,
    CompensationStatus,
)
from packages.agent_domain.errors import ConcurrentStateError, InvariantViolation
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_runtime.compensation_events import (
    COMPENSATION_AMENDED,
    COMPENSATION_UPGRADED,
    emit,
    status_event_type,
    transition_event_type,
)

# ---------------------------------------------------------------- 端口


class CompensationStore(Protocol):
    """补偿账本。

    **A-12 的判据：这份数据丢了会变错，不会变慢** ——
    副作用还在，但没人知道要撤销。所以实现必须是 PG，内存版只用于测试与对照。
    """

    def add(self, record: CompensationRecord) -> None:
        """登记一条待撤销记录。同一 `execution_id` 只能有一条（S-2）。

        X-15：登记就是一次变化 —— 记完不发事件，下游就永远不知道
        这个世界上有这么一笔副作用等着撤销。
        """
        ...

    def get(self, compensation_id: str) -> CompensationRecord | None:
        ...

    def get_by_execution(self, execution_id: str) -> CompensationRecord | None:
        ...

    def open_for(self, run_id: str) -> Sequence[CompensationRecord]:
        """S-3：按**产生顺序倒序**返回未完成的（PENDING / RUNNING）。"""
        ...

    def unresolved_for(self, run_id: str) -> Sequence[CompensationRecord]:
        ...

    def claim(self, compensation_id: str) -> CompensationRecord | None:
        """S-4：原子地把 PENDING 认领成 RUNNING。抢不到返回 `None`。

        与 A-11 同源：两个 Coordinator 同时扫到同一条记录时，
        "先读一下是不是 PENDING 再写"两边都会通过 —— 然后撤销两次。
        所以认领必须是一次 `UPDATE ... WHERE status='pending'`。
        """
        ...

    def save(self, record: CompensationRecord) -> None:
        """写回（带乐观锁）。"""
        ...

    def upgrade_to_compensable(
        self, execution_id: str, *, args: Mapping[str, Any], reason: str
    ) -> CompensationRecord | None:
        """D-25：把一条"**没有撤销参数**"的 UNRESOLVED 升级成 PENDING。

        与 D-23 的分工：那是**改一句话**（账还是"撤销不了"），
        这是**改一个结论**（它现在撤销得掉了）。

        返回 `None` 的三种情形，都与 `amend_reason` 同款：没有这一行、
        已经处置过了、或者它本来就不缺撤销参数。第三种不是"缺参数"那一类，
        真相到达不构成自动重试的理由（S-14 唯一例外是**人工**拉回）。

        ------------------------------------------------------------------
        为什么判据必须在 SQL 的 WHERE 里

        与 S-4 / A-11 / D-23 是同一个陷阱的又一个副本：两个进程同时到达时，
        "先读一下是不是 UNRESOLVED 且 args 为空再写"两边都会通过 ——
        然后撤销两次。所以判胜负靠 `UPDATE ... WHERE` 的 rowcount。
        """
        ...

    def amend_reason(
        self, execution_id: str, *, reason: str
    ) -> CompensationRecord | None:
        """D-23：把一条 UNRESOLVED 的**理由**换成刚查明的真相。

        返回 `None` 的两种情形：

            · 这条 Execution 没有账本行 —— 没有可改的
            · 这一行**已经处置过了**（COMPENSATED / NOT_NEEDED / PENDING）
              —— 那是历史，不是草稿

        两种都不新建行（S-2）："补一句真相"绝不能变成"多一笔账"。

        ------------------------------------------------------------------
        为什么判据必须在 SQL 的 WHERE 里，不能先读再写

        与 S-4 / A-11 是同一个陷阱的又一个副本：两个进程同时到达时，
        "先读一下是不是 UNRESOLVED 再写"两边都会通过。
        所以判胜负靠 `UPDATE ... WHERE status='unresolved'` 的 rowcount。
        """
        ...


class InMemoryCompensationStore:
    """测试 / 单进程用的实现。

    ⚠️ 它**不能**证明 S-4 成立：内存里"认领"是同步的，两个协程根本抢不起来。
    真正的并发保证在 PG 实现里（见 adapters/postgres.py）。
    """

    def __init__(self, events: Any | None = None) -> None:
        """`events` 是事件落点（X-3：必须与状态写入同一事务）。

        PG 那边传 `PostgresOutboxStore(conn)`；这里传 `InMemoryOutbox()`。
        不传 = 这一本账不发事件（显式选择，不是"还没接上"）。
        """
        self._by_id: dict[str, CompensationRecord] = {}
        self._by_execution: dict[str, str] = {}
        # X-15：上一次**存储边界**上的状态。
        #
        # 不能靠"读一眼 `self._by_id` 里那条"来回答"改之前是什么"：
        # 字典里存的是**同一个对象**，调用方 `transition()` 之后再 `save()`，
        # 此刻读到的已经是新状态了 —— 于是"pending → compensated"这种
        # 从来没发生过的迁移会被当成没变，一条事件都不发。
        #
        # 真 PG 那边由 `WHERE version = ?` 取旧行（版本只增不回退，
        # 所以取到的一定是被覆盖的那一行）；这里是它的等价物（E-25 同款记账）。
        self._status: dict[str, CompensationStatus] = {}
        self.events = events

    def add(self, record: CompensationRecord) -> None:
        if record.execution_id in self._by_execution:
            raise InvariantViolation(
                f"S-2: execution {record.execution_id} already has a compensation record"
            )
        self._by_id[record.compensation_id] = record
        self._by_execution[record.execution_id] = record.compensation_id
        self._status[record.compensation_id] = record.status
        emit(self.events, record, event_type=status_event_type(record.status))

    def get(self, compensation_id: str) -> CompensationRecord | None:
        return self._by_id.get(compensation_id)

    def get_by_execution(self, execution_id: str) -> CompensationRecord | None:
        cid = self._by_execution.get(execution_id)
        return self._by_id.get(cid) if cid else None

    def open_for(self, run_id: str) -> Sequence[CompensationRecord]:
        open_records = [
            r for r in self._by_id.values()
            if r.run_id == run_id and r.is_open
        ]
        return sorted(open_records, key=lambda r: (r.created_at, r.compensation_id), reverse=True)

    def unresolved_for(self, run_id: str) -> Sequence[CompensationRecord]:
        return [
            r for r in self._by_id.values()
            if r.run_id == run_id and r.status is CompensationStatus.UNRESOLVED
        ]

    def claim(self, compensation_id: str) -> CompensationRecord | None:
        record = self._by_id.get(compensation_id)
        if record is None or record.status is not CompensationStatus.PENDING:
            return None
        record.transition(CompensationStatus.RUNNING)
        self._status[record.compensation_id] = CompensationStatus.RUNNING
        emit(
            self.events,
            record,
            event_type=transition_event_type(
                CompensationStatus.PENDING, CompensationStatus.RUNNING
            ),
        )
        return record

    def save(self, record: CompensationRecord) -> None:
        before = self._status.get(record.compensation_id)
        if before is None:
            # 与 PG 的 rowcount = 0 对齐：`save()` 不是插入通道，
            # 对一条不存在的行说"我改好了"是谎话（替身不许比真的松）。
            raise ConcurrentStateError(
                f"E-13: cannot save compensation {record.compensation_id} — "
                f"no such row (save is not an insert; use add)"
            )
        self._by_id[record.compensation_id] = record
        self._status[record.compensation_id] = record.status
        emit(
            self.events,
            record,
            event_type=transition_event_type(before, record.status),
        )

    def upgrade_to_compensable(
        self, execution_id: str, *, args: Mapping[str, Any], reason: str
    ) -> CompensationRecord | None:
        cid = self._by_execution.get(execution_id)
        if cid is None:
            return None
        record = self._by_id.get(cid)
        if record is None or record.status is not CompensationStatus.UNRESOLVED:
            return None
        if dict(record.args):
            # 同 PG 的 WHERE：它不是因为缺参数才撤销不了的（S-5/S-6），
            # 自动重试的理由不成立。替身不许比真库宽。
            return None
        record.become_compensable(args)
        self._status[record.compensation_id] = CompensationStatus.PENDING
        emit(
            self.events, record,
            event_type=COMPENSATION_UPGRADED, reason=reason,
        )
        return record

    def amend_reason(
        self, execution_id: str, *, reason: str
    ) -> CompensationRecord | None:
        cid = self._by_execution.get(execution_id)
        if cid is None:
            return None
        record = self._by_id.get(cid)
        if record is None or record.status is not CompensationStatus.UNRESOLVED:
            # D-23：已经处置过的是历史。真 PG 那边由 UPDATE 的 WHERE 挡住，
            # 这里必须自己挡 —— 否则内存版会比 PG 版宽，于是"过了单测"的
            # 代码在真库上行为不同（替身比真的松，是最坏的一种替身）。
            return None
        record.amend_reason(reason)
        emit(self.events, record, event_type=COMPENSATION_AMENDED)
        return record


# ---------------------------------------------------------------- 结果


@dataclass(frozen=True)
class CompensationOutcome:
    """一次补偿跑完的结果 —— 必须能回答"还有没有没撤销干净的"。"""

    attempted: int = 0
    compensated: int = 0
    unresolved: int = 0
    skipped: int = 0                    # 没抢到认领（别的 Coordinator 在处理）
    execution_ids: tuple[str, ...] = ()
    unresolved_ids: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.unresolved == 0


# ---------------------------------------------------------------- 协调器


class SagaCoordinator:
    """决定"要不要撤销、按什么顺序、撤销不掉怎么收尾"。

    它**不执行**任何东西 —— 撤销动作交给 `executor` 回调（由 AgentLoop 提供），
    因为只有 Loop 握着 Kernel / TaskFactory / Worker。
    """

    def __init__(
        self,
        *,
        store: CompensationStore | None = None,
        now: Any | None = None,
    ) -> None:
        self.store = store or InMemoryCompensationStore()
        self._now = now

    # ------------------------------------------------------------ 登记
    def record(
        self,
        *,
        run_id: str,
        step_id: str,
        task_id: str,
        execution_id: str,
        action: Action,
        result: Mapping[str, Any] | None,
        execution_status: ExecutionStatus,
        failure_class: str | None = None,
    ) -> CompensationRecord | None:
        """正向执行结束后登记。返回 `None` = 不需要撤销。

        三种结局：

            COMPLETED           → 副作用确实发生了 → PENDING（待撤销）
            FAILED/EXTERNAL_UNKNOWN
                                → 副作用**存疑** → UNRESOLVED（不能自动撤销，也不能当没发生）
            其它失败            → 认为没产生副作用 → 不登记

        第二种是最容易被漏掉的：工具超时 / 网络断开之后，
        "到底改没改成"这个系统**不知道**。此时任何自动行为都是猜 ——
        自动撤销可能撤销一个并不存在的东西，当作没事则留下一笔没人管的副作用。
        所以唯一诚实的处理是**记下来，交给人工**。
        """
        spec = action.compensation
        if spec is None:
            return None
        if self.store.get_by_execution(execution_id) is not None:
            return None                       # S-2：已经有了一条

        unknown = (
            execution_status is not ExecutionStatus.COMPLETED
            and failure_class == "external_unknown"
        )

        if unknown:
            return self.record_unresolved(
                run_id=run_id,
                step_id=step_id,
                task_id=task_id,
                execution_id=execution_id,
                action=action,
                reason=(
                    "S-11: forward execution ended with EXTERNAL_UNKNOWN; "
                    "whether the side effect happened is unknown, so it cannot be "
                    "auto-compensated and must not be treated as if nothing happened"
                ),
            )

        if execution_status is not ExecutionStatus.COMPLETED:
            return None

        try:
            args = spec.materialize(result)
        except InvariantViolation as exc:
            # S-8：撤销需要的参数拿不到 —— 记成 UNRESOLVED，绝不带着缺失参数去撤销
            return self.record_unresolved(
                run_id=run_id,
                step_id=step_id,
                task_id=task_id,
                execution_id=execution_id,
                action=action,
                reason=str(exc),
            )

        record = CompensationRecord(
            run_id=run_id,
            step_id=step_id,
            task_id=task_id,
            execution_id=execution_id,
            action_type=action.action_type.value,
            tool=spec.tool,
            args=args,
            description=spec.description,
            status=CompensationStatus.PENDING,
        )
        self.store.add(record)
        return record

    # ------------------------------------------------------------ 存疑 / 无人负责
    def record_unresolved(
        self,
        *,
        run_id: str,
        step_id: str,
        task_id: str,
        execution_id: str,
        action: Action,
        reason: str,
    ) -> CompensationRecord | None:
        """登记一条 `UNRESOLVED`：这笔副作用**存疑**或**无人负责**。

        返回 `None` 的两种情形与 `record()` 一致：Action 没声明逆操作
        （那就没有可记的账），或这条 Execution 已经有一条了（S-2）。

        ------------------------------------------------------------------
        为什么单独一个方法，而不是在 `record()` 里再写一个分支

        "UNRESOLVED 怎么登记"必须**只有一个定义**（B-7）。
        `record()` 内部原本就有两处各自 `CompensationRecord(...)` 一次
        （EXTERNAL_UNKNOWN、撤销参数取不到），而 D-12 / D-13 又要再加两处
        （委派以 failed/cancelled 收尾、父 Run 已终态）。
        抄成四份之后，"缺参数时 `args` 是什么、`reason` 必填吗、
        S-2 的判重做没做"就会有四个答案 —— 而它们必须只有一个。

        ------------------------------------------------------------------
        为什么 `args` 恒为空

        撤销参数是**从正向执行的结果里取的**（`spec.materialize(result)`）。
        走到这里的四种情形都有一个共同点：**没有可信的结果可取**。
        带着猜出来的参数去撤销，比记一条"不知道"更糟 ——
        那是在用一次真实副作用去抵消一个可能并不存在的副作用。
        """
        spec = action.compensation
        if spec is None:
            return None
        if self.store.get_by_execution(execution_id) is not None:
            return None                       # S-2
        record = CompensationRecord(
            run_id=run_id,
            step_id=step_id,
            task_id=task_id,
            execution_id=execution_id,
            action_type=action.action_type.value,
            tool=spec.tool,
            args={},
            description=spec.description,
            status=CompensationStatus.UNRESOLVED,
            reason=reason,
        )
        self.store.add(record)
        return record

    # ------------------------------------------------------------ "撤销不了" → "待撤销"
    def upgrade_to_compensable(
        self, *, execution_id: str, args: Mapping[str, Any], reason: str
    ) -> CompensationRecord | None:
        """D-25：可信结果证明了副作用确实发生了 ⟹ 这笔账变得**撤销得掉**。

        与 `amend_unresolved` 的分工：那是"**改一句话**"（账还是撤销不了，
        但我们不再说不知道），这是"**改一个结论**"（它现在撤销得掉了）。

        ------------------------------------------------------------------
        为什么它**不**真的去撤销

        它只把账本从"撤销不了"搬回"待撤销"（PENDING）。
        真正去撤销是 `compensate()` 的事，而那一趟要跑一条真实的 Execution
        （S-1）—— 该由谁跑、什么时候跑，是**调度**的问题，不是账本的问题。
        让这里顺手撤销掉，等于把"什么时候动外部世界"这个决定
        藏在一次结果对账里 —— 那是策略，不该由对账的人替业务做（S-9）。

        ------------------------------------------------------------------
        返回 `None` 不是失败

        没有这一行 / 已处置过 / 本来就不缺参数 —— 三种都不动账本。
        第三种尤其要分清：那种行是"撤销动作跑失败了"（S-5/S-6），
        要不要再试一次是**人**的决定（S-14 唯一例外）。
        """
        return self.store.upgrade_to_compensable(
            execution_id, args=dict(args), reason=reason
        )

    # ------------------------------------------------------------ 收回"不知道"
    def amend_unresolved(
        self, *, execution_id: str, reason: str
    ) -> CompensationRecord | None:
        """D-23：真相到达时，把账本上那句"不知道"**换**成真相。

        与 `record_unresolved` 的分工：那是**开**一笔账（副作用还没人负责），
        这是**补**一笔账（账还开着，但我们当初不知道的那件事现在知道了）。

        ------------------------------------------------------------------
        刻意不叫 `resolve`

        它**并不解决**那笔副作用 —— 副作用还在外部世界，这条记录仍然
        UNRESOLVED（S-15）。叫 `resolve` 会让人以为"这笔账结了"，
        于是运维不再看它 —— 而它恰恰还在等人去看（PR-19）。

        ------------------------------------------------------------------
        返回 `None` 不是失败

        没有这一行、或这一行已经处置过了，都返回 `None`。
        后者是对的：人工已经把它拉回 PENDING 并撤销掉了，
        这时"补一句真相"改的就是历史。
        """
        return self.store.amend_reason(execution_id, reason=reason)

    # ------------------------------------------------------------ 撤销
    def open_items(self, run_id: str) -> Sequence[CompensationRecord]:
        return self.store.open_for(run_id)

    def build_action(self, record: CompensationRecord, *, risk_level: RiskLevel) -> Action:
        """把一条待撤销记录变成 Action。

        注意 `compensation=None`：撤销动作本身**不再声明自己的逆操作**（S-12）。
        "撤销的撤销"在语义上是不成立的 —— 它等于把刚撤销掉的副作用再制造一遍。
        """
        return Action(
            run_id=record.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": record.tool, "args": dict(record.args)},
            risk_level=risk_level,
            rationale=f"compensation for {record.execution_id}: {record.description}",
        )

    def compensate(
        self,
        run_id: str,
        *,
        executor: Any,
        risk_level: RiskLevel = RiskLevel.HIGH,
    ) -> CompensationOutcome:
        """按 LIFO 逆序撤销（S-3）。

        `executor(action) -> bool`：由 AgentLoop 提供，真正把撤销动作交给 Kernel 跑。
        返回 True = 撤销成功。

        **S-6：单条失败不阻断其余。** 一条撤销动作坏掉就停下的话，
        排在它后面（更早发生）的副作用就全被永久化了 ——
        那等于用一个局部故障换一批永久性副作用。
        """
        attempted = compensated = unresolved = skipped = 0
        done_ids: list[str] = []
        unresolved_ids: list[str] = []

        for record in self.store.open_for(run_id):
            claimed = self.store.claim(record.compensation_id)
            if claimed is None:
                skipped += 1          # 别的 Coordinator 抢走了
                continue
            attempted += 1
            try:
                ok = executor(self.build_action(claimed, risk_level=risk_level))
            except Exception as exc:  # noqa: BLE001 —— 撤销动作抛异常也不能中断补偿循环
                ok = False
                reason = f"S-6: compensation raised {type(exc).__name__}: {exc}"
            else:
                reason = (
                    "S-5: compensation execution did not complete "
                    f"(tool={claimed.tool})"
                )
            if ok:
                claimed.transition(CompensationStatus.COMPENSATED, now=self._now)
                compensated += 1
                done_ids.append(claimed.execution_id)
            else:
                claimed.transition(
                    CompensationStatus.UNRESOLVED, reason=reason, now=self._now
                )
                unresolved += 1
                unresolved_ids.append(claimed.execution_id)
            self.store.save(claimed)

        return CompensationOutcome(
            attempted=attempted,
            compensated=compensated,
            unresolved=unresolved,
            skipped=skipped,
            execution_ids=tuple(done_ids),
            unresolved_ids=tuple(unresolved_ids),
        )

    def release(self, run_id: str) -> int:
        """S-16：Run **成功**了 —— 把待撤销记录结案为 NOT_NEEDED。

        返回值是结案的条数。

        没有这一步的话，每一次成功的运行都会在账本里留下一串 PENDING，
        而 PENDING 的字面意思就是"待撤销"。运维看板上会永远挂着一串假待办，
        真正要撤销的那几条会淹没在里面 —— 这和"静默失败"是同一种伤害：
        信息还在，但指向是错的。
        """
        closed = 0
        for record in self.store.open_for(run_id):
            if record.status is not CompensationStatus.PENDING:
                continue                      # 已经在跑的让别人跑完
            record.transition(CompensationStatus.NOT_NEEDED, now=self._now)
            self.store.save(record)
            closed += 1
        return closed

    def reopen(self, compensation_id: str) -> CompensationRecord:
        """人工介入：把一条 UNRESOLVED 拉回 PENDING 重试（S-14 唯一允许的倒流）。"""
        record = self.store.get(compensation_id)
        if record is None:
            raise KeyError(compensation_id)
        record.transition(CompensationStatus.PENDING, now=self._now)
        self.store.save(record)
        return record


__all__ = [
    "CompensationOutcome",
    "CompensationStore",
    "InMemoryCompensationStore",
    "SagaCoordinator",
]
