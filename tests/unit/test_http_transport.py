"""M108 · HTTP 传输（M4）：MCP Streamable HTTP / A2A 的 HTTP 形态。

此前只有 `InMemoryTransport`（测试）与 `StdioTransport`（MCP 的 stdio）——
"MCP / A2A 走 HTTP"这件事没有落点。这里用 stdlib `urllib` 补上，
并用**真起一个本地 HTTP 服务端**来端到端验证（不然只是"我们包了一层 urlopen"）。

覆盖两种响应形态：
    application/json     单条响应（A2A 的全部 / MCP 的非流式）
    text/event-stream    SSE 流，逐条 data: 都是 JSON-RPC 消息
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from packages.agent_runtime.connectivity import (
    A2AClient,
    HttpTransport,
    JsonRpcClient,
    JsonRpcError,
    McpClient,
    TransportError,
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 别把测试输出弄脏
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if "id" not in request:  # 通知
            self.send_response(202)
            self.end_headers()
            return
        method = request.get("method")
        rid = request["id"]
        if method == "ping":
            self._json({"jsonrpc": "2.0", "id": rid, "result": {"echo": "ping"}})
        elif method == "auth":
            self._json(
                {"jsonrpc": "2.0", "id": rid, "result": {"authorization": self.headers.get("Authorization", "")}}
            )
        elif method == "tools/list":
            self._json(
                {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "echo"}]}}
            )
        elif method == "message/send":
            self._json(
                {"jsonrpc": "2.0", "id": rid, "result": {"id": "t1", "status": {"state": "working"}}}
            )
        elif method == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            other = {"jsonrpc": "2.0", "id": 999, "result": "someone else's"}
            mine = {"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}
            self.wfile.write(b"event: message\ndata: " + json.dumps(other).encode() + b"\n\n")
            self.wfile.write(b"event: message\ndata: " + json.dumps(mine).encode() + b"\n\n")
            self.wfile.flush()
        elif method == "boom":
            self._json(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "kaboom"}},
                status=400,
            )
        elif method == "html":
            body = b"<html>not json</html>"
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class _ServerCase(unittest.TestCase):
    """起一个本地 HTTP 服务端；先 `shutdown`（停循环）再 `server_close`（放套接字）。"""

    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class HttpTransportTest(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = JsonRpcClient(HttpTransport(self.url))

    def test_a_json_response_is_returned(self) -> None:
        self.assertEqual(self.client.call("ping"), {"echo": "ping"})

    def test_headers_are_sent(self) -> None:
        client = JsonRpcClient(HttpTransport(self.url, headers={"Authorization": "Bearer tok"}))
        self.assertEqual(client.call("auth"), {"authorization": "Bearer tok"})

    def test_an_sse_stream_returns_the_matching_message(self) -> None:
        """SSE 流里有别人的消息（id=999）时，只认 id 匹配的那一条。"""
        self.assertEqual(self.client.call("sse"), {"ok": True})

    def test_a_notification_does_not_wait_for_a_body(self) -> None:
        self.client.notify("anything")  # 服务端回 202；不抛就是通过

    def test_a_jsonrpc_error_in_an_http_body_is_raised(self) -> None:
        with self.assertRaises(JsonRpcError) as ctx:
            self.client.call("boom")
        self.assertEqual(ctx.exception.code, -32000)

    def test_a_non_json_http_error_is_a_transport_error(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            self.client.call("html")
        self.assertIn("500", str(ctx.exception))

    def test_an_unreachable_url_is_a_transport_error(self) -> None:
        client = JsonRpcClient(HttpTransport("http://127.0.0.1:1/nope", timeout=2.0))
        with self.assertRaises(TransportError):
            client.call("ping")


class ProtocolOverHttpTest(_ServerCase):
    """MCP / A2A 通过 `from_http` 走真 HTTP。"""

    def test_mcp_lists_tools_over_http(self) -> None:
        tools = McpClient.from_http(self.url).list_tools()
        self.assertEqual([t.name for t in tools], ["echo"])

    def test_a2a_sends_a_message_over_http(self) -> None:
        task = A2AClient.from_http(self.url).send_message("hi")
        self.assertEqual(task["id"], "t1")


if __name__ == "__main__":
    unittest.main()
