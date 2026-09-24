"""Connectivity（M4）—— 把 AgentOS 接到**别的系统**的两套协议。

```text
JSON-RPC 2.0（jsonrpc.py）          一次 RPC 的请求/响应/错误，与协议无关
        │
        ├── Transport（transport.py）   stdio（MCP）/ 内存（测试）/ [HTTP：待办]
        │
        ├── MCP（mcp.py）               工具连接：initialize / tools/list / tools/call
        │
        └── A2A（a2a.py）               远端 Agent 委派：AgentCard / message/send / tasks/*
```

**边界**：本包不 import `execution_kernel`，也不 import `agent_harness`。
它只认识 ToolRuntime 的 Invoker 协议（`ToolInvoker`）与委派的
`ChildRunSpawner` 协议 —— 于是"工具来自一个远端 MCP 服务端"
与"子任务交给一个远端 Agent"对 Kernel 与 Harness 完全透明。
"""
from .a2a import (
    AGENT_CARD_PATH,
    TERMINAL_A2A_STATES,
    A2AClient,
    A2AChildRunSpawner,
    A2ATaskState,
    AgentCard,
    AgentCardTransport,
    HttpCardTransport,
    InMemoryCardTransport,
    parse_state,
)
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
    "AGENT_CARD_PATH",
    "JSONRPC_VERSION",
    "MCP_PROTOCOL_VERSION",
    "TERMINAL_A2A_STATES",
    "A2AClient",
    "A2AChildRunSpawner",
    "A2ATaskState",
    "AgentCard",
    "AgentCardTransport",
    "HttpCardTransport",
    "InMemoryCardTransport",
    "InMemoryTransport",
    "JsonRpcClient",
    "JsonRpcError",
    "JsonRpcProtocolError",
    "JsonRpcRequest",
    "McpClient",
    "McpInvoker",
    "McpTool",
    "StdioTransport",
    "Transport",
    "TransportError",
    "parse_state",
    "register_mcp_tools",
]
