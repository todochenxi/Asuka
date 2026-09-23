"""Agent Harness —— 控制 Agent 如何运行、受什么约束。

基线 §23：

    AgentHarness
    ├── ContextManager     （M17）
    ├── MemoryManager      （M17）
    ├── PolicyEngine       ← M16
    ├── GuardrailEngine    ← M16
    ├── CostManager        ← M16
    └── HumanLoop          ← M16

**最重要的约束（基线 §2 的 P0-3）**：

    ┌─ Runtime ──┐                    ┌─ Harness ──┐
    │  AgentLoop │──── 调用 ─────────►│  Harness   │
    └────────────┘                    └────────────┘

> **Harness 永远是被调用方。**
> 它不反向调用 Kernel，也不在 Kernel 内部埋钩子。
> 这是"Harness 不侵入 Execution Kernel"的唯一实现方式。

因此本包 **不 import execution_kernel**（H-4）：
Harness 挂起一个 Action 时，它只产出一个 `ApprovalRequest`；
真正去 Kernel 写 `SUSPENDED` 的是 AgentLoop —— 谁有 Kernel 引用谁动手。
"""
from .approval import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    HumanLoop,
    InMemoryApprovalStore,
)
from .cost import Budget, CostManager
from .guardrail import (
    Guardrail,
    GuardrailEngine,
    GuardrailFinding,
    GuardrailSeverity,
    GuardrailStage,
    GuardrailVerdict,
    SecretPatternGuardrail,
    SensitiveTopicGuardrail,
    ToolAllowlistGuardrail,
)
from .harness import Harness, HarnessVerdict
from .policy import PolicyContext, PolicyDecision, PolicyEngine, PolicyRule, Verdict
from .policy_document import PolicyDocument, PolicyDocumentError
from .ports import Clock, ManualClock, SystemClock

__all__ = [
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalStore",
    "Budget",
    "Clock",
    "CostManager",
    "Guardrail",
    "GuardrailEngine",
    "GuardrailFinding",
    "GuardrailSeverity",
    "GuardrailStage",
    "GuardrailVerdict",
    "Harness",
    "HarnessVerdict",
    "HumanLoop",
    "InMemoryApprovalStore",
    "ManualClock",
    "PolicyContext",
    "PolicyDecision",
    "PolicyDocument",
    "PolicyDocumentError",
    "PolicyEngine",
    "PolicyRule",
    "SecretPatternGuardrail",
    "SensitiveTopicGuardrail",
    "SystemClock",
    "ToolAllowlistGuardrail",
    "Verdict",
]
