"""协议适配层：Tool Runtime 与真实工具之间的可替换点（§24）。

```text
Tool Runtime ──► ToolInvoker ──► Native / HTTP / MCP / CLI / Sandbox / Agent
```

**Tool Runtime 不关心底层连接协议** —— 它只认 `ToolInvoker.invoke(call)`。
所以这里不 import requests / mcp / docker，客户端一律 duck-typed。

### `idempotency_key` 怎么透传由 Invoker 决定

| 协议 | 通常做法 |
|---|---|
| HTTP | `Idempotency-Key` header |
| MCP | 请求 metadata |
| CLI / Sandbox | 环境变量或参数 |
| Native | 一般无外部副作用，通常不需要 |

Runtime 只负责**把 key 送到 Invoker 手里**，并保证 WRITE 类一定带（§16）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol

from .spec import ToolSpec


@dataclass(frozen=True)
class ToolCall:
    """交给 Invoker 的一次调用。不含任何 Kernel 概念。"""

    spec: ToolSpec
    args: Mapping[str, Any] = field(default_factory=dict)
    #: §16：= execution_id，跨 Attempt 稳定。WRITE 类工具必须带上。
    idempotency_key: str = ""
    timeout: timedelta | None = None

    @property
    def name(self) -> str:
        return self.spec.name


class ToolInvoker(Protocol):
    """一个具体协议的执行器。"""

    def invoke(self, call: ToolCall) -> Mapping[str, Any]: ...


@dataclass
class FunctionInvoker:
    """把进程内函数包成 Invoker（NATIVE 协议，测试与内置工具用）。

    `pass_idempotency_key=True` 时把去重键作为关键字参数传下去 ——
    进程内工具一般无外部副作用，默认不传，需要时显式打开。
    """

    fn: Callable[..., Mapping[str, Any]]
    pass_idempotency_key: bool = False

    def invoke(self, call: ToolCall) -> Mapping[str, Any]:
        if self.pass_idempotency_key:
            return dict(self.fn(call.args, idempotency_key=call.idempotency_key))
        return dict(self.fn(call.args))
