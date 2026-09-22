"""Action → Task 的唯一通道。

I-4  Action 自己不产生 Task，也不产生副作用
X-1  Runtime 造出 Task 交给 Kernel 之后就交棒，不再碰生命周期
X-2  Kernel 不认识 Goal / Decision，只认识 Task

所以 TaskFactory 是**边界本身**：它把 Runtime 的语义（Action / risk / step）
翻译成 Kernel 认得的字段（task_type / executor_type / payload / timeout）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution.task import ExecutorType, Task, TaskType
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.ids import new_step_id

#: ActionType → (TaskType, ExecutorType)。`None` = 这个 Action 不产生 Task。
ACTION_TO_TASK: dict[ActionType, tuple[TaskType, ExecutorType] | None] = {
    # LLM 走 HTTP：模型服务都是 HTTP 接口，且要跟本地工具用不同的 Executor
    ActionType.LLM_CALL: (TaskType.LLM_CALL, ExecutorType.HTTP),
    ActionType.TOOL_CALL: (TaskType.TOOL_CALL, ExecutorType.NATIVE),
    ActionType.SKILL_CALL: (TaskType.SKILL, ExecutorType.NATIVE),
    ActionType.AGENT_DELEGATION: (TaskType.AGENT_DELEGATION, ExecutorType.AGENT_RUNTIME),
    ActionType.HUMAN_APPROVAL: (TaskType.HUMAN_APPROVAL, ExecutorType.NATIVE),
    ActionType.ASK_USER: (TaskType.HUMAN_APPROVAL, ExecutorType.NATIVE),
    ActionType.FINISH: None,          # 终态，不需要执行
    ActionType.WAIT: None,            # 等待由 Wake-up Controller 管，不是一个 Task
    ActionType.REPLAN: None,          # 触发重新规划，由 Loop 自己处理
}

#: 高风险 → 调度时降优先级，且必须先过 Harness 审批（见 AgentLoop）
RISK_PRIORITY = {"low": 0, "medium": -1, "high": -2}


class TaskFactory:
    """把 Action 变成 Task。这是 Runtime → Kernel 的唯一入口。"""

    def __init__(self, *, default_timeout: timedelta = timedelta(seconds=60)) -> None:
        self.default_timeout = default_timeout

    def from_action(
        self,
        action: Action,
        *,
        step_id: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> Task:
        mapping = ACTION_TO_TASK.get(action.action_type)
        if mapping is None:
            raise InvariantViolation(
                f"I-4: action type {action.action_type.value} produces no Task"
            )
        task_type, executor_type = mapping

        payload = dict(action.to_task_kwargs()["payload"])
        payload.update(extra_payload or {})
        # risk_level 跟下去，Worker / Harness 才能按风险决定是否放行
        payload["risk_level"] = action.risk_level.value

        return Task(
            run_id=action.run_id,
            step_id=step_id or new_step_id(),          # E-11：必须能溯源到 Step
            task_type=task_type,
            executor_type=executor_type,
            payload=payload,
            priority=RISK_PRIORITY.get(action.risk_level.value, 0),
            timeout=action.timeout or self.default_timeout,
        )
