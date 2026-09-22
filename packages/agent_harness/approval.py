"""HITL：高风险 Action 需要人来放行（基线 §23）。

链路：

    Action
      ↓
    Policy → REQUIRE_APPROVAL
      ↓
    Checkpoint                      ← 挂起前强制写两层（基线 §14）
      ↓
    SUSPENDED(HUMAN_APPROVAL)       ← **Kernel** 写，不是 Harness 写
      ↓
    Human
      ↓
    Approved
      ↓
    Wake Up
      ↓
    Execution                        ← 不重新执行整个 Run

**所有权（基线 §10，与 Cancellation 同构）**：

| 角色 | 职责 |
|---|---|
| Harness / Runtime | **发起请求**：REQUIRE_APPROVAL |
| Kernel | **拥有生命周期**：写 SUSPENDED + suspension_reason + wait condition |
| Wake-up Controller | **检测条件** |
| Scheduler | **重新调度** |

> 所以 `HumanLoop` 只产出 `ApprovalRequest`，**它不碰 Kernel**。
> 真正去 `kernel.suspend()` 的是 AgentLoop —— 谁持有 Kernel 引用谁动手（H-4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Protocol, Sequence

from packages.agent_domain.errors import ConcurrentStateError, InvariantViolation
from packages.agent_domain.ids import new_id
from packages.agent_domain.intelligence.action import Action

from .ports import Clock, SystemClock


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_TERMINAL = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED,
     ApprovalStatus.CANCELLED}
)


@dataclass
class ApprovalRequest:
    approval_id: str = field(default_factory=lambda: new_id("apr"))
    run_id: str = ""
    action: Action | None = None
    reason: str = ""
    requested_at: datetime | None = None
    expires_at: datetime | None = None
    #: Kernel 里那个 SUSPENDED(HUMAN_APPROVAL) 的 Execution。
    #: Harness 不知道它，由 AgentLoop 挂起后回填（`attach_execution`）。
    execution_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("ApprovalRequest.run_id is required")
        if self.action is None:
            raise InvariantViolation("ApprovalRequest.action is required")
        if not self.reason:
            raise InvariantViolation("H-1: approval request must carry a reason")

        # H-5：审批必须有截止时间。
        # 这是 I-8（HUMAN_APPROVAL Action 必须带 timeout）在 Harness 侧的对应物：
        # 一条没有截止时间的审批，等价于允许一个人把整个 Run 永久挂住。
        if self.requested_at is None or self.expires_at is None:
            raise InvariantViolation(
                "H-5: approval request must carry requested_at and expires_at"
            )
        if self.expires_at <= self.requested_at:
            raise InvariantViolation("H-5: expires_at must be after requested_at")

    # ------------------------------------------------------------ 判定
    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def expired_at(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def attach_execution(self, execution_id: str) -> None:
        if self.execution_id is not None:
            raise InvariantViolation(
                "H-6: approval request already bound to an execution"
            )
        self.execution_id = execution_id


class ApprovalStore(Protocol):
    def save(self, request: ApprovalRequest) -> None: ...

    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def pending(self, run_id: str | None = None) -> Sequence[ApprovalRequest]: ...

    def transition(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> bool:
        """**原子**地把一条 PENDING 审批推进到终态；返回 False = 它已经不是 PENDING。

        **A-11：为什么必须有这个方法，而不能"读出来判一下再 save"。**

        两个审批请求同时到达（人双击了 / 两个审批人同时点了）时：

        ```text
        请求 A  读 → PENDING          写 → APPROVED(by=alice)
        请求 B    读 → PENDING        写 → APPROVED(by=bob)     ← 覆盖了 alice
        ```

        两边的前置检查都通过了，因为**它们读到的都是 PENDING**。
        结果是：审计记录里"谁批的"变成了后到的那个人，
        而 A-5 声称的"已决定的再回调 → 409"在并发下根本不成立 ——
        它防的是"先来后到"，防不住"同时到达"。

        所以判定与写入必须是**一个操作**，且由存储保证：
        PG 版本是 `UPDATE ... WHERE approval_id = ? AND status = 'pending'`，
        rowcount = 0 就是输了这场竞争。
        """
        ...


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._items: dict[str, ApprovalRequest] = {}

    def save(self, request: ApprovalRequest) -> None:
        self._items[request.approval_id] = request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._items.get(approval_id)

    def pending(self, run_id: str | None = None) -> list[ApprovalRequest]:
        return [
            r for r in self._items.values()
            if r.is_pending and (run_id is None or r.run_id == run_id)
        ]

    def transition(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> bool:
        """内存版同样只认 PENDING —— 语义必须与 PG 版一致，否则
        "并发下只有一个赢"这条保证只在一种实现里成立，等于没保证。"""
        req = self._items.get(approval_id)
        if req is None or req.status is not ApprovalStatus.PENDING:
            return False
        req.status = status
        req.decided_by = by
        req.decided_at = decided_at
        req.comment = comment
        return True


@dataclass
class HumanLoop:
    """审批的**发起与裁决**。

    注意它只能"请求"和"裁决"，不能"挂起" —— 挂起是 Kernel 的事。
    """

    store: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    clock: Clock = field(default_factory=SystemClock)
    default_ttl: timedelta = timedelta(minutes=30)

    # ------------------------------------------------------------ 发起
    def request(
        self,
        action: Action,
        *,
        reason: str,
        ttl: timedelta | None = None,
    ) -> ApprovalRequest:
        now = self.clock.now()
        req = ApprovalRequest(
            run_id=action.run_id,
            action=action,
            reason=reason,
            requested_at=now,
            expires_at=now + (ttl or self.default_ttl),
        )
        self.store.save(req)
        return req

    def bind(
        self,
        approval_id: str,
        execution_id: str,
    ) -> ApprovalRequest:
        """AgentLoop 把 Kernel 的 Execution 回填进来，形成审批 ↔ 挂起的双向可追溯。"""
        req = self._must_get(approval_id)
        req.attach_execution(execution_id)
        self.store.save(req)
        return req

    # ------------------------------------------------------------ 裁决
    def approve(
        self,
        approval_id: str,
        *,
        by: str,
        comment: str = "",
    ) -> ApprovalRequest:
        req = self._must_get(approval_id)
        self._guard_decidable(req)
        self._decide(req, ApprovalStatus.APPROVED, by=by, comment=comment)
        return req

    def reject(
        self,
        approval_id: str,
        *,
        by: str,
        comment: str = "",
    ) -> ApprovalRequest:
        req = self._must_get(approval_id)
        self._guard_decidable(req)
        self._decide(req, ApprovalStatus.REJECTED, by=by, comment=comment)
        return req

    def cancel(self, approval_id: str, *, by: str = "system") -> ApprovalRequest:
        req = self._must_get(approval_id)
        self._guard_decidable(req)
        self._decide(req, ApprovalStatus.CANCELLED, by=by, comment="")
        return req

    # ------------------------------------------------------------ 超时
    def expire_due(self, now: datetime | None = None) -> list[str]:
        """把超时的 PENDING 审批推进到 EXPIRED。

        这与 Cancellation 的 sweep 是同一个道理：**意图不会自己变成终态**。
        没人来扫的话，一条过期审批会永远停在 PENDING，Run 也就永远挂着。
        """
        now = now or self.clock.now()
        expired: list[str] = []
        for req in self.store.pending():
            if req.expired_at(now):
                # A-11：扫描也可能并发（多个 sweeper），同样走原子 transition
                if self.store.transition(
                    req.approval_id,
                    ApprovalStatus.EXPIRED,
                    by="timeout",
                    decided_at=now,
                ):
                    req.status = ApprovalStatus.EXPIRED
                    req.decided_at = now
                    req.decided_by = "timeout"
                    expired.append(req.approval_id)
        return expired

    # ------------------------------------------------------------ 查询
    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.store.get(approval_id)

    def pending(self, run_id: str | None = None) -> Sequence[ApprovalRequest]:
        return self.store.pending(run_id)

    def summary(self, approval_id: str) -> Mapping[str, object]:
        req = self._must_get(approval_id)
        return {
            "approval_id": req.approval_id,
            "run_id": req.run_id,
            "status": req.status.value,
            "reason": req.reason,
            "execution_id": req.execution_id,
            "decided_by": req.decided_by,
        }

    # ------------------------------------------------------------ 内部
    def _must_get(self, approval_id: str) -> ApprovalRequest:
        req = self.store.get(approval_id)
        if req is None:
            raise InvariantViolation(f"unknown approval_id: {approval_id}")
        return req

    def _decide(
        self,
        req: ApprovalRequest,
        status: ApprovalStatus,
        *,
        by: str,
        comment: str,
    ) -> None:
        """A-11：判定与写入是一次原子操作（理由见 `ApprovalStore.transition`）。

        `_guard_decidable` 只是为了让**错误消息可读**（"已决定" / "已过期"说得清楚），
        它不作数 —— 真正的裁决在存储里。两个检查都做，是因为它们防的不是同一种情况：
        前置检查防"先来后到"，原子 transition 防"同时到达"。
        """
        now = self.clock.now()
        if not self.store.transition(
            req.approval_id, status, by=by, decided_at=now, comment=comment
        ):
            raise ConcurrentStateError(
                f"H-6: approval {req.approval_id} was decided concurrently; "
                "this decision did NOT take effect"
            )
        # 内存实现里 transition 已经改过同一个对象；PG 实现里这是本地副本，需要同步
        req.status = status
        req.decided_by = by
        req.decided_at = now
        req.comment = comment

    def _guard_decidable(self, req: ApprovalRequest) -> None:
        # H-6：终态审批不可再改 —— 否则"先批准再驳回"会让审计自相矛盾
        if req.is_terminal:
            raise InvariantViolation(
                f"H-6: approval {req.approval_id} is already {req.status.value}"
            )
        # H-8：过期审批不能被批准 —— 必须先判定 EXPIRED，让 Run 走超时分支
        if req.expired_at(self.clock.now()):
            raise InvariantViolation(
                f"H-8: approval {req.approval_id} already expired; "
                "run expire_due() before deciding"
            )
