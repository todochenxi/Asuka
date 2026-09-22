"""Tool Spec：一个"可调用能力"的**声明**（基线 §24 / §25）。

```text
Tool    = Capability      一个可调用的能力
Skill   = Procedure       一组步骤
Agent   = Decision Maker  做决定的那个
```

Tool Spec 只描述"这个工具是什么"，不包含"怎么连它" ——
协议（Native / HTTP / MCP / CLI / Sandbox）由 `protocols.py` 的 Invoker 适配，
这就是 §24 说的"**Tool Runtime 不关心底层连接协议**"。

### `side_effect` 为什么是必填项

因为它决定 `idempotency_key` 要不要透传给外部系统（§16）：

```text
READ     无副作用，随便重试，不需要去重键
WRITE    有副作用，**必须**透传 idempotency_key = execution_id
UNKNOWN  说不清 —— 失败时只能记 EXTERNAL_UNKNOWN，不许盲重试
```

不声明的话，"这个工具失败了能不能重试"就变成一句口头约定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Mapping


class ToolProtocol(str, Enum):
    """底层连接协议。Tool Runtime 不关心，Invoker 关心。"""

    NATIVE = "native"
    HTTP = "http"
    MCP = "mcp"
    CLI = "cli"
    SANDBOX = "sandbox"
    AGENT = "agent"


class SideEffect(str, Enum):
    """这个工具会不会改变外部世界的状态。"""

    READ = "read"
    WRITE = "write"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。**不含实现** —— 实现注册在 `ToolRegistry` 里。"""

    name: str
    version: str = "1.0.0"
    description: str = ""
    protocol: ToolProtocol = ToolProtocol.NATIVE
    side_effect: SideEffect = SideEffect.READ
    #: 极简 JSON-Schema 子集：`{"required": [...], "properties": {...}}`
    input_schema: Mapping[str, Any] | None = None
    #: 工具自己的超时上限（与 Task.timeout 取较小值）
    timeout: timedelta | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolSpec.name is required")
        if not self.version:
            raise ValueError("ToolSpec.version is required")

    @property
    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def needs_idempotency_key(self) -> bool:
        """WRITE / UNKNOWN 的调用必须带上去重键（§16）。"""
        return self.side_effect is not SideEffect.READ


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    `version` 记的是**实际执行的版本**，不是请求时说的版本 ——
    和 Model Gateway 的 `model_id` 同一个道理：
    请求 "latest" 时这两个值不同，记成请求值会让审计无从追溯。
    """

    tool_name: str
    version: str
    protocol: ToolProtocol
    side_effect: SideEffect
    output: Mapping[str, Any] = field(default_factory=dict)
    #: 透传给外部系统的去重键（READ 类为 ""）
    idempotency_key: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.tool_name}@{self.version}"
