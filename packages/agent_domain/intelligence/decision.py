"""Decision：Agent 对下一步行动的判断。

Decision 不直接执行（I-4）—— 它没有 execute() 方法，也没有任何副作用通道。

I-9  confidence_signal 不是经过校准的概率，禁止用作自动执行阈值。
     "confidence > 0.9 → 自动执行" 是被明令禁止的写法。
     是否放行由 Policy / Guardrail 决定，不由 confidence_signal 决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from ..errors import InvariantViolation
from ..ids import new_id
from .action import Action


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Decision:
    decision_id: str = field(default_factory=lambda: new_id("dec"))
    run_id: str = ""
    selected_action: Action | None = None
    evidence: tuple[str, ...] = ()
    confidence_signal: float | None = None     # ← signal，不是 probability
    rationale: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("Decision.run_id is required")
        if self.selected_action is None:
            raise InvariantViolation("Decision.selected_action is required")
        if self.selected_action.run_id != self.run_id:
            raise InvariantViolation("Decision.selected_action belongs to another run")
        if self.confidence_signal is not None:
            if not 0.0 <= self.confidence_signal <= 1.0:
                raise InvariantViolation(
                    "I-9: confidence_signal must be within [0, 1] if provided"
                )
        object.__setattr__(self, "metadata", dict(self.metadata))

    # ------------------------------------------------------------------ I-9
    @property
    def has_confidence(self) -> bool:
        return self.confidence_signal is not None

    def describe_confidence(self) -> str:
        """只用于展示 / 排序 / 人工审核，**绝不用于自动放行**。"""
        if self.confidence_signal is None:
            return "unknown"
        v = self.confidence_signal
        if v >= 0.8:
            return "high-signal"
        if v >= 0.5:
            return "medium-signal"
        return "low-signal"


class ActionResolver:
    """I-4：Decision → Action 的唯一通道。

    Decision 自身不可执行；只有经过 Resolver 拿到 Action，
    再由 TaskFactory 变成 Task，才允许产生副作用。
    """

    def resolve(self, decision: Decision) -> Action:
        if decision.selected_action is None:
            raise InvariantViolation("I-4: decision has no selected_action")
        return decision.selected_action
