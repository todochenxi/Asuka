"""AgentOS 客户端（M58 / M11 Developer Platform 的 SDK 部分）。

--------------------------------------------------------------------------
它是什么

控制面 HTTP API 的**薄壳**。屏幕上／返回值里每一个数字都由服务真的回答，
它不推断、不缓存、不补全。

--------------------------------------------------------------------------
为什么必须有它（B-7：一个事实一处定义）

在此之前，**同一套 urllib 封装写了两遍**：

    apps/cli/__init__.py                 request(...)
    packages/agent_evaluation/harness.py _http(...)

两份都各自带着"绕过系统代理"那段（连 localhost 走了代理，
服务没起时会拿到代理返回的 502，"连不上"被误报成"服务返回错误"）。

那种知识**只许有一份**：改一处忘一处，就会出现"CLI 修好了、
评估还在误报"这种只对了一半的修复。

现在 CLI 与评估平台都走这里。

--------------------------------------------------------------------------
错误语义

`request()` **不**把 HTTP 错误吞成异常 —— 调用方要能区分：

    (0, None, "")      连不上（环境问题：该去检查部署）
    (>=400, ...)       服务说了不（业务结果）

这两种失败的处置完全不同，压成一个异常就分不开了（PR-19）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

DEFAULT_BASE = "http://127.0.0.1:8011"

import os


def _build_no_proxy_opener() -> Any:
    """构建一个**不走代理**的 opener（抽成函数是为了能被直接测）。

    连的是本地控制面，走 `HTTP_PROXY` 本身就是错的：本机服务没起时
    会拿到代理的 502 `upstream connect failed`，
    于是"连不上"被误报成"服务返回了一个错误"。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


#: 模块级**共享**一份 opener。
#:
#: 刻意不是"每个 client 一个"：`build_opener()` 会顺带创建 SSL context，
#: 而一次评估要打几十上百个请求 —— 每次都建一个，会把 OpenSSL 拖到
#: 建不出 context（`SSLError: [SSL] unknown error`）。
#: opener 是无状态的，共享它没有任何问题。
_OPENER: Any = None


def _shared_opener() -> Any:
    global _OPENER
    if _OPENER is None:
        _OPENER = _build_no_proxy_opener()
    return _OPENER


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


class Unreachable(RuntimeError):
    """连不上控制面 —— 环境问题，不是业务结果。"""


class AgentOSClient:
    """控制面的客户端。每个方法对应一个端点。"""

    def __init__(self, base: str = "", *, timeout: float = 15.0) -> None:
        self.base = (base or os.environ.get("AGENTOS_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # ---------------------------------------------------------------- 底层

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any, str]:
        """打一次 HTTP。返回 `(状态码, 解析后的 JSON 或 None, 原始文本)`。

        状态码 `0` 表示连不上 —— 一个 HTTP 里不可能出现的值，
        用来和"服务返回了错误"区分开。
        """
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _shared_opener().open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except (urllib.error.URLError, OSError):
            return (0, None, "")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return (status, parsed, raw)

    def request_or_raise(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """给"连不上就是致命错误"的场景（如批处理）：连不上直接抛。"""
        status, parsed, raw = self.request(
            method, path, body=body, headers=headers, timeout=timeout
        )
        if status == 0:
            raise Unreachable(f"cannot reach {self.base}{path}")
        return parsed

    # ---------------------------------------------------------------- 端点

    def health(self) -> Any:
        return self.request_or_raise("GET", "/health")

    def start_run(
        self, agent_id: str, user_request: str, *, key: str = ""
    ) -> Any:
        headers = {"Idempotency-Key": key} if key else None
        return self.request_or_raise(
            "POST",
            f"/agents/{_quote(agent_id)}/runs",
            body={"user_request": user_request},
            headers=headers,
        )

    def get_run(self, run_id: str) -> Any:
        return self.request_or_raise("GET", f"/runs/{_quote(run_id)}")

    def step(self, run_id: str) -> Any:
        return self.request_or_raise("POST", f"/runs/{_quote(run_id)}/step", body={})

    def drive(self, run_id: str) -> Any:
        return self.request_or_raise("POST", f"/runs/{_quote(run_id)}/run", body={})

    def cancel(
        self, run_id: str, *, reason: str, by: str, key: str = ""
    ) -> Any:
        headers = {"Idempotency-Key": key} if key else None
        return self.request_or_raise(
            "POST",
            f"/runs/{_quote(run_id)}/cancel",
            body={"reason": reason, "by": by},
            headers=headers,
        )

    def approvals(self) -> Any:
        return self.request_or_raise("GET", "/approvals")

    def decide(
        self,
        run_id: str,
        approval_id: str,
        *,
        approve: bool,
        by: str = "human",
        comment: str = "",
    ) -> Any:
        """契约是 `decision: "approve" | "reject"`，不是 `approved: true`。"""
        return self.request_or_raise(
            "POST",
            f"/runs/{_quote(run_id)}/approvals/{_quote(approval_id)}/decision",
            body={
                "decision": "approve" if approve else "reject",
                "by": by,
                "comment": comment,
            },
        )

    def trace(self, run_id: str) -> Any:
        return self.request_or_raise("GET", f"/runs/{_quote(run_id)}/trace")
