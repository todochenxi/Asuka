"""Step：Plan Node 的运行时实例（基线 §3.1 / §5.1）。

它不是 Agent 启动时就存在的静态图节点 —— Agent 是动态决策图，
Step 由 Runtime **在执行过程中动态产生**。Workflow 的静态 Node 在执行时同样实例化为 Step。

**B-5：Step.status 由所属 Task 派生，不由 Step 自己维护。**

```text
Step.status ← derive_step_status([Execution.status for task in step.tasks])
```

所以 Step 里没有 `mark_running()` / `complete()`。
它只是逻辑分组，**不拥有调度语义**（Lease / Retry / Worker / Cancellation 全在 Kernel）。

**B-6：Step : Task = 1 : N**（基线 §4）。
一个 Step 可以挂多个 Task —— 这是 Parallel / Fan-out / Multi-Agent 的支撑点。
如果每个 Task 都新开一个 Step，1:N 就被悄悄降成了 1:1，扇出能力随之消失。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..errors import InvariantViolation
from ..execution.execution import ExecutionStatus
from ..ids import new_step_id


class StepStatus(str, Enum):
    """比 Execution 少一个 STALE —— Step 是业务视角，不关心 Lease 过期这种基础设施细节。"""

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STEP_STATUSES = frozenset(
    {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED}
)

_GUARDED = frozenset({"status"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Step:
    step_id: str = field(default_factory=new_step_id)
    run_id: str = ""
    plan_node_id: str = ""
    name: str = ""
    task_ids: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    attributes: Mapping[str, Any] = field(default_factory=dict)

    _sealed: bool = field(default=False, repr=False, compare=False)
    _deriving: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("B-5: Step.run_id is required")
        if not self.plan_node_id:
            raise InvariantViolation("B-5: Step.plan_node_id is required (Step is a Plan Node instance)")
        object.__setattr__(self, "attributes", dict(self.attributes))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            getattr(self, "_sealed", False)
            and name in _GUARDED
            and not getattr(self, "_deriving", False)
        ):
            raise InvariantViolation(
                "B-5: Step.status is derived from its Tasks; use sync() instead"
            )
        object.__setattr__(self, name, value)

    @contextmanager
    def deriving(self) -> Iterator[None]:
        prev = getattr(self, "_deriving", False)
        object.__setattr__(self, "_deriving", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_deriving", prev)

    # ------------------------------------------------------------ 组成
    def add_task(self, task_id: str) -> None:
        """B-6：一个 Step 挂多个 Task —— 扇出的支撑点。"""
        if task_id not in self.task_ids:
            self.task_ids = (*self.task_ids, task_id)

    @property
    def task_count(self) -> int:
        return len(self.task_ids)

    # ------------------------------------------------------------ 派生
    def sync(
        self,
        statuses: Sequence[ExecutionStatus],
        *,
        now: datetime | None = None,
    ) -> StepStatus:
        """从所属 Task 的 Execution 状态投影自己的状态。"""
        from .derive import derive_step_status

        new_status = derive_step_status(statuses)
        if new_status is not self.status:
            with self.deriving():
                self.status = new_status
                self.updated_at = now or _utcnow()
        return new_status

    def summary(self) -> Mapping[str, Any]:
        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "plan_node_id": self.plan_node_id,
            "name": self.name,
            "status": self.status.value,
            "task_count": self.task_count,
        }
