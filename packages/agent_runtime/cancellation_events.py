"""取消意图 → 事件（M47 / 空洞 225 修正版）。

--------------------------------------------------------------------------
为什么单独一个文件

两个 `RunCancellationStore` 实现（`InMemoryRunCancellationStore` 与
`PostgresRunCancellationStore`）落在两个模块里，而它们必须发出**同一种**事件：
同样的 `aggregate_type`、同样的 payload 字段、同样的"什么算一次变化"。

抄两份之后，"settle 算不算一次变化"、"abandon 的 payload 里带不带 reason"
就会有两个答案 —— 而下游（Read Model / 审计）只有一个消费者（B-7）。

--------------------------------------------------------------------------
X-15 同款：事件流必须能**独立读懂**

一条 `cancellation.abandoned`（"我们不等了"）如果独自出现在流里，
下游根本无从判断放弃的是哪条 Run、当初谁要停它、为什么。
所以这一轮的发射点是取消意图的**全部**写路径，而不只是"放弃"那一处：

    有人要停它       → requested
    那条 Run 确实停了 → settled
    我们不等了       → abandoned

只发最后一种，等于给下游寄一张没有前文的更正通知。

--------------------------------------------------------------------------
为什么按**迁移**命名事件，不按**落地后的状态**

和 compensation 同一条理由：`settled` 落地之后那一行的 `settled_at` 有值，
和"正在等"的那一行**完全不同**。所以"看状态命名"分不出"刚开始等"
和"等完了" —— 而下游对它们的反应正好相反：
一个是"取消通道收到一条新请求"，
一个是"那条 Run 已经停了，通道可以清了"。

按迁移命名就不会有这个问题：事件回答的是"刚才发生了什么"，
不是"这一行现在写着什么"。
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol

from packages.agent_domain.events.event import (
    CANCELLATION_ABANDONED,
    CANCELLATION_REQUESTED,
    CANCELLATION_SETTLED,
    new_event,
)

CANCELLATION_AGGREGATE = "cancellation"


class CancellationEventSink(Protocol):
    """事件的落点。

    PG 实现传 `PostgresOutboxStore(conn)` —— **同一个 conn**：
    X-3 要求"状态写入"与"事件写入"在同一事务里，两个连接就不是了。
    内存实现传 `InMemoryOutbox()`。

    `None` 表示"这一次不发" —— 那是**显式**的选择（测试里不想看到事件、
    或者这一段压根还没接上 outbox），而不是"忘了传"。
    """

    def append(self, events: Iterable[Any]) -> None: ...


def cancellation_event(
    record: Any, *, event_type: str
) -> Any:
    """一条取消意图变化 → 一个 Event。

    `run_id` 在 payload 里（也是 `aggregate_id`）：下游按它建索引。
    `reason` / `by` 也带：审计要能不看 PG 就知道"这条请求当初为什么被提交"。

    `settled_at` / `abandoned_at` 同样带：事件流必须能独立读懂"它停了没"
    和"我们等不等了"——这是 R-14 的前提（两件独立的事可以同时成立）。
    """
    return new_event(
        aggregate_type=CANCELLATION_AGGREGATE,
        aggregate_id=record.run_id,
        event_type=event_type,
        payload={
            "run_id": record.run_id,
            "reason": record.reason,
            "by": record.by,
            "requested_at": record.requested_at.isoformat()
            if record.requested_at
            else None,
            "settled_at": record.settled_at.isoformat()
            if record.settled_at
            else None,
            "abandoned_at": record.abandoned_at.isoformat()
            if record.abandoned_at
            else None,
            "abandon_after": record.abandon_after.isoformat()
            if record.abandon_after
            else None,
        },
    )


def emit(
    sink: Any, record: Any, *, event_type: str | None
) -> Any | None:
    """把一次变化写进 outbox。`sink is None` 或 `event_type is None` 都不写。

    返回发出的那个 Event（没发返回 `None`）—— 测试要能断言"发了什么"，
    而不只是"发了几条"。
    """
    if sink is None or event_type is None:
        return None
    event = cancellation_event(record, event_type=event_type)
    sink.append([event])
    return event


#: request 写路径 → 那个事件。
REQUEST_EVENT = CANCELLATION_REQUESTED

#: settle 写路径 → 那个事件。
SETTLE_EVENT = CANCELLATION_SETTLED

#: abandon 写路径 → 那个事件。
ABANDON_EVENT = CANCELLATION_ABANDONED


__all__ = [
    "ABANDON_EVENT",
    "CANCELLATION_AGGREGATE",
    "CANCELLATION_ABANDONED",
    "CANCELLATION_REQUESTED",
    "CANCELLATION_SETTLED",
    "CancellationEventSink",
    "REQUEST_EVENT",
    "SETTLE_EVENT",
    "cancellation_event",
    "emit",
]
