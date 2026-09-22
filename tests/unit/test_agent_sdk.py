"""M58 / M11 SDK：客户端的契约。

最要紧的两条：

1. **HTTP 封装只许有一份**（B-7）。CLI 与评估平台共用 SDK 的
   `request()`，而"绕过系统代理"那段知识只写在 SDK 里。
   这一条被测住，才不会出现"改一处忘一处"。
2. **连不上与"服务说了不"必须分得开**（PR-19）。
   `request()` 用状态码 `0` 表示连不上 —— 一个 HTTP 里不可能出现的值。
"""
from __future__ import annotations

import unittest
from unittest import mock

from packages.agent_sdk import DEFAULT_BASE, AgentOSClient, Unreachable


class TestBaseResolution(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(AgentOSClient(base="http://x:1").base, "http://x:1")

    def test_env_fallback(self):
        with mock.patch.dict("os.environ", {"AGENTOS_API_BASE": "http://env:2"}):
            self.assertEqual(AgentOSClient().base, "http://env:2")

    def test_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(AgentOSClient().base, DEFAULT_BASE)

    def test_trailing_slash_is_stripped(self):
        self.assertEqual(AgentOSClient(base="http://x:1/").base, "http://x:1")


class TestProxyIsDisabled(unittest.TestCase):
    """连本地控制面走了代理 = "服务没起"被误报成代理的 502。

    这段知识现在只在 SDK 里有一份，所以**这里**测住它，
    就等于同时守住了 CLI 和评估平台（两者共用）。
    """

    def test_opener_is_built_with_an_empty_proxy_table(self):
        import urllib.request

        from packages.agent_sdk import _build_no_proxy_opener

        captured = {}
        real = urllib.request.build_opener

        def fake(*handlers):
            captured["handlers"] = handlers
            return real(*handlers)

        with mock.patch.object(urllib.request, "build_opener", fake):
            _build_no_proxy_opener()

        phs = [h for h in captured.get("handlers", ())
               if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(phs, "必须传一个 ProxyHandler")
        self.assertEqual(phs[0].proxies, {}, "代理表必须是空的")

    def test_the_opener_is_shared_not_rebuilt_per_request(self):
        """opener 共享一份 —— 每个请求都 `build_opener()` 会拖垮 OpenSSL。"""
        from packages.agent_sdk import _shared_opener

        self.assertIs(_shared_opener(), _shared_opener())


class TestErrorSemantics(unittest.TestCase):
    """连不上 vs 服务说了不。"""

    def test_unreachable_returns_zero(self):
        status, parsed, _raw = AgentOSClient(base="http://127.0.0.1:9").request(
            "GET", "/health", timeout=3.0
        )
        self.assertEqual(status, 0, "连不上必须是 0，不是 5xx")
        self.assertIsNone(parsed)

    def test_request_or_raise_raises_when_unreachable(self):
        with self.assertRaises(Unreachable):
            AgentOSClient(base="http://127.0.0.1:9").request_or_raise(
                "GET", "/health", timeout=3.0
            )


class TestContractShapes(unittest.TestCase):
    """请求体的形状 —— 页面当初就在 `approved` 上栽过。"""

    def test_decide_sends_decision_not_approved(self):
        client = AgentOSClient(base="http://x")
        with mock.patch.object(client, "request", return_value=(200, {}, "")) as m:
            client.decide("r1", "a1", approve=True, by="alice")
        body = m.call_args.kwargs["body"]
        self.assertEqual(body["decision"], "approve")
        self.assertNotIn("approved", body)

    def test_cancel_requires_attribution(self):
        """B-8：归因是必填参数 —— 缺了就 TypeError，不替调用方编。"""
        client = AgentOSClient(base="http://x")
        with self.assertRaises(TypeError):
            client.cancel("r1")

    def test_idempotency_key_goes_to_the_header(self):
        """幂等键必须进 header，不是 body。"""
        client = AgentOSClient(base="http://x")
        with mock.patch.object(client, "request", return_value=(200, {}, "")) as m:
            client.start_run("agent-it", "x", key="k1")
        self.assertEqual(m.call_args.kwargs["headers"]["Idempotency-Key"], "k1")


if __name__ == "__main__":
    unittest.main()
