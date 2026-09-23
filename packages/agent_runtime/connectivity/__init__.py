"""Connectivity（M4）—— 把 AgentOS 接到**别的系统**的两套协议。

```text
JSON-RPC 2.0（jsonrpc.py）          一次 RPC 的请求/响应/错误，与协议无关
        │
        ├── Transport（transport.py）   stdio（MCP）/ 内存（测试）/ [HTTP：待办]
        │
        └── MCP（mcp.py）               工具连接：initialize / tools/list / tools/call
```

**边界**：本包不 import `execution_kernel`，也不 import `agent_harness`。
它只认识 ToolRuntime 的 Invoker 协议（`ToolInvoker`）—— 于是"工具来自
一个远端 MCP 服务端"对 Kernel 与 Harness 完全透明（T-6：Runtime 不关心协议）。

M4 的另一半 —— **A2A**（远端 Agent 委派）—— 复用同一层 JSON-RPC，
但它要处理 Agent Card / 任务生命周期 / 异步结果回传，是单独一轮的事。
"""
from .jsonrpc import (
    JSONRPC_VERSION,
    JsonRpcClient,
    JsonRpcError,
    JsonRpcProtocolError,
    JsonRpcRequest,
    Transport,
)
from .mcp import (
    MCP_PROTOCOL_VERSION,
    McpClient,
    McpInvoker,
    McpTool,
    register_mcp_tools,
)
from .transport import InMemoryTransport, StdioTransport, TransportError

__all__ = [
    "JSONRPC_VERSION",
    "InMemoryTransport",
    "JsonRpcClient",
    "JsonRpcError",
    "JsonRpcProtocolError",
    "JsonRpcRequest",
    "MCP_PROTOCOL_VERSION",
    "McpClient",
    "McpInvoker",
    "McpTool",
    "StdioTransport",
    "Transport",
    "TransportError",
    "register_mcp_tools",
]
