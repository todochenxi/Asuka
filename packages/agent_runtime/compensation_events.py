"""补偿账本 → 事件（M40 / 空洞 232）。

--------------------------------------------------------------------------
为什么单独一个文件

两个 `CompensationStore` 实现（`InMemoryCompensationStore` 与
`PostgresCompensationStore`）落在两个模块里，而它们必须发出**同一种**事件：
同样的 `aggregate_type`、同样的 payload 字段、同样的"什么算一次变化"。

抄两份之后，"认领算不算一次变化"、"`amended` 的 payload 里带不带 reason"
就会有两个答案 —— 而下游（Read Model / 审计）只有一个消费者（B-7）。

--------------------------------------------------------------------------
X-15：事件流必须能**独立读懂**

一条 `compensation.amended`（"那句不知道被收回了"）如果独自出现在流里，
下游根本无从判断被收回的是哪句话。所以这一轮的发射点是账本的
**全部**写路径，而不只是"改口"那一处：

    记下一笔账        → recorded / unresolved
    抢到认领          → claimed
    状态真的变了      → claimed / unresolved / compensated / not_needed / reopened
    改口（D-23）      → amended

只发最后一种，等于给下游寄一张没有前文的更正通知。

--------------------------------------------------------------------------
为什么按**迁移**命名事件，不按**落地后的状态**

`reopened`（UNRESOLVED → PENDING）落地之后那一行也是 PENDING，
和 `recorded` 一模一样。所以"看状态命名"必然把两者合成一个 ——
而下游对它们的反应正好相反：一个是"新出现一笔待撤销"，
一个是"早就登记过的那一笔，人工决定再试一次"。

按迁移命名就不会有这个问题：事件回答的是"刚才发生了什么"，
不是"这一行现在写着什么"。后者在 payload 的 `status` 里（谁都能看）。
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.events.event import (
    COMPENSATION_AMENDED,
    COMPENSATION_CLAIMED,
    COMPENSATION_COMPENSATED,
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_RECORDED,
    COMPENSATION_REOPENED,
    COMPENSATION_UNRESOLVED,
    COMPENSATION_UPGRADED,
    new_event,
)

COMPENSATION_AGGREGATE = "compensation"


class CompensationEventSink(Protocol):
    """事件的落点。

    PG 实现传 `PostgresOutboxStore(conn)` —— **同一个 conn**：
    X-3 要求"状态写入"与"事件写入"在同一事务里，两个连接就不是了。
    内存实现传 `InMemoryOutbox()`。

    `None` 表示"这一次不发" —— 那是**显式**的选择（测试里不想看到事件、
    或者这一段压根还没接上 outbox），而不是"忘了传"。
    """

    def append(self, events: Iterable[Any]) -> None: ...


#: 新开一笔账 → 那个事件。只有这两个状态会"凭空出现"。
ADD_EVENTS: dict[CompensationStatus, str] = {
    CompensationStatus.PENDING: COMPENSATION_RECORDED,
    CompensationStatus.UNRESOLVED: COMPENSATION_UNRESOLVED,
}

#: 一次**迁移** → 那个事件。
#:
#: 这里覆盖的是 `_ALLOWED_TRANSITIONS` 的全部六种合法迁移，一个不落。
#: 漏掉任何一种，那一次账本变化就会"改了 PG、没发事件" ——
#: 而这正是本轮要闭合的空洞本身（PR-34：宁可拒绝，不许编造 ——
#: 所以查不到就抛，而不是静默不发）。
TRANSITION_EVENTS: dict[tuple[CompensationStatus, CompensationStatus], str] = {
    (CompensationStatus.PENDING, CompensationStatus.RUNNING): COMPENSATION_CLAIMED,
    (CompensationStatus.PENDING, CompensationStatus.UNRESOLVED): COMPENSATION_UNRESOLVED,
    (CompensationStatus.PENDING, CompensationStatus.NOT_NEEDED): COMPENSATION_NOT_NEEDED,
    (CompensationStatus.RUNNING, CompensationStatus.COMPENSATED): COMPENSATION_COMPENSATED,
    (CompensationStatus.RUNNING, CompensationStatus.UNRESOLVED): COMPENSATION_UNRESOLVED,
    (CompensationStatus.UNRESOLVED, CompensationStatus.PENDING): COMPENSATION_REOPENED,
}


def status_event_type(status: CompensationStatus) -> str:
    """新开一笔账时该发哪个事件。

    只有 PENDING / UNRESOLVED 会凭空出现；其余状态都是迁移的结果。
    """
    try:
        return ADD_EVENTS[status]
    except KeyError:
        raise InvariantViolation(
            f"X-15: a compensation record cannot be created directly in status "
            f"{status.value!r} — it can only be reached by a transition, and "
            f"every transition has its own event"
        ) from None


def transition_event_type(
    before: CompensationStatus, after: CompensationStatus
) -> str | None:
    """这次写回该发哪个事件。

    `before is after` → `None`（**没有**变化：`touch()` 只动 attempts /
    updated_at，账本说的事实没变，就不该有一条事件说它变了）。

    除此之外查不到 → 抛。`touch()` 之外不存在"改了却没名字"的合法迁移，
    出现了就说明状态机与事件表已经脱节 —— 静默跳过会让下游永远缺一条。
    """
    if before is after:
        return None
    try:
        return TRANSITION_EVENTS[(before, after)]
    except KeyError:
        raise InvariantViolation(
            f"X-15: compensation transition {before.value} → {after.value} has no "
            f"event type — either the transition is illegal (S-14) or the event "
            f"table is missing a row; refusing to write silently either way"
        ) from None


def compensation_event(
    record: Any, *, event_type: str, reason: str | None = None
) -> Any:
    """一条账本变化 → 一个 Event。

    `run_id` / `execution_id` / `task_id` 都在 payload 里：下游按
    `compensation_id` 建索引，但要按 Run / Execution **聚合** ——
    一个 Run 到底留下了几笔没人管的副作用，是看板第一个要问的数。

    `reason` 也带：审计要能不看 PG 就知道"这一笔为什么是这样"。

    `reason` 可以**覆盖**行上那个（D-25）：升级之后行上的 `reason`
    按约定留空（PENDING 不带理由，否则会让人以为还有事），
    而来龙去脉必须留下 —— 它只能落在事件里。
    """
    return new_event(
        aggregate_type=COMPENSATION_AGGREGATE,
        aggregate_id=record.compensation_id,
        event_type=event_type,
        payload={
            "compensation_id": record.compensation_id,
            "run_id": record.run_id,
            "execution_id": record.execution_id,
            "task_id": record.task_id,
            "step_id": record.step_id,
            "tool": record.tool,
            "status": record.status.value,
            "reason": record.reason if reason is None else reason,
            "attempts": record.attempts,
        },
        aggregate_version=record.version,
    )


def emit(
    sink: Any, record: Any, *, event_type: str | None, reason: str | None = None
) -> Any | None:
    """把一次变化写进 outbox。`sink is None` 或 `event_type is None` 都不写。

    返回发出的那个 Event（没发返回 `None`）—— 测试要能断言"发了什么"，
    而不只是"发了几条"。
    """
    if sink is None or event_type is None:
        return None
    event = compensation_event(record, event_type=event_type, reason=reason)
    sink.append([event])
    return event


__all__ = [
    "ADD_EVENTS",
    "COMPENSATION_AGGREGATE",
    "COMPENSATION_AMENDED",
    "COMPENSATION_CLAIMED",
    "COMPENSATION_UPGRADED",
    "CompensationEventSink",
    "TRANSITION_EVENTS",
    "compensation_event",
    "emit",
    "status_event_type",
    "transition_event_type",
]
