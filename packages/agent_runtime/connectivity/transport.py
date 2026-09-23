"""传输：把一条 JSON-RPC 消息送出去、把响应取回来（M4）。

`jsonrpc.py` 只认 `Transport` 这个协议，不认它底下是 stdio、HTTP 还是内存。
于是"换传输"与"换协议"是两个互不干扰的动作：

    InMemoryTransport   脚本化的对端，测试与本地替身（不连任何东西）
    StdioTransport      换行分隔的 JSON-RPC over stdin/stdout —— **MCP 的 stdio 传输**

⚠️ HTTP 传输（MCP 的 Streamable HTTP、A2A 的 HTTP）刻意**不在这里**：
它要处理 SSE、重连、鉴权，是另一件事；先把它当成一条待办，
而不是塞一个只会 GET 一次的假 HTTP 进来（PR-34：宁可拒绝，不要编）。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .jsonrpc import JsonRpcError


class TransportError(Exception):
    """链路层失败：对端没了、写不出去、读回来一行不是 JSON。

    与 `JsonRpcProtocolError` / `JsonRpcError` 三者分开，是因为处置不同：
    链路坏了可以重连 / 重试，协议对不上要换版本，业务报错要看 code。
    """


# ---------------------------------------------------------------------------
# 内存传输：脚本化的对端
# ---------------------------------------------------------------------------


@dataclass
class InMemoryTransport:
    """`method → 响应` 的脚本化对端。

    值可以是：

        普通值             → 包成 `{"jsonrpc":"2.0","id":<req id>,"result": value}`
        `JsonRpcError`     → 包成 `error`
        `Callable(params)` → 用它的返回值按上面两条处理（动态响应）

    `sent` 记下每一条**发出去**的请求 —— 断言"我们到底发了什么"靠它。
    """

    responses: Mapping[str, Any] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def send(self, payload: Mapping[str, Any]) -> None:
        """通知：只记下，不产生响应（真实对端同样不回答）。"""
        self.sent.append(dict(payload))

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.sent.append(dict(payload))
        method = payload.get("method")
        if method not in self.responses:
            raise TransportError(
                f"the in-memory peer has no script for method {method!r}; "
                f"known methods: {sorted(self.responses)}"
            )
        value = self.responses[method]
        if callable(value):
            value = value(payload.get("params"))
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": payload.get("id")}
        if isinstance(value, JsonRpcError):
            response["error"] = value.to_wire()
        else:
            response["result"] = value
        return response

    def close(self) -> None:  # pragma: no cover - 没有东西要关
        pass


# ---------------------------------------------------------------------------
# stdio 传输：MCP 的 stdio 形态
# ---------------------------------------------------------------------------


class StdioTransport:
    """起一个子进程，用**换行分隔的 JSON** 与它说话。

    MCP 的 stdio 传输就是这一条：客户端写一行请求到 stdin，
    服务端写一行响应到 stdout。刻意用 `text=True` + 行缓冲 ——
    二进制模式下 `readline()` 也能工作，但编码错误会变成静默的乱码。

    ⚠️ 没有超时。`readline()` 会一直等，一个卡住的服务端会挂住调用方。
    超时属于**调用方**（ToolRuntime 的 `timeout` 或进程级看门狗），
    在传输层加一个半吊子超时只会制造"到底谁超时了"的第二个答案。
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.argv = list(argv)
        self._proc = subprocess.Popen(  # noqa: S603 - argv 列表，shell=False
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _write_line(self, payload: Mapping[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None:  # pragma: no cover - Popen 一定给了
            raise TransportError("the stdio peer has no pipes")
        try:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()
        except (BrokenPipeError, ValueError) as err:
            raise TransportError(f"cannot write to the stdio peer: {err}") from err

    def send(self, payload: Mapping[str, Any]) -> None:
        """通知：写一行就走，**不**读响应。"""
        self._write_line(payload)

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover - Popen 一定给了
            raise TransportError("the stdio peer has no pipes")
        self._write_line(payload)
        line = stdout.readline()
        if not line:
            raise TransportError(
                "the stdio peer closed its output (EOF) before answering; "
                f"exit code={self._proc.poll()}"
            )
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as err:
            raise TransportError(f"peer wrote a non-JSON line: {line[:200]!r}") from err
        if not isinstance(parsed, Mapping):
            raise TransportError(f"peer wrote a non-object line: {line[:200]!r}")
        return parsed

    def close(self) -> None:
        # REFERENCE 第 41 条：terminate 之后必须 wait，否则残留进程。
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:  # pragma: no cover
                    pass

    def __enter__(self) -> "StdioTransport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["InMemoryTransport", "StdioTransport", "TransportError"]
