"""Runtime 层的端口（阶段 8）。

Runtime **驱动**循环，但它不替 Intelligence 做决定，也不替 Kernel 管生命周期：

    Intelligence  决定（Planner / DecisionEngine）
    Runtime       驱动（AgentLoop：拿决定 → 造 Task → 交棒 → 收 Observation）
    Kernel        执行（Execution / Attempt / Lease）

这里只放 Runtime 需要而别处没有的接口。**不重复定义** Kernel 或 Domain 已有的东西。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.plan import Plan
from packages.agent_domain.intelligence.state import State


class Planner(Protocol):
    """I-2：产出 Plan（计划的**节点**是静态的；运行时的 Step 是它的实例）。"""

    def plan(self, state: State) -> Plan: ...


class DecisionEngine(Protocol):
    """产出 Decision。

    ⚠️ Decision **不可执行**（I-4）：它只有 `selected_action` 和 `confidence_signal`，
    没有 `execute()` —— 副作用必须经过 ActionResolver → TaskFactory → Kernel。
    """

    def decide(self, state: State) -> Decision: ...


class LLMClient(Protocol):
    """最小 LLM 接口（真实实现接 OpenAI / Anthropic / 内部网关）。"""

    def complete(self, prompt: str, **kwargs: Any) -> Mapping[str, Any]: ...


class Tool(Protocol):
    """一个可被调用的工具。"""

    name: str

    def call(self, args: Mapping[str, Any]) -> Mapping[str, Any]: ...
