"""Action：一个具体的行动意图。

Action ≠ Task：
    Action 是"我要做什么"（Intelligence 层）
    Task  是"把这个行动变成可调度工作"（Kernel 层）

I-4  Decision 不可直接执行，必须经 ActionResolver → TaskFactory
I-8  Action 为 HUMAN_APPROVAL 时必须带 timeout
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Mapping

from ..business.compensation import CompensationSpec
from ..errors import InvariantViolation
from ..ids import new_id


class ActionType(str, Enum):
    LLM_CALL = "llm_call"                # 调模型：与"调工具"是不同的决策，不能混成一个
    TOOL_CALL = "tool_call"
    SKILL_CALL = "skill_call"
    AGENT_DELEGATION = "agent_delegation"
    HUMAN_APPROVAL = "human_approval"
    ASK_USER = "ask_user"
    WAIT = "wait"
    REPLAN = "replan"
    FINISH = "finish"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: S-13：允许声明补偿的动作类型。
#:
#: 判据是"**这个动作会不会在外部世界留下状态**"：
#: 工具调用 / 技能 / 委派会在别的系统里留下东西（订单、工单、子 Run）；
#: 而 LLM_CALL 只是花钱换一段文本，ASK_USER / WAIT / REPLAN / FINISH / HUMAN_APPROVAL
#: 同理 —— 它们没有"可撤销的外部状态"。
#:
#: 给它们声明补偿是句谎话，而谎话的代价是**稀释**：
#: "这个 Run 有副作用"被高估之后，运维会对撤销动作麻木，
#: 真正需要人工介入的那一条会跟着一起被忽略。
COMPENSABLE_ACTION_TYPES = frozenset(
    {
        ActionType.TOOL_CALL,
        ActionType.SKILL_CALL,
        ActionType.AGENT_DELEGATION,
    }
)


@dataclass(frozen=True)
class Action:
    action_id: str = field(default_factory=lambda: new_id("act"))
    run_id: str = ""
    action_type: ActionType = ActionType.TOOL_CALL
    payload: Mapping[str, Any] = field(default_factory=dict)
    timeout: timedelta | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    rationale: str = ""
    #: S-8：逆操作的声明。**必须随正向动作一起提出**，不允许事后发明。
    compensation: CompensationSpec | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("Action.run_id is required")
        if not isinstance(self.action_type, ActionType):
            raise InvariantViolation("Action.action_type must be an ActionType")
        object.__setattr__(self, "payload", dict(self.payload))

        # S-13：只有可能产生外部副作用的动作类型才允许声明补偿。
        #
        # 给 LLM_CALL 声明"撤销"是句谎话（模型调用没有可撤销的外部状态）。
        # 放任它的后果不是多跑一次空工具，而是让"这个 Run 有副作用"被高估 ——
        # 运维对一堆空撤销动作麻木之后，真正需要人工介入的那一条也会一起被忽略。
        if self.compensation is not None:
            if self.action_type not in COMPENSABLE_ACTION_TYPES:
                raise InvariantViolation(
                    f"S-13: action type {self.action_type.value} cannot declare a "
                    f"compensation; only {sorted(t.value for t in COMPENSABLE_ACTION_TYPES)} "
                    f"can produce an external side effect that is worth undoing"
                )

        # I-8
        if self.action_type is ActionType.HUMAN_APPROVAL and self.timeout is None:
            raise InvariantViolation(
                "I-8: HUMAN_APPROVAL action must carry a timeout, "
                "otherwise a human can block the run forever"
            )
        if self.timeout is not None and self.timeout <= timedelta(0):
            raise InvariantViolation("Action.timeout must be positive")

    @property
    def needs_approval(self) -> bool:
        return self.action_type is ActionType.HUMAN_APPROVAL

    def to_task_kwargs(self) -> dict[str, Any]:
        """交给 Task Factory 的素材（I-4：Action 自己不产生 Task）。"""
        return {
            "run_id": self.run_id,
            "payload": dict(self.payload),
            "action_type": self.action_type.value,
            "risk_level": self.risk_level.value,
        }
