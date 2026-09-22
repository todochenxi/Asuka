"""AgentOS · agent_runtime（M15 阶段 8）

Runtime 层的职责只有一句话：**驱动循环**。

    Intelligence 决定    Planner / DecisionEngine（不在本包内，由调用方注入）
    Runtime     驱动     AgentLoop：Plan → Decision → Action → Task → Kernel → Observation → State
    Kernel      执行     ExecutionKernel / Worker（已实现，本包只依赖其接口）

    ports.py         Planner / DecisionEngine / LLMClient / Tool
    loop.py          AgentLoop：最小闭环；高风险必须挂起等 Harness（I-9 / X-11）
    reducer.py       RuntimeReducer：Observation → State 的唯一解释器（X-7）
    task_factory.py  Action → Task 的边界（I-4 / X-1）
    executors.py     Kernel 语义翻译层（Gateway / ToolRuntime → ExecutorError）
    model_gateway/   模型调用的唯一通道（阶段 9，G-1~G-8）
    tool_runtime/    工具调用的唯一通道（阶段 10，T-1~T-6）
    trace.py         Run Trace：append-only 审计账本（阶段 11，L-6）
    assembly.py      §44 第一条 End-to-End 闭环的**唯一装配点**（阶段 11，L-2）

本包**不**新增任何生命周期状态机，也不碰数据库 —— 那些全在 Kernel。
"""
from __future__ import annotations

from .assembly import RuntimeStack, assemble_runtime_stack  # noqa: F401
from .executors import LLMCallExecutor, ToolCallExecutor, ToolRegistry  # noqa: F401
from .loop import AgentLoop, AgentLoopConfig, StepOutcome  # noqa: F401
from .ports import DecisionEngine, LLMClient, Planner, Tool  # noqa: F401
from .reducer import RuntimeReducer  # noqa: F401
from .task_factory import TaskFactory  # noqa: F401
from .trace import RunTrace, TraceEntry  # noqa: F401

__version__ = "0.1.0"
