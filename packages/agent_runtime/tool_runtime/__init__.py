"""Tool Runtime（基线 §24，M15 阶段 10）。

```text
Tool Call → Resolution → Version Resolution → Input Validation
          → Timeout → Executor → Execution Result
```

**不变量 T-1 ~ T-6**

| # | 内容 |
|---|---|
| T-1 | Tool Runtime **不做业务重试** —— 重试统一归 Kernel 的 Attempt，避免 3×3 双重重试 |
| T-2 | `side_effect` ≠ READ 的工具**必须**带 `idempotency_key`（= execution_id），漏传即硬拒绝 |
| T-3 | Input Validation 失败是 **PERMANENT**（参数不会自己变对） |
| T-4 | Policy / Guardrail / Rate Limit **不在这里** —— 那是 Harness 的唯一拦截点（§2） |
| T-5 | 版本显式解析；默认版本必须显式登记，不能随注册顺序漂移 |
| T-6 | Tool Runtime 不关心底层协议（Native / HTTP / MCP / CLI / Sandbox 由 Invoker 适配） |

**边界**：本包不 import `execution_kernel`，也不 import `agent_harness` ——
它既不认识 Kernel 的执行生命周期，也不替 Harness 做准入判断。
Kernel 语义（FailureClass）由 `agent_runtime.executors.ToolCallExecutor` 翻译。
"""
from .protocols import FunctionInvoker, ToolCall, ToolInvoker
from .registry import ToolNotFoundError, ToolRegistry
from .runtime import ToolExecutionError, ToolRuntime
from .sandbox import (
    SandboxProfile,
    SandboxViolation,
    SandboxedCommandInvoker,
    SandboxedInvoker,
)
from .spec import (
    SideEffect,
    ToolProtocol,
    ToolResult,
    ToolSpec,
)
from .validation import ToolValidationError, validate_input

__all__ = [
    "FunctionInvoker",
    "SandboxProfile",
    "SandboxViolation",
    "SandboxedCommandInvoker",
    "SandboxedInvoker",
    "SideEffect",
    "ToolCall",
    "ToolExecutionError",
    "ToolInvoker",
    "ToolNotFoundError",
    "ToolProtocol",
    "ToolRegistry",
    "ToolResult",
    "ToolRuntime",
    "ToolSpec",
    "ToolValidationError",
    "validate_input",
]
