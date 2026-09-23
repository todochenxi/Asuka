"""M103 · MCP 客户端 + Tool Invoker（M4）。

补的空洞：`ToolProtocol.MCP` 与 `ToolRegistry` 的注释都提到 MCP，
但全仓没有一行真的说过它。这里给三段真实实现（client / tool / invoker）
加一个把服务端工具登记进 `ToolRegistry` 的桥，并钉住两条不变量：

    MC-1  没有名字（或重名）的 MCP 工具拒绝登记
    MC-2  `side_effect` 不猜：只有服务端声明 `readOnlyHint` 才是 READ

最后一条用**真子进程**（一个 stdio MCP 服务端）端到端跑一遍 ——
否则"stdio 传输能用"只是"我们包了一层 Popen"。
"""
from __future__ import annotations

import sys
import unittest

from packages.agent_runtime.connectivity import (
    InMemoryTransport,
    JsonRpcClient,
    McpClient,
    StdioTransport,
    register_mcp_tools,
)
from packages.agent_runtime.tool_runtime import (
    SideEffect,
    ToolExecutionError,
    ToolProtocol,
    ToolRegistry,
    ToolRuntime,
)

_TOOLS = {
    "tools": [
        {
            "name": "echo",
            "description": "echo it back",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "read_doc",
            "description": "read a doc",
            "annotations": {"readOnlyHint": True},
        },
    ]
}

#: 一个最小 MCP 服务端：换行分隔的 JSON-RPC over stdio。
_SERVER = r"""
import json, sys

def reply(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    if "id" not in req:
        continue
    method = req.get("method")
    if method == "initialize":
        reply(req["id"], {"protocolVersion": "2025-06-18",
                          "capabilities": {},
                          "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "tools/list":
        reply(req["id"], {"tools": [{
            "name": "echo",
            "description": "echo it back",
            "inputSchema": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]},
        }]})
    elif method == "tools/call":
        args = req.get("params", {}).get("arguments", {})
        reply(req["id"], {"content": [{"type": "text",
                                       "text": "echo:" + str(args.get("text", ""))}],
                          "isError": False})
    else:
        reply(req["id"], None)
"""


def _client(responses) -> McpClient:
    return McpClient(JsonRpcClient(InMemoryTransport(responses)))


class InitializeTest(unittest.TestCase):
    def test_it_sends_the_client_info_and_the_initialized_notification(self) -> None:
        transport = InMemoryTransport(
            {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}}
        )
        info = McpClient(JsonRpcClient(transport)).initialize()

        self.assertEqual(info["serverInfo"]["name"], "fake")
        methods = [p["method"] for p in transport.sent]
        self.assertIn("notifications/initialized", methods)
        init = next(p for p in transport.sent if p["method"] == "initialize")
        self.assertEqual(init["params"]["clientInfo"]["name"], "agentos")
        self.assertIn("protocolVersion", init["params"])


class ListToolsTest(unittest.TestCase):
    def test_it_parses_tools_and_their_read_only_hint(self) -> None:
        tools = {t.name: t for t in _client({"tools/list": _TOOLS}).list_tools()}
        self.assertEqual(set(tools), {"echo", "read_doc"})
        self.assertFalse(tools["echo"].read_only)
        self.assertTrue(tools["read_doc"].read_only)
        self.assertEqual(tools["echo"].input_schema["required"], ["text"])

    def test_mc1_a_tool_without_a_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _client({"tools/list": {"tools": [{"description": "nameless"}]}}).list_tools()

    def test_mc1_a_duplicate_tool_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _client({"tools/list": {"tools": [{"name": "a"}, {"name": "a"}]}}).list_tools()


class CallToolTest(unittest.TestCase):
    def test_the_idempotency_key_travels_in_meta(self) -> None:
        transport = InMemoryTransport(
            {"tools/call": {"content": [{"type": "text", "text": "ok"}], "isError": False}}
        )
        McpClient(JsonRpcClient(transport)).call_tool(
            "echo", {"text": "hi"}, idempotency_key="exec-1"
        )
        params = transport.sent[0]["params"]
        self.assertEqual(params["name"], "echo")
        self.assertEqual(params["arguments"], {"text": "hi"})
        self.assertEqual(params["_meta"]["idempotencyKey"], "exec-1")


class RegisterToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.client = _client(
            {
                "tools/list": _TOOLS,
                "tools/call": {"content": [{"type": "text", "text": "echo:hi"}], "isError": False},
            }
        )

    def test_it_registers_tools_with_the_mcp_protocol(self) -> None:
        register_mcp_tools(self.registry, self.client)
        spec = self.registry.resolve("echo")
        self.assertIs(spec.protocol, ToolProtocol.MCP)
        self.assertEqual(spec.description, "echo it back")

    def test_mc2_only_a_declared_read_only_tool_is_read(self) -> None:
        specs = {s.name: s for s in register_mcp_tools(self.registry, self.client)}
        self.assertIs(specs["read_doc"].side_effect, SideEffect.READ)
        self.assertIs(specs["echo"].side_effect, SideEffect.UNKNOWN)

    def test_a_prefix_disambiguates_between_servers(self) -> None:
        register_mcp_tools(self.registry, self.client, prefix="mcp.")
        self.assertTrue(self.registry.has("mcp.echo"))

    def test_an_unknown_side_effect_tool_needs_an_idempotency_key(self) -> None:
        """MC-2 的下游：UNKNOWN ⇒ T-2 硬拒绝没有键的调用。"""
        register_mcp_tools(self.registry, self.client)
        runtime = ToolRuntime(self.registry)
        with self.assertRaises(ToolExecutionError) as ctx:
            runtime.call("echo", {"text": "hi"})
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_KEY_REQUIRED")

    def test_a_read_only_tool_runs_without_a_key(self) -> None:
        register_mcp_tools(self.registry, self.client)
        result = ToolRuntime(self.registry).call("read_doc", {})
        self.assertEqual(result.output["text"], "echo:hi")
        self.assertIs(result.side_effect, SideEffect.READ)

    def test_is_error_becomes_a_named_tool_failure(self) -> None:
        registry = ToolRegistry()
        register_mcp_tools(
            registry,
            _client(
                {
                    "tools/list": _TOOLS,
                    "tools/call": {"content": [{"type": "text", "text": "bad"}], "isError": True},
                }
            ),
        )
        with self.assertRaises(ToolExecutionError) as ctx:
            ToolRuntime(registry).call("read_doc", {})
        self.assertEqual(ctx.exception.code, "MCP_TOOL_ERROR")


class StdioEndToEndTest(unittest.TestCase):
    """真的起一个子进程，用换行分隔的 JSON 说话 —— MCP 的 stdio 形态。"""

    def test_a_real_stdio_server_works(self) -> None:
        transport = StdioTransport([sys.executable, "-c", _SERVER])
        try:
            client = McpClient(JsonRpcClient(transport))
            client.initialize()
            tools = client.list_tools()
            self.assertEqual([t.name for t in tools], ["echo"])

            result = client.call_tool("echo", {"text": "hi"})
            self.assertEqual(result["content"][0]["text"], "echo:hi")
        finally:
            transport.close()

    def test_an_mcp_invoker_drives_a_real_server(self) -> None:
        transport = StdioTransport([sys.executable, "-c", _SERVER])
        try:
            registry = ToolRegistry()
            register_mcp_tools(registry, McpClient(JsonRpcClient(transport)))
            result = ToolRuntime(registry).call("echo", {"text": "yo"}, idempotency_key="k")
            self.assertEqual(result.output["text"], "echo:yo")
        finally:
            transport.close()

    def test_closing_the_transport_reaps_the_process(self) -> None:
        transport = StdioTransport([sys.executable, "-c", _SERVER])
        transport.close()
        self.assertIsNotNone(transport._proc.poll())


if __name__ == "__main__":
    unittest.main()
