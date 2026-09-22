"""`apps.cli`：退出码、归因、契约字段名（M50）。

--------------------------------------------------------------------------
为什么要有这些测试

CLI 是给脚本用的。脚本只看两样东西：**退出码**和 **stdout**。
一个"连不上却返回 0"的 CLI 会让 CI 以为流程跑通了 ——
而它其实什么都没做。所以退出码的语义必须被测住，不能靠肉眼。

--------------------------------------------------------------------------
测的是**契约**，不是网络

真服务的往返由端到端验证覆盖（手工 / 集成层）。这里测的是 CLI 自己的
三件决定：退出码怎么定、归因缺了怎么办、请求体长什么样。
它们都是纯函数式的判断，不需要起服务。
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from apps.cli import (
    DEFAULT_BASE,
    EXIT_HTTP_ERROR,
    EXIT_OK,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    _base_url,
    _error_of,
    cmd_cancel,
    cmd_decide,
    cmd_health,
    request,
)


def _ns(**kw):
    """造一个 argparse.Namespace 的替身（只要属性在就行）。"""
    return type("NS", (), kw)()


class TestExitCodes(unittest.TestCase):
    """退出码的语义必须稳定 —— 脚本靠它分支。"""

    def test_unreachable_is_three_not_two(self):
        """连不上服务是**环境问题**，不是"服务说了不"。

        这两者必须分开：前者该让 CI 去检查部署，后者是业务结果。
        """
        with mock.patch("apps.cli.request", return_value=(0, None, "")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = cmd_health(_ns(base=""))
        self.assertEqual(code, EXIT_UNREACHABLE)
        self.assertIn("连不上服务", buf.getvalue())

    def test_http_error_is_two(self):
        """服务返回 4xx / 5xx → 2，且原因要说中真发生了什么（PR-19）。"""
        payload = {"error": {"code": "RUN_NOT_FOUND", "message": "no such run"}}
        with mock.patch("apps.cli.request", return_value=(404, payload, "")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = cmd_health(_ns(base=""))
        self.assertEqual(code, EXIT_HTTP_ERROR)
        self.assertIn("RUN_NOT_FOUND", buf.getvalue())

    def test_success_is_zero(self):
        with mock.patch("apps.cli.request", return_value=(200, {"status": "ok"}, "")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cmd_health(_ns(base=""))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ok", buf.getvalue())


class TestAttributionIsRequired(unittest.TestCase):
    """B-8 / A-8：取消必须能归因 —— 缺了就拒绝，不替用户编一句。"""

    def test_missing_reason_is_refused(self):
        code = cmd_cancel(_ns(run_id="r1", reason="", by="alice", base="", key=""))
        self.assertEqual(code, EXIT_USAGE)

    def test_missing_by_is_refused(self):
        code = cmd_cancel(_ns(run_id="r1", reason="x", by="", base="", key=""))
        self.assertEqual(code, EXIT_USAGE)

    def test_refusal_explains_why(self):
        """拒绝要说清楚缺的是什么 —— 不然用户只会重试。"""
        buf = io.StringIO()
        with redirect_stderr(buf):
            cmd_cancel(_ns(run_id="r1", reason="", by="", base="", key=""))
        self.assertIn("B-8", buf.getvalue())


class TestDecisionContract(unittest.TestCase):
    """契约是 `decision: "approve" | "reject"`，不是 `approved: true`。

    页面当初就是在这里栽的（写了 `approved: true` → 400）。
    CLI 不能重蹈覆辙，所以这条被单独测住。
    """

    def test_approve_sends_decision_approve(self):
        with mock.patch("apps.cli.request", return_value=(200, {}, "")) as m:
            cmd_decide(_ns(run_id="r", approval_id="a", approve=True,
                           reject=False, by="alice", comment="", base=""))
        body = m.call_args.kwargs["body"]
        self.assertEqual(body["decision"], "approve")
        self.assertNotIn("approved", body)

    def test_reject_sends_decision_reject(self):
        with mock.patch("apps.cli.request", return_value=(200, {}, "")) as m:
            cmd_decide(_ns(run_id="r", approval_id="a", approve=False,
                           reject=True, by="alice", comment="", base=""))
        body = m.call_args.kwargs["body"]
        self.assertEqual(body["decision"], "reject")


class TestBaseUrlResolution(unittest.TestCase):
    """`--base` > 环境变量 > 默认。"""

    def test_explicit_wins(self):
        self.assertEqual(_base_url("http://x:1"), "http://x:1")

    def test_env_fallback(self):
        with mock.patch.dict("os.environ", {"AGENTOS_API_BASE": "http://env:2"}):
            self.assertEqual(_base_url(""), "http://env:2")

    def test_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_base_url(""), DEFAULT_BASE)


class TestNoProxy(unittest.TestCase):
    """CLI 连本地控制面，不许走系统代理。

    走了代理的后果：本机服务没起时拿到代理的 502 `upstream connect failed`，
    于是"连不上"被误报成"服务返回了一个错误"（退出码 2 而不是 3）。

    这条测的是**行为**：设一个指向黑洞的假代理，再去连一个没人听的端口。
    若代码走了代理，会拿到代理返回的 5xx 而不是"连不上"。
    测行为而不是测 opener 的内部 handler 列表 ——
    后者会随 urllib 版本变，前者不会。
    """

    def test_it_builds_an_opener_with_proxy_disabled(self):
        """构建 opener 时必须显式传一个**空代理表**。

        为什么测这个而不是测"连不上返回 0"：
        后者无论走不走代理都是 0（代理自己也连不上），**区分不出来** ——
        那是一条会假绿的断言（本轮变红验证时抓到过一次）。
        真正能证明"没走代理"的，是 opener 构造时拿到一张空代理表。
        """
        import urllib.request

        from apps.cli import _build_no_proxy_opener

        captured = {}

        real_build = urllib.request.build_opener

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return real_build(*handlers)

        with mock.patch.object(urllib.request, "build_opener", fake_build_opener):
            _build_no_proxy_opener()

        phs = [h for h in captured.get("handlers", ())
               if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(phs, "build_opener 必须收到一个 ProxyHandler")
        self.assertEqual(phs[0].proxies, {}, "代理表必须是空的")

    def test_unreachable_returns_zero(self):
        """连不上 → 0（调用方据此给退出码 3）。"""
        status, _payload, _raw = request(
            "GET", "/health", base="http://127.0.0.1:9", timeout=3.0
        )
        self.assertEqual(status, 0)


class TestErrorExtraction(unittest.TestCase):
    """报错要说中真发生了什么（PR-19）。"""

    def test_nested_error_object(self):
        self.assertIn("NOPE", _error_of({"error": {"code": "NOPE", "message": "m"}}, ""))

    def test_detail_field(self):
        self.assertIn("nope", _error_of({"detail": "nope"}, ""))

    def test_falls_back_to_raw(self):
        self.assertEqual(_error_of(None, "raw text")[:8], "raw text")


if __name__ == "__main__":
    unittest.main()
