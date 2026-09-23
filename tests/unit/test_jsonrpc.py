"""M103 · JSON-RPC 2.0 核心（MCP 与 A2A 共用的那一层）。

补的空洞：MCP / A2A 都是 JSON-RPC 2.0，但全仓没有一行实现过它。
这一层把**协议层的三种病**挡在门口（J-1 / J-2 / J-3），
因为它们的共同后果都是"把别人的东西当成自己的结果读下去"。
"""
from __future__ import annotations

import unittest
from typing import Any, Mapping

from packages.agent_runtime.connectivity import (
    InMemoryTransport,
    JsonRpcClient,
    JsonRpcError,
    JsonRpcProtocolError,
    JsonRpcRequest,
    TransportError,
)


class _RawTransport:
    """返回一段**写死**的响应 —— 用来构造 InMemoryTransport 造不出的坏响应。"""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.sent: list[dict[str, Any]] = []

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.sent.append(dict(payload))
        return self.payload

    def send(self, payload: Mapping[str, Any]) -> None:
        self.sent.append(dict(payload))

    def close(self) -> None:  # pragma: no cover
        pass


class CallTest(unittest.TestCase):
    def test_a_call_returns_the_result(self) -> None:
        client = JsonRpcClient(InMemoryTransport({"add": 7}))
        self.assertEqual(client.call("add", {"a": 3, "b": 4}), 7)

    def test_the_request_is_well_formed(self) -> None:
        transport = InMemoryTransport({"ping": "pong"})
        JsonRpcClient(transport).call("ping")
        sent = transport.sent[0]
        self.assertEqual(sent["jsonrpc"], "2.0")
        self.assertEqual(sent["method"], "ping")
        self.assertIn("id", sent)

    def test_a_notification_carries_no_id(self) -> None:
        transport = InMemoryTransport()
        JsonRpcClient(transport).notify("notifications/initialized")
        self.assertNotIn("id", transport.sent[0])
        self.assertEqual(transport.sent[0]["method"], "notifications/initialized")

    def test_ids_are_distinct_per_call(self) -> None:
        transport = InMemoryTransport({"a": 1, "b": 2})
        client = JsonRpcClient(transport)
        client.call("a")
        client.call("b")
        self.assertNotEqual(transport.sent[0]["id"], transport.sent[1]["id"])

    def test_an_unknown_method_is_a_transport_error(self) -> None:
        with self.assertRaises(TransportError):
            JsonRpcClient(InMemoryTransport({})).call("ghost")


class ServerErrorTest(unittest.TestCase):
    def test_an_error_response_raises_jsonrpc_error(self) -> None:
        client = JsonRpcClient(
            InMemoryTransport({"boom": JsonRpcError(-32000, "kaboom", {"x": 1})})
        )
        with self.assertRaises(JsonRpcError) as ctx:
            client.call("boom")
        self.assertEqual(ctx.exception.code, -32000)
        self.assertEqual(ctx.exception.data, {"x": 1})

    def test_a_malformed_error_object_is_a_protocol_error(self) -> None:
        """J-2：没有 code 的 error 不许被"尽量理解"。"""
        raw = _RawTransport({"jsonrpc": "2.0", "id": 1, "error": {"message": "no code"}})
        with self.assertRaises(JsonRpcProtocolError):
            JsonRpcClient(raw).call("x")


class ProtocolDisciplineTest(unittest.TestCase):
    """J-1 / J-2 / J-3：三种"对面没在说这门协议"的形态。"""

    def _client_for(self, payload: Mapping[str, Any]) -> JsonRpcClient:
        return JsonRpcClient(_RawTransport(payload))

    def test_j1_a_wrong_version_is_refused(self) -> None:
        with self.assertRaises(JsonRpcProtocolError):
            self._client_for({"jsonrpc": "1.0", "id": 1, "result": 1}).call("x")

    def test_j2_neither_result_nor_error_is_refused(self) -> None:
        with self.assertRaises(JsonRpcProtocolError):
            self._client_for({"jsonrpc": "2.0", "id": 1}).call("x")

    def test_j2_both_result_and_error_is_refused(self) -> None:
        with self.assertRaises(JsonRpcProtocolError):
            self._client_for(
                {"jsonrpc": "2.0", "id": 1, "result": 1, "error": {"code": 1, "message": "m"}}
            ).call("x")

    def test_j3_a_mismatched_id_is_refused(self) -> None:
        """响应串了线 —— 拿它当自己的结果，比报错危险得多。"""
        with self.assertRaises(JsonRpcProtocolError) as ctx:
            self._client_for({"jsonrpc": "2.0", "id": 999, "result": "someone else's"}).call("x")
        self.assertIn("999", str(ctx.exception))

    def test_a_non_object_response_is_refused(self) -> None:
        with self.assertRaises(JsonRpcProtocolError):
            self._client_for(["not", "an", "object"]).call("x")


class RequestTest(unittest.TestCase):
    def test_a_request_without_params_omits_the_key(self) -> None:
        wire = JsonRpcRequest(method="ping", id=1).to_wire()
        self.assertNotIn("params", wire)

    def test_a_notification_request_omits_the_id(self) -> None:
        wire = JsonRpcRequest(method="note").to_wire()
        self.assertNotIn("id", wire)


if __name__ == "__main__":
    unittest.main()
