"""Business Domain（基线 §3.1）。

    AgentRun / WorkflowRun / SkillRun / EvaluationRun / Step

这些对象表达**"谁正在执行什么业务"**，拥有业务语义；
与 Kernel Domain（Task / Execution / Attempt）严格分离。

本包最核心的一条设计（解决基线 §9.2 与 §10 的表面冲突）：

> §9.2 说 "AgentRun / Task 的状态由下层状态派生"
> §10 又给了 AgentRun 一套生命周期（CREATED → QUEUED → RUNNING ↔ SUSPENDED → …）

两者不矛盾，合起来的意思是：

    AgentRun **有**状态集合与合法转换（§10）
    但状态值**不是被设置的，而是从下层 Step → Task → Execution 投影上来的**（§9.2）

所以这里没有"谁来把 Run 改成 RUNNING"这个问题 ——
**没有任何人改它**，它是被算出来的。见 `derive.py`。
"""
from .derive import (
    AgentRunStateMachine,
    derive_run_status,
    derive_step_status,
)
from .run import (
    TERMINAL_RUN_STATUSES,
    AgentRun,
    AgentRunStatus,
)
from .step import (
    TERMINAL_STEP_STATUSES,
    Step,
    StepStatus,
)

__all__ = [
    "AgentRun",
    "AgentRunStateMachine",
    "AgentRunStatus",
    "Step",
    "StepStatus",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_STEP_STATUSES",
    "derive_run_status",
    "derive_step_status",
]
