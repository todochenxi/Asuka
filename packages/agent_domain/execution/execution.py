"""Execution：Task 在 Kernel 中的生命周期实体。

E-1   状态变更必须走 StateMachine.transition()，禁止直接赋值
E-2   终态（COMPLETED / FAILED / CANCELLED）不可逆
E-8   SUSPENDED 必须携带 suspension_reason
E-13  所有对象带 version，更新走 Optimistic Lock
E-19  Task : Execution = 1 : 1
E-21  idempotency_key = execution_id，跨 Attempt 稳定
E-23  STALE 只能由 RUNNING 进入，且必须经 Recovery
X-6   AgentRun ≠ Kernel Execution：Execution 是 Kernel 实体，不是业务 Run

注意：**Execution 里不存在 TaskExecution / ToolExecution**。
一个 Tool Call 产生的是 `Task(task_type=TOOL_CALL)`，它对应一个 Execution。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Mapping

from ..errors import ConcurrentStateError, InvariantViolation, TerminalStateError
from ..ids import idempotency_key_for, new_execution_id
from .lease import Lease

# 只能通过 StateMachine 修改的字段
_GUARDED_FIELDS = frozenset(
    {
        "status",
        "lease",
        "suspension",
        "current_attempt_no",
        "cancellation_requested",
        # M48 / 空洞 228：归因与意图是一件事的两半 ——
        # 说不出"谁叫停、为什么"的取消等于没有发生过（B-8 / A-8），
        # 所以它们和意图位一起受状态机保护，不许绕过 transition 直接改。
        "cancellation_reason",
        "cancellation_by",
    }
)


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"          # 已创建，未调度
    RUNNING = "RUNNING"          # 已 Claim，Lease 有效
    STALE = "STALE"              # Lease 过期 / Worker 失联（中间态，必进 Recovery）
    SUSPENDED = "SUSPENDED"      # 挂起等待（带 suspension_reason）
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)


class SuspensionReason(str, Enum):
    """等待原因不是顶层状态 —— 它是 SUSPENDED 的 reason 字段。

    M25 之前，`CHILD_AGENT` 被冻结在这里，但**全仓库没有任何一处设置过它** ——
    和 A-3（幂等键接到 Redis）是同一种病：概念冻结了，实现从没跟上，
    而因为没有测试去断言"谁设置了它"，一直没人发现。
    现在它由 `AgentLoop._suspend_for_child()` 设置（详见 §63）。
    """

    HUMAN_APPROVAL = "human_approval"
    #: 派生子 AgentRun（A2A / Agent Delegation）
    CHILD_AGENT = "child_agent"
    #: 派生子 SkillRun（Skill = Procedure，内部有多步，故也是一条子 Run）
    CHILD_SKILL = "child_skill"
    TIMER = "timer"
    EXTERNAL_EVENT = "external_event"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Suspension:
    reason: SuspensionReason
    wait_condition: Mapping[str, Any] = field(default_factory=dict)
    suspended_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, SuspensionReason):
            raise InvariantViolation("E-8: suspension reason must be a SuspensionReason")
        if not self.wait_condition:
            raise InvariantViolation("E-18: suspension must record a wait condition")
        object.__setattr__(self, "wait_condition", dict(self.wait_condition))


@dataclass
class Execution:
    execution_id: str = field(default_factory=new_execution_id)
    task_id: str = ""                       # E-19：1:1
    idempotency_key: str = ""               # E-21：= execution_id
    execution_mode: str = "task"            # task（无状态短任务）/ stateful（长任务 Actor）
    status: ExecutionStatus = ExecutionStatus.PENDING
    parent_id: str | None = None
    current_attempt_no: int = 0
    lease: "Lease | None" = None                # E-20：Lease 挂在 Execution
    suspension: Suspension | None = None
    cancellation_requested: bool = False    # 取消**意图**字段，不是状态
    #: M48 / 空洞 228：B-8 / A-8 —— 一条说不出"谁叫停、为什么"的取消
    #: 等于取消这件事没有发生过。Run 级（`run_cancellations`）与子 Run 级
    #: （012 迁移 / D-14）都有这两列，Execution 这一层原先没有。
    #:
    #: 刻意**不**给默认值以外的形态：`""` 就是"没有归因"，
    #: 而"没有归因"只允许出现在**从未被请求过取消**的 Execution 上。
    cancellation_reason: str = ""
    cancellation_by: str = ""
    version: int = 1
    _previous_version: int = field(default=0, repr=False, compare=False)
    # E-25：乐观锁真正要比的那个号 —— **上一次存储边界**上的版本。
    # 它只能由 Repository（add / get / save）改写，内存里的自增**不许**碰它。
    _store_version: int = field(default=0, repr=False, compare=False)
    _sealed: bool = field(default=False, repr=False, compare=False)
    _in_transition: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise InvariantViolation("E-19: Execution.task_id is required")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", idempotency_key_for(self.execution_id))
        if self.idempotency_key != self.execution_id:
            raise InvariantViolation(
                "E-21: idempotency_key must equal execution_id "
                "(so it stays stable across attempts)"
            )
        object.__setattr__(self, "_sealed", True)
        # E-25：新建/装载时，"存储里的版本"就是当前版本（还没人改过）。
        object.__setattr__(self, "_store_version", self.version)
        self.validate()

    # ------------------------------------------------------------ E-1 保护
    def __setattr__(self, name: str, value: Any) -> None:
        # 记住"写入前的版本"，供 Repository 做 Optimistic Lock（E-13）
        if name == "version":
            object.__setattr__(self, "_previous_version", getattr(self, "version", 0))
        # 构造期（_sealed=False）允许初始化；之后只允许 StateMachine 的 mutating 窗口写入
        if (
            name in _GUARDED_FIELDS
            and getattr(self, "_sealed", False)
            and not getattr(self, "_in_transition", False)
        ):
            raise InvariantViolation(
                "E-1: Execution state fields can only be changed via "
                f"StateMachine.transition(); direct assignment to '{name}' is forbidden"
            )
        object.__setattr__(self, name, value)

    @contextmanager
    def mutating(self) -> Iterator[None]:
        """StateMachine 专用的写入窗口。业务代码不应调用。"""
        prev = self._in_transition
        object.__setattr__(self, "_in_transition", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_in_transition", prev)

    # ------------------------------------------------------------ 不变量
    def validate(self) -> None:
        if self.status is ExecutionStatus.SUSPENDED:
            if self.suspension is None:
                raise InvariantViolation("E-8: SUSPENDED requires suspension_reason")
        elif self.suspension is not None:
            raise InvariantViolation("E-8: suspension must be None when not SUSPENDED")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_EXECUTION_STATUSES

    def assert_not_terminal(self) -> None:
        if self.is_terminal:
            raise TerminalStateError(
                f"E-2: Execution {self.execution_id} is already {self.status.value} (terminal)"
            )

    @property
    def previous_version(self) -> int:
        """**上一次内存自增前**的版本号 —— 只用于诊断，**不要**拿它当乐观锁。"""
        return self._previous_version

    @property
    def store_version(self) -> int:
        """E-25：上一次**存储边界**上的版本号 —— 乐观锁 `WHERE version = ?` 用它。

        为什么不能用 `previous_version`：

            `resume()` 内部是 SUSPENDED → PENDING → RUNNING **两次**跃迁，
            但 Kernel 只 `_persist()` 一次。此时 `previous_version` = 4（最后一次自增前的号），
            而库里还是 3 —— `WHERE version = 4` 命中 0 行，E-13 报错，
            一条**人明明已经批准**的 SUSPENDED Execution 就永远醒不过来。

        语义上：乐观锁比的是"我**读到的**那个版本"，不是"我上次内存自增前的那个版本"。
        一次 Kernel 操作允许包含多次状态跃迁（resume / recover 都是），
        所以"自增次数 == 落库次数"这个隐含假设从一开始就不成立。
        """
        return self._store_version

    def check_version(self, expected_version: int | None) -> None:
        """E-13：Optimistic Lock。"""
        if expected_version is not None and expected_version != self.version:
            raise ConcurrentStateError(
                f"E-13: execution version mismatch: expected={expected_version}, "
                f"actual={self.version}"
            )
