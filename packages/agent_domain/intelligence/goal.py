"""Goal：系统真正可执行的任务定义。

I-1  Goal 必须由 UserRequest 经 Goal Interpreter 生成，不可跳过
I-2  Goal 必须含至少一条 success_criteria

Goal ≠ 原始 User Request。原始请求是自然语言，Goal 是可判定成功与否的结构化定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol

from ..errors import InvariantViolation
from ..ids import new_id


@dataclass(frozen=True)
class Budget:
    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    deadline: datetime | None = None


@dataclass(frozen=True)
class Goal:
    goal_id: str = field(default_factory=lambda: new_id("goal"))
    run_id: str = ""
    objective: str = ""
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    priority: int = 0
    budget: Budget = field(default_factory=Budget)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("I-1: Goal.run_id is required")
        if not self.objective.strip():
            raise InvariantViolation("I-1: Goal.objective is required")
        if not self.success_criteria:
            raise InvariantViolation(
                "I-2: Goal must contain at least one success_criteria, "
                "otherwise the run cannot be judged as completed"
            )
        for c in self.success_criteria:
            if not str(c).strip():
                raise InvariantViolation("I-2: success_criteria must not be empty")


class GoalInterpreter(Protocol):
    """Goal 的唯一生产入口（I-1）。

    实现方可以是 LLM、规则引擎或人工表单，但**领域层不允许直接 `Goal(...)` 构造业务 Goal**，
    必须经 `interpret_goal()`，否则"Goal 与 UserRequest 的关系"就会失控。
    """

    def interpret(self, user_request: str, context: Mapping[str, object]) -> Goal: ...


def interpret_goal(
    user_request: str,
    interpreter: GoalInterpreter,
    *,
    run_id: str,
    context: Mapping[str, object] | None = None,
) -> Goal:
    """I-1：唯一入口。禁止绕过 Goal Interpreter 直接造 Goal。"""
    if not user_request or not user_request.strip():
        raise InvariantViolation("I-1: user_request is required")
    if not run_id:
        raise InvariantViolation("I-1: run_id is required")
    goal = interpreter.interpret(user_request, {**(context or {}), "run_id": run_id})
    if goal.run_id != run_id:
        raise InvariantViolation(
            f"I-1: interpreter returned goal for run {goal.run_id}, expected {run_id}"
        )
    return goal
