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
    ActionType.FINISH: None,          # ② 终态 —— 由 Loop 的 FINISH 分支处理
    # ③ **声明了，但没有一层执行它。** 这句注释此前写的是"等待由 Wake-up
    #    Controller 管"—— 那是一句**描述不存在连接的话**：那个 Controller 吃的是
    #    `Suspension`，而 wait 动作不产生 Task、不产生 Execution，**不可能**有
    #    Suspension（probe90.py 实测）。而它要等的那件事也没有 producer：
    #    生产代码里没有任何一处设置过 `SuspensionReason.TIMER` / `EXTERNAL_EVENT`。
    #    ⇒ 它落在 `UNEXECUTABLE_ACTION_TYPES` 里，由 `AgentLoop._step()` 零副作用
    #    判死并点名（I-20）—— 而不是让它崩在一个没有账本记录的异常上。
    ActionType.WAIT: None,
    ActionType.REPLAN: None,          # ② 触发重新规划 —— 由 Loop 的 REPLAN 分支处理
}

# ---------------------------------------------------------------------------
# I-20：把 `ACTION_TO_TASK` 那张表**读成三个集合**，而不是靠注释解释
# ---------------------------------------------------------------------------
#
# `None` 这一个值同时承担了三种**完全不同**的意思：
#
#     ① 有 Task 映射        -> 交给 Kernel 执行
#     ② 没有 Task，但由 Loop 自己处理（FINISH / REPLAN）-> 照样是能执行的
#     ③ 没有 Task，也没有任何一层处理它 -> **声明了但执行不了**
#
# 前两种是"设计如此"，第三种是"洞"。用同一个 `None` 表达它们，读代码的人
# 分不出来 —— 而运行时也分不出来：`_step()` 里没有 WAIT 的分支，
# 于是它一路掉到 `from_action()` 抛异常（probe90.py 实测：账本一条都没有）。
#
# ⇒ 三个集合都**从那张表推导**，不手写。手写的白名单会在 `ActionType`
#   新增成员时静默漏掉它 —— 而那正是 M88 那种洞的成因（声明了却没人管）。

#: ① 会产生真实 Task 的动作类型。
TASK_PRODUCING_ACTION_TYPES: frozenset[ActionType] = frozenset(
    k for k, v in ACTION_TO_TASK.items() if v is not None
)

#: ② 不产生 Task，但由 `AgentLoop._step()` 自己处理的动作类型。
LOOP_HANDLED_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.FINISH, ActionType.REPLAN}
)

#: ③ **声明了、但没有任何一层执行它**的动作类型 —— 由前两者推导，不手写。
UNEXECUTABLE_ACTION_TYPES: frozenset[ActionType] = (
    frozenset(ActionType) - TASK_PRODUCING_ACTION_TYPES - LOOP_HANDLED_ACTION_TYPES
)

#: I-20：这个运行时**真的能执行**的动作类型（= ① ∪ ②）。
EXECUTABLE_ACTION_TYPES: frozenset[ActionType] = (
    TASK_PRODUCING_ACTION_TYPES | LOOP_HANDLED_ACTION_TYPES
)

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
            # 刻意把两种"没有 Task"分开说（PR-19：错误码要说中真发生了什么）：
            # 一种是**调用方的 bug**（FINISH/REPLAN 该由 Loop 处理，不该走到这里），
            # 另一种是**能力的缺口**（声明了但没有任何一层执行它）。
            # 混成一句 "produces no Task"，读的人分不出该改调用方还是该补实现。
            if action.action_type in LOOP_HANDLED_ACTION_TYPES:
                raise InvariantViolation(
                    f"I-4: action type {action.action_type.value!r} produces no Task — "
                    f"it is handled by AgentLoop._step() itself and must not reach "
                    f"TaskFactory; calling from_action() with it is a caller bug"
                )
            raise InvariantViolation(
                f"I-20: action type {action.action_type.value!r} produces no Task and "
                f"nothing executes it — it is declared in ActionType but this runtime "
                f"has no path for it (see UNEXECUTABLE_ACTION_TYPES); AgentLoop refuses "
                f"it before any side effect instead of crashing here"
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
