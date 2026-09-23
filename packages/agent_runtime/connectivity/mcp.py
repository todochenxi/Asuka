"""MCP（Model Context Protocol）客户端与 Tool Invoker（基线 §24 / M4）。

### 它补的是什么

`ToolProtocol.MCP` 从 M15 起就冻结在枚举里，`ToolRegistry` 的注释也写着
"真实实现换成 MCP / HTTP 工具网关" —— 但**全仓没有一行真的说过 MCP**。
于是"工具来自一个 MCP 服务端"这件事，此前只能靠手写一个 Invoker 去假装。

这一层把它落成三段：

    McpClient          会说 `initialize` / `tools/list` / `tools/call`
    McpTool            服务端声明的一个工具（名字 / 描述 / 输入 schema / 只读提示）
    McpInvoker         把 ToolRuntime 的一次 `ToolCall` 翻成 `tools/call`
    register_mcp_tools 把服务端的工具**发现并登记**进 ToolRegistry

### 两条不变量

| # | 内容 |
|---|---|
| MC-1 | 一个 MCP 工具**没有名字**就拒绝登记 —— 没有名字的工具无法被寻址，登记它等于制造一个永远调不到的表项 |
| MC-2 | `side_effect` **不猜**：只有服务端显式声明 `annotations.readOnlyHint=true` 才是 `READ`，其余一律 `UNKNOWN`。把没声明的当成只读，正是 T-2（WRITE 必须带幂等键）想防的重复副作用 |

### 为什么 `isError` 要翻成异常

MCP 的 `tools/call` 用 `{"isError": true}` 表示"这次调用失败了"，
而不是 JSON-RPC 的 `error`（那是**协议/方法**层失败）。`ToolRuntime` 只认异常，
所以 `McpInvoker` 必须把 `isError` 翻成一个带 code 的 `ToolExecutionError` ——
否则一次失败的工具调用会被读成"成功，结果里有个 isError 字段"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..tool_runtime import (
    SideEffect,
    ToolExecutionError,
    ToolProtocol,
    ToolRegistry,
    ToolSpec,
)
from .jsonrpc import JsonRpcClient, JsonRpcError
from .transport import StdioTransport

#: MCP 协议版本（协商用）。服务端可以回一个它支持的版本。
MCP_PROTOCOL_VERSION = "2025-06-18"

#: JSON-RPC 的"方法/参数不对"这一档 —— 重试一百年也不会变对（同 T-3）。
_MCP_PERMANENT_CODES = frozenset({-32700, -32600, -32601, -32602})


@dataclass(frozen=True)
class McpTool:
    """服务端 `tools/list` 里的一项。"""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] | None = None
    read_only: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("MC-1: an MCP tool must have a name")


class McpClient:
    """一个 MCP 服务端的会话。"""

    def __init__(
        self,
        rpc: JsonRpcClient,
        *,
        client_name: str = "agentos",
        client_version: str = "1.0.0",
    ) -> None:
        self.rpc = rpc
        self.client_name = client_name
        self.client_version = client_version
        self.server_info: Mapping[str, Any] = {}

    @classmethod
    def from_stdio(
        cls, argv: Sequence[str], **kwargs: Any
    ) -> "McpClient":
        """用 stdio 传输起一个 MCP 服务端（MCP 最常见的形态）。"""
        return cls(JsonRpcClient(StdioTransport(argv)), **kwargs)

    # ------------------------------------------------------------ 会话
    def initialize(self) -> Mapping[str, Any]:
        """`initialize` 然后发 `notifications/initialized`。

        MCP 要求初始化之后发一条通知；漏掉它，服务端会一直等 ——
        而症状是"调用工具时它说不认识"，指向的是工具名，不是初始化。
        """
        result = self.rpc.call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        self.server_info = result if isinstance(result, Mapping) else {}
        self.rpc.notify("notifications/initialized")
        return self.server_info

    def list_tools(self) -> tuple[McpTool, ...]:
        result = self.rpc.call("tools/list", {})
        raw = result.get("tools", ()) if isinstance(result, Mapping) else ()
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"MC-1: tools/list returned a non-list 'tools': {raw!r}")
        tools = tuple(_tool(entry) for entry in raw)
        seen: set[str] = set()
        for tool in tools:
            if tool.name in seen:
                # 同名两项无法区分 —— 登记第二项会覆盖第一项，静默。
                raise ValueError(
                    f"MC-1: the MCP server listed tool {tool.name!r} twice; "
                    f"registering it would silently keep only one"
                )
            seen.add(tool.name)
        return tools

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str = "",
    ) -> Mapping[str, Any]:
        """调一个工具。`idempotency_key` 走 `_meta`（MCP 的扩展位，§16）。"""
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
        if idempotency_key:
            params["_meta"] = {"idempotencyKey": idempotency_key}
        result = self.rpc.call("tools/call", params)
        if not isinstance(result, Mapping):
            raise ValueError(f"MCP tools/call returned a non-object: {result!r}")
        return result


@dataclass
class McpInvoker:
    """把一次 `ToolCall` 翻成 MCP 的 `tools/call`（`ToolInvoker` 协议）。"""

    client: McpClient
    tool_name: str

    def invoke(self, call: Any) -> Mapping[str, Any]:
        try:
            result = self.client.call_tool(
                self.tool_name,
                dict(call.args),
                idempotency_key=call.idempotency_key,
            )
        except JsonRpcError as err:
            # 协议/方法层失败。参数错（-32602）等是 PERMANENT（同 T-3）。
            raise ToolExecutionError(
                "MCP_ERROR",
                f"mcp tools/call failed for {self.tool_name!r}: {err.code} {err.message}",
                retryable=err.code not in _MCP_PERMANENT_CODES,
            ) from err

        text = _join_text(result)
        if result.get("isError"):
            raise ToolExecutionError(
                "MCP_TOOL_ERROR",
                f"mcp tool {self.tool_name!r} reported isError: {text[:400]}",
                retryable=False,
            )
        return {
            "text": text,
            "content": list(result.get("content") or ()),
            "is_error": False,
        }


def register_mcp_tools(
    registry: ToolRegistry,
    client: McpClient,
    *,
    prefix: str = "",
    version: str = "1.0.0",
    make_default: bool = True,
) -> tuple[ToolSpec, ...]:
    """发现一个 MCP 服务端的工具并登记进 `ToolRegistry`。

    `prefix` 用来给不同服务端的同名工具消歧（`github.search` / `kb.search`）。
    `side_effect` 按 MC-2 推：只有 `readOnlyHint` 才是 `READ`。
    """
    registered: list[ToolSpec] = []
    for tool in client.list_tools():
        spec = ToolSpec(
            name=f"{prefix}{tool.name}",
            version=version,
            description=tool.description,
            protocol=ToolProtocol.MCP,
            # MC-2：没声明只读就是 UNKNOWN —— 于是 T-2 会要求幂等键。
            side_effect=SideEffect.READ if tool.read_only else SideEffect.UNKNOWN,
            input_schema=dict(tool.input_schema) if tool.input_schema else None,
        )
        registry.register(spec, McpInvoker(client, tool.name), make_default=make_default)
        registered.append(spec)
    return tuple(registered)


# ---------------------------------------------------------------- 内部


def _tool(entry: Any) -> McpTool:
    if not isinstance(entry, Mapping) or not entry.get("name"):
        raise ValueError(f"MC-1: malformed MCP tool entry (no name): {entry!r}")
    annotations = entry.get("annotations")
    read_only = bool(
        isinstance(annotations, Mapping) and annotations.get("readOnlyHint") is True
    )
    schema = entry.get("inputSchema")
    return McpTool(
        name=str(entry["name"]),
        description=str(entry.get("description") or ""),
        input_schema=dict(schema) if isinstance(schema, Mapping) else None,
        read_only=read_only,
    )


def _join_text(result: Mapping[str, Any]) -> str:
    parts = result.get("content")
    if not isinstance(parts, (list, tuple)):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, Mapping) and part.get("type") == "text":
            chunks.append(str(part.get("text") or ""))
    return "\n".join(chunks)


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpClient",
    "McpInvoker",
    "McpTool",
    "register_mcp_tools",
]
