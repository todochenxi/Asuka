"""AgentRun：业务级执行实例（基线 §3.1 / §10）。

生命周期（§10）：

    CREATED → QUEUED → RUNNING ↔ SUSPENDED → COMPLETED
                          ↓          ↓
                        FAILED    CANCELLED

**B-2：status 是派生值，不可直接赋值。**
**B-7：三个终态只能来自 `runtime_terminal`，不能从 Step 派生。**

```text
AgentRun.status  ←  derive_run_status(step_statuses, runtime_terminal)
                              ↑                          ↑
                        从 Step 投影               Runtime 判定的终态
                    （只产出活跃态）            （COMPLETED/FAILED/CANCELLED）
```

没有 `run.mark_running()` 这种东西 —— 谁都不能"把 Run 改成 RUNNING"，
它只是当前所有 Step 状态的投影结果。这样就不存在"Run 说跑完了但还有 Task 在跑"这种
状态不一致，因为**根本没有第二个地方可以记这个状态**。

**AgentRun ≠ Execution**（§41 不变量）：
AgentRun 是业务语义的 Run，内部产生的 Task 由 Kernel 管理。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..errors import IllegalTransition, InvariantViolation
from ..execution.execution import SuspensionReason
from ..ids import new_run_id
from ..intelligence.goal import Goal
from ..intelligence.state import State

from .step import Step


class AgentRunStatus(str, Enum):
    """§10 的生命周期。注意这些值都是**投影结果**，不是被设置的。"""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
)

#: 派生写入保护：这些字段只能经 `sync()` 写
_GUARDED = frozenset({"status", "suspension_reason"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentRun:
    run_id: str = field(default_factory=new_run_id)
    agent_id: str = ""
    agent_version_id: str = ""
    goal: Goal | None = None
    state: State | None = None
    parent_run_id: str | None = None
    status: AgentRunStatus = AgentRunStatus.CREATED
    suspension_reason: SuspensionReason | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    version: int = 1
    attributes: Mapping[str, Any] = field(default_factory=dict)

    _sealed: bool = field(default=False, repr=False, compare=False)
    _deriving: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # B-1：没有 goal 的 Run 不成立 —— I-2（success_criteria 必填）在 Run 侧的对应物
        if self.goal is None:
            raise InvariantViolation("B-1: AgentRun.goal is required")
        if not self.agent_id:
            raise InvariantViolation("B-1: AgentRun.agent_id is required")
        object.__setattr__(self, "attributes", dict(self.attributes))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            getattr(self, "_sealed", False)
            and name in _GUARDED
            and not getattr(self, "_deriving", False)
        ):
            raise InvariantViolation(
                "B-2: AgentRun.status / suspension_reason are derived values; "
                "use sync() instead of assigning them"
            )
        object.__setattr__(self, name, value)

    @contextmanager
    def deriving(self) -> Iterator[None]:
        """只给 `sync()` 用的写窗口（与 `State.reducing()` 同一个套路）。"""
        prev = getattr(self, "_deriving", False)
        object.__setattr__(self, "_deriving", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_deriving", prev)

    # ------------------------------------------------------------ 派生
    def sync(
        self,
        steps: Sequence[Step],
        *,
        runtime_terminal: "AgentRunStatus | None" = None,
        now: datetime | None = None,
    ) -> "AgentRunStatus":
        """把 status 重新投影一次。

        `runtime_terminal` 是 Runtime 判定的终态（Goal 达成 / 主动放弃 / 取消）。
        非终态时，status 完全由下层 Step 决定 —— 没有任何人能"设置"它。
        """
        from .derive import derive_run_status, AgentRunStateMachine

        step_statuses = [s.status for s in steps]
        new_status = derive_run_status(step_statuses, runtime_terminal=runtime_terminal)

        # B-3：终态不可变
        #
        # M51：这里抛的是 `IllegalTransition`，不是 `InvariantViolation`。
        #
        # 两者在 API 层的映射完全不同（见 `agent_api/errors.py`）：
        #     IllegalTransition → 409  "资源存在、状态明确，只是这个转换不允许"
        #     InvariantViolation → 422 "请求本身不合法" —— 改请求就有用
        #
        # B-3 明明是前者：Run 存在、状态明确（completed），
        # 只是"已 completed 就不能变成 X"这个转换不被允许。
        # 报成 422 会误导调用方以为改改请求就行 —— 而它改一万次结果都一样。
        #
        # 症状：并发推进同一条 Run 时，先跑完的那个把它推到终态，
        #       另一个撞 B-3 → 422；而单线程下同样的情况走
        #       `_assert_advancable` → 409。同一个事实两个错误码，
        #       调用方只能靠消息里的 "B-3" 字样区分，脆弱。
        if self.status in TERMINAL_RUN_STATUSES and new_status != self.status:
            raise IllegalTransition(
                f"B-3: run {self.run_id} is already {self.status.value}; "
                f"cannot become {new_status.value}"
            )
        if new_status is not self.status:
            AgentRunStateMachine().can_transition(self.status, new_status)

        with self.deriving():
            self.status = new_status
            # B-4：SUSPENDED 必须说清楚在等什么（与 Execution 的 E-8 同构）
            if new_status is AgentRunStatus.SUSPENDED:
                if self.suspension_reason is None:
                    self.suspension_reason = SuspensionReason.HUMAN_APPROVAL
            else:
                self.suspension_reason = None
            self.updated_at = now or _utcnow()
        return new_status

    # ------------------------------------------------------------ 视图
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def summary(self) -> Mapping[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "suspension_reason": (
                self.suspension_reason.value if self.suspension_reason else None
            ),
            "parent_run_id": self.parent_run_id,
            "version": self.version,
        }
