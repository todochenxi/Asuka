"""Event：系统发生过的事情。

严格区分：

    Event        给系统 / 审计 / 评估 / Read Model 看（写 Outbox → Kafka）
    Observation  给 Agent 看（进入 State，参与决策）
    Trace        给可观测性看（OpenTelemetry）

X-3：每次状态变更必须产生一个 Event（与状态写入同事务）。
X-5：PostgreSQL 是唯一 Truth；Kafka 只是 Durable Event Log。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..ids import new_event_id

# 状态变更类事件（由 StateMachine 产出，禁止手写字符串）
EXECUTION_CREATED = "execution.created"
EXECUTION_RUNNING = "execution.running"
EXECUTION_STALE = "execution.stale"
EXECUTION_SUSPENDED = "execution.suspended"
EXECUTION_RESUMED = "execution.resumed"
EXECUTION_COMPLETED = "execution.completed"
EXECUTION_FAILED = "execution.failed"
EXECUTION_CANCEL_REQUESTED = "execution.cancel_requested"
EXECUTION_CANCELLED = "execution.cancelled"

ATTEMPT_STARTED = "attempt.started"
ATTEMPT_SUCCEEDED = "attempt.succeeded"
ATTEMPT_FAILED = "attempt.failed"
ATTEMPT_TIMEOUT = "attempt.timeout"
ATTEMPT_CANCELLED = "attempt.cancelled"

LEASE_ACQUIRED = "lease.acquired"
LEASE_EXPIRED = "lease.expired"

#: 子 Run 终态（M30 / 空洞 209）。
#:
#: 这两个是 **Run 级**事件，不是 Execution 级：一条子 Run 内部有它自己的
#: Task / Execution / Attempt，它们各自都会发 `execution.*`，
#: 但**没有任何一条**会说"这一整条子 Run 结束了"。
#: 而父 Run 等的就是这一句 —— 它挂起时写进 `wait_condition` 的是
#: `child_run_id`（R-6），不是某个 execution_id。
#:
#: 缺失它的后果不是"慢一点"，是**委派永远回不来**：
#: `AgentLoop.child_completed()` 此前只有测试直接调用它，
#: 生产路径上没有任何人来叫醒父 Run，父 Execution 会一直 SUSPENDED。
CHILD_RUN_COMPLETED = "child_run.completed"
CHILD_RUN_FAILED = "child_run.failed"
#: 为什么取消要单独一个类型，而不是并进 `child_run.failed`：
#: 取消说的是"到此为止"（S-15），失败说的是"没做成"。
#: 对父 Run 来说两者确实都意味着"没拿到结果"，但对**审计**来说不是一回事 ——
#: "委派的子 Agent 自己失败了"和"有人把它取消了"是两种处置方式。
#: 并成一个类型之后，这个区别就只能在 payload 里找，而没有人会去找。
CHILD_RUN_CANCELLED = "child_run.cancelled"

#: 补偿账本（M40 / 空洞 232）。
#:
#: 在此之前，`compensations` 表上的**任何一次写入都不发事件**：
#: 一条副作用被记下来、被撤销、被判"存疑"、被改口 —— 下游
#: （Read Model / 审计 / 看板）一个字都看不到，因为 Kafka 那边从来没有
#: 对应的消息。按 X-3 的判据，那是"两件事实不同事务"的同一族错误：
#: PG 里的事实改了，而事件流这条事实**没有改**。
#:
#: 为什么是七个而不是一个 `compensation.changed`：
#: 这七种变化要的人不一样、要去做的事也不一样 ——
#:
#:     recorded      有人要开始撤销它
#:     claimed       某个 Coordinator 正在撤销（抢到了认领）
#:     unresolved    **没有人**能撤销它，必须有人来看（S-5）
#:     amended       当初那句"不知道"被真相顶掉了（D-23）
#:     compensated   已经撤销完了，这笔账结了
#:     not_needed    副作用按预期保留，不需要撤销（S-16）
#:     reopened      人工把它从"撤销不了"拉回"待撤销"（S-14 唯一例外）
#:
#: 合成一个类型之后，这个区别只能在 payload 里找，
#: 而没有人会去找（与 `CHILD_RUN_CANCELLED` 同一条理由）。
COMPENSATION_RECORDED = "compensation.recorded"
COMPENSATION_CLAIMED = "compensation.claimed"
COMPENSATION_UNRESOLVED = "compensation.unresolved"
COMPENSATION_AMENDED = "compensation.amended"
COMPENSATION_COMPENSATED = "compensation.compensated"
COMPENSATION_NOT_NEEDED = "compensation.not_needed"

#: `reopened` 为什么不能复用 `recorded`
#:
#: 两者落地之后，那一行**都是** PENDING。所以"看状态"的下游分不出来 ——
#: 而它们要做的反应正好相反：`recorded` 是"新出现一笔待撤销"，
#: `reopened` 是"早就有的那一笔，人工决定再试一次"。
#: 一个按补偿 id 去重的 Read Model 若把两者都叫 `recorded`，
#: 要么把重试当成新账（计数翻倍），要么因为 id 已存在而把重试整个丢掉。
COMPENSATION_REOPENED = "compensation.reopened"

#: M41 / 空洞 233：迟到的结果**证明副作用确实发生了** ⟹ 那笔账从
#: "撤销不了"升级成"待撤销"（D-25）。
#:
#: 它和 `reopened` 的迁移完全一样（UNRESOLVED → PENDING），
#: 所以 M40 那条"按迁移命名"在这里不够用了 —— 同一个迁移有两个起因：
#:
#:     reopened   人工决定再试一次（重试次数 +1）
#:     upgraded   真相到了，它**变得**可撤销了（待撤销笔数 +1）
#:
#: 下游对两者的反应不同，所以事件名要回答"**谁**让它变成这样的"，
#: 而不是只回答"它现在是什么状态"。
COMPENSATION_UPGRADED = "compensation.upgraded"

#: 取消意图（M47 / 空洞 225 修正版）。
#:
#: `run_cancellations` 表上的三条写路径在此前**一条事件都不发**，
#: 与 M40 治过的 `compensations` 是同一族病（X-3）：
#: PG 里的事实改了，而事件流这条事实没有改。
#:
#: 为什么是三个而不是一个 `cancellation.changed`（和 compensation 同一条理由）：
#:
#:     requested   有人要停它          → 取消通道收到一条新请求
#:     settled     那条 Run 确实停了   → 正常收尾，账清了
#:     abandoned   我们不知道它停没停   → 要人去看，账上记着"未知"
#:
#: 三种变化要的人不一样、要去做的事也不一样。
#: 合成一个类型之后，这个区别只能在 payload 里找，
#: 而没有人会去找。
CANCELLATION_REQUESTED = "cancellation.requested"
CANCELLATION_SETTLED = "cancellation.settled"
CANCELLATION_ABANDONED = "cancellation.abandoned"

#: 三个终态事件 → 那个终态的名字。唤醒路径按它决定"结果里有什么"。
CHILD_RUN_OUTCOMES: dict[str, str] = {
    CHILD_RUN_COMPLETED: "completed",
    CHILD_RUN_FAILED: "failed",
    CHILD_RUN_CANCELLED: "cancelled",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Event:
    """不可变事件（I-5）：事件一旦产生就不能修改，这是 Replay 的前提。"""

    event_id: str
    aggregate_type: str                 # execution / attempt / run / ...
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=_utcnow)
    aggregate_version: int = 1

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id is required")
        if not self.aggregate_type or not self.aggregate_id:
            raise ValueError("aggregate_type / aggregate_id are required")
        if not self.event_type:
            raise ValueError("event_type is required")
        object.__setattr__(self, "payload", dict(self.payload))


def new_event(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    aggregate_version: int = 1,
) -> Event:
    return Event(
        event_id=new_event_id(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=dict(payload or {}),
        aggregate_version=aggregate_version,
    )
