"""API 的请求 / 响应对象（M18）。

**A-6：响应里不出现 Kernel 内部对象。**

`RunView` 里**没有** `Execution` / `Attempt` / `Lease` / `fencing_token`。
把它们直接吐出去有两个后果：

  · 调用方开始依赖 Kernel 的内部结构 —— 之后改一次 Kernel 就是一次 breaking change
  · `fencing_token` 这类字段出现在响应里，迟早有人拿它去"手动重试"

API 给的是**业务视角**（这个 Run 怎么样了、有没有在等人），
不是执行视角（哪条 Execution 在什么 Lease 上）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class StartRunRequest:
    agent_id: str
    user_request: str
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id is required")
        if not self.user_request:
            raise ValueError("user_request is required")


@dataclass(frozen=True)
class ApprovalView:
    """给人看的审批卡片 —— 人只需要知道"要不要同意这件事"。"""

    approval_id: str
    run_id: str
    status: str
    question: str
    requested_at: datetime | None = None
    expires_at: datetime | None = None
    #: 挂起在 Kernel 里的那条 Execution（A-4：审批必须能追溯到具体执行）
    execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "status": self.status,
            "question": self.question,
            "requested_at": _iso(self.requested_at),
            "expires_at": _iso(self.expires_at),
            "execution_id": self.execution_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ApprovalView":
        return cls(
            approval_id=str(payload.get("approval_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            status=str(payload.get("status") or ""),
            question=str(payload.get("question") or ""),
            requested_at=_parse_iso(payload.get("requested_at")),
            expires_at=_parse_iso(payload.get("expires_at")),
            execution_id=str(payload.get("execution_id") or ""),
        )


@dataclass(frozen=True)
class RunView:
    """业务视角的 Run。A-6：不含任何 Kernel 对象。"""

    run_id: str
    agent_id: str
    status: str
    step_count: int = 0
    pending_approval: ApprovalView | None = None
    #: A-3：True = 这次调用命中了幂等键，**没有**新建 Run。
    #: 它决定返回 200 还是 201 —— 调用方要能区分"我刚建的"和"早就有了"。
    replayed: bool = False
    #: F-1：刚刚那一步的结果。`""` = 这一步还没走过（刚 start 的 Run）。
    #: 没有它，调用方只能看见"状态变了"，说不出"刚才发生了什么"。
    last_outcome: str = ""
    #: R-6 / F-3：挂起时在等谁 —— 审批 id 或子 Run id。
    #: 少了它，一个挂起的 Run 在界面上是"卡住了"，而不是"在等 X"。
    waiting_for: str | None = None
    #: M34 / 空洞 222：这次调用**只是落下了一个取消意图**，
    #: 那条 Run 还没被认领（它跑在别的进程里，或正停在某个安全点之前）。
    #:
    #: 没有它，跨进程取消只能二选一：要么谎报"已取消"（它还活着），
    #: 要么报 404 RUN_NOT_FOUND（它明明活着，只是不在这一进程）。
    #: 两个都是 PR-19 那类错 —— 说的和发生的不是同一件事。
    cancel_requested: bool = False
    attributes: Mapping[str, Any] = None      # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if self.attributes is None:
            object.__setattr__(self, "attributes", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "step_count": self.step_count,
            "pending_approval": (
                self.pending_approval.to_dict() if self.pending_approval else None
            ),
            "replayed": self.replayed,
            "last_outcome": self.last_outcome,
            "waiting_for": self.waiting_for,
            "cancel_requested": self.cancel_requested,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunView":
        """`to_dict()` 的反面 —— 线上形状只有**一处**定义（B-7）。

        为什么必须存在：幂等键记录的是第一次给出的**答案**（D-36），
        而它是按 `to_dict()` 的形状落库的（JSON 才能进 PG）。
        回放时若另写一份"从字典拼回 RunView"的代码，那份代码会在
        `to_dict()` 加字段的那一天开始**静默丢字段** ——
        于是回放出来的答案与第一次给出的不一样，而没有任何报错。

        `attributes` 不进 `to_dict()`（它是给 Trace 用的内部字段），
        所以这里也取不到 —— 回放出来的答案与第一次给出的**线上**答案
        逐字段相同，这正是要保证的那件事。
        """
        approval = payload.get("pending_approval")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            status=str(payload.get("status") or ""),
            step_count=int(payload.get("step_count") or 0),
            pending_approval=ApprovalView.from_dict(approval) if approval else None,
            replayed=bool(payload.get("replayed", False)),
            last_outcome=str(payload.get("last_outcome") or ""),
            waiting_for=payload.get("waiting_for"),
            cancel_requested=bool(payload.get("cancel_requested", False)),
        )


@dataclass(frozen=True)
class TraceView:
    """F-4：一个 Run 的账本。

    为什么它必须是一个独立端点而不是塞进 `RunView`：
    账本是**只增**的，一次 Run 可能有几十上百条。把它塞进 `GET /runs/{id}`
    会让"看一眼状态"这个最频繁的动作背上整本账的代价，
    而真正需要账本的时候（排障、审计）又常常只要最后 N 条。
    """

    run_id: str
    entries: tuple[Mapping[str, Any], ...] = ()
    step_count: int = 0
    #: M109：把同一本账**按层摊开** —— 目标/计划/动作、任务/执行、Harness、
    #: 观测/状态各一段。账本本身是"只增的一串"，而排障时人问的是
    #: "这一层发生了什么"；两件事都留着，谁也别替谁。
    layers: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_count": self.step_count,
            "entries": [dict(e) for e in self.entries],
            "layers": dict(self.layers),
        }


@dataclass(frozen=True)
class DecisionRequest:
    """A-8：审批必须带 `by` —— 匿名审批进不了审计。"""

    approval_id: str
    decision: str                 # "approve" | "reject"
    by: str
    comment: str = ""

    def __post_init__(self) -> None:
        if not self.approval_id:
            raise ValueError("approval_id is required")
        if self.decision not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        if not self.by:
            raise ValueError("by is required: an anonymous approval cannot be audited")

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _parse_iso(value: Any) -> datetime | None:
    """`_iso()` 的反面。`""` / None ⟹ None（"没这个时刻"，不是"此刻"）。"""
    return datetime.fromisoformat(value) if value else None
