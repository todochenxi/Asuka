"""Checkpoint 必须分两层。

    KernelCheckpoint  Execution 级，Kernel 拥有：执行到哪 + 恢复需要什么
    RunCheckpoint     Run 级，Runtime / Harness 拥有：跑到了哪个 Step / 完成哪些 Task

E-24  Kernel Checkpoint 不得包含 current_step / completed_tasks（业务语义属 Run Checkpoint）

写入时机（不定义就会在实现时随机化）：

    Attempt 成功          → KernelCheckpoint
    Step 完成             → RunCheckpoint
    进入 SUSPENDED 前     → 两者都写，强制
    每次 LLM 调用后       → 不写 Checkpoint，只写 ContextSnapshot
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..errors import InvariantViolation
from ..ids import new_checkpoint_id
from ..intelligence.observation import ArtifactRef


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class KernelCheckpoint:
    """Kernel 视角的恢复点：只装 Kernel 认识的东西。"""

    checkpoint_id: str = field(default_factory=new_checkpoint_id)
    execution_id: str = ""
    attempt_no: int = 1
    seq: int = 1                                # 同一 Attempt 内递增
    execution_state: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    fencing_token: int = 1
    artifact_refs: tuple[ArtifactRef, ...] = ()
    recovery_metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise InvariantViolation("KernelCheckpoint.execution_id is required")
        if not self.idempotency_key:
            raise InvariantViolation("KernelCheckpoint.idempotency_key is required")
        if self.attempt_no < 1 or self.seq < 1:
            raise InvariantViolation("KernelCheckpoint.attempt_no / seq must be >= 1")
        # E-24：业务语义不允许出现在 Kernel Checkpoint 里
        forbidden = {"current_step", "completed_tasks"} & set(self.execution_state)
        if forbidden:
            raise InvariantViolation(
                f"E-24: KernelCheckpoint must not contain {sorted(forbidden)}; "
                "they belong to RunCheckpoint"
            )
        object.__setattr__(self, "execution_state", dict(self.execution_state))
        object.__setattr__(self, "recovery_metadata", dict(self.recovery_metadata))


@dataclass(frozen=True)
class RunCheckpoint:
    """Run 视角的恢复点：业务语义在这里。"""

    checkpoint_id: str = field(default_factory=new_checkpoint_id)
    run_id: str = ""
    current_step: str = ""
    completed_tasks: tuple[str, ...] = ()
    variables: Mapping[str, Any] = field(default_factory=dict)
    context_snapshot_id: str | None = None      # 引用，不内嵌
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("RunCheckpoint.run_id is required")
        if not self.current_step:
            raise InvariantViolation("RunCheckpoint.current_step is required")
        object.__setattr__(self, "variables", dict(self.variables))
