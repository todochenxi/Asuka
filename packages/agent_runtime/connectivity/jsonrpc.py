"""JSON-RPC 2.0 —— MCP 与 A2A 共用的那一层（基线 §24 / §26 / M4）。

### 为什么先有这一层

MCP 与 A2A 是两套协议，但它们底下**是同一个东西**：JSON-RPC 2.0。
分开各写一份请求/响应/错误处理，就等于"怎么发一次 RPC"有两个定义 ——
而两份实现的分岔点，恰好是错误路径（对端返回 error 时谁抛、抛什么）。
所以这一层只做一件事：把一次 RPC 发出去，并把**协议层的**三种病挡在门口。

### 三条不变量

| # | 内容 |
|---|---|
| J-1 | 对端必须说 `jsonrpc: "2.0"`。一个 1.0 / 缺字段的响应不许被当成 2.0 读下去 |
| J-2 | 响应必须是 `result` 或 `error` **恰好其一**。两者都没有、或 `error` 形状不对，都是协议错误 |
| J-3 | `id` 必须与请求的 `id` **相等**。不等说明响应串了线（并发 / 重排的传输），此时把别人的结果当成自己的，比报错危险得多 |

⚠️ 这一层**不认识** MCP，也不认识 A2A。它只知道 `method` / `params` / `id`
与一个 `Transport`（`transport.py`）。所以换传输（stdio / HTTP / 内存）
不需要动它，也不需要动上面那两层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any, Iterator, Mapping, Protocol

JSONRPC_VERSION = "2.0"


class JsonRpcProtocolError(Exception):
    """对端说的不是 JSON-RPC 2.0（J-1 / J-2 / J-3）。

    刻意**不**是 `JsonRpcError`：那个是"对端**正确地**告诉我一次调用失败了"，
    这个是"对端根本没在说这门协议"。两者的处置完全不同 ——
    前者是业务结果，后者说明对面连的是别的东西（或版本对不上）。
    """


class JsonRpcError(Exception):
    """对端返回的 `error` 对象（JSON-RPC 2.0 §5.1）。

    `code` / `message` 是协议规定的，`data` 可选。
    它是**业务失败**：链路是好的，是对端说这次调用不成功。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out

    @classmethod
    def from_wire(cls, payload: Any) -> "JsonRpcError":
        # J-2：形状不对的 error 不许被"尽量理解" —— 一个没有 code 的 error
        # 会让上层拿到 `None` 去当 code，排障时指向错误的地方。
        if (
            not isinstance(payload, Mapping)
            or not isinstance(payload.get("code"), int)
            or isinstance(payload.get("code"), bool)
            or not isinstance(payload.get("message"), str)
        ):
            raise JsonRpcProtocolError(
                f"malformed JSON-RPC error object (needs integer code + string "
                f"message): {payload!r}"
            )
        return cls(payload["code"], payload["message"], payload.get("data"))


class Transport(Protocol):
    """一次 RPC 的传输。

    `request` 发一条**请求**并等响应；`send` 发一条**通知**（不等响应）。

    ⚠️ 两者必须分开：通知没有响应，若也走 `request`，
    stdio 那类"发完就读一行"的传输会**永久阻塞**在 readline 上
    （服务端按协议不会回答通知）。这不是性能问题，是死锁。
    """

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def send(self, payload: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class JsonRpcRequest:
    """一条请求。`id is None` 表示**通知**（notification）：不等响应。"""

    method: str
    params: Any = None
    id: Any = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": self.method}
        if self.params is not None:
            wire["params"] = self.params
        if self.id is not None:
            wire["id"] = self.id
        return wire


@dataclass
class JsonRpcClient:
    """把请求发出去，把响应拆成"结果"或"异常"。

    `id` 用单调递增的整数（JSON-RPC 允许 string / number / null）——
    递增的整数让 J-3 的比对有意义：串线的响应几乎不可能刚好撞上同一个 id。
    """

    transport: Transport
    _ids: Iterator[int] = field(default_factory=lambda: count(1), repr=False)

    def call(self, method: str, params: Any = None) -> Any:
        """发一条**请求**并等结果。对端报错 → 抛 `JsonRpcError`。"""
        request = JsonRpcRequest(method=method, params=params, id=next(self._ids))
        payload = self.transport.request(request.to_wire())
        return self._unwrap(payload, expected_id=request.id, method=method)

    def notify(self, method: str, params: Any = None) -> None:
        """发一条**通知**（无 id，不等响应）—— 走 `send`，绝不 `readline`。"""
        self.transport.send(JsonRpcRequest(method=method, params=params).to_wire())

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _unwrap(payload: Any, *, expected_id: Any, method: str) -> Any:
        if not isinstance(payload, Mapping):
            raise JsonRpcProtocolError(
                f"JSON-RPC response for {method!r} is not an object: {payload!r}"
            )
        # J-1
        if payload.get("jsonrpc") != JSONRPC_VERSION:
            raise JsonRpcProtocolError(
                f"response for {method!r} is not JSON-RPC 2.0 "
                f"(jsonrpc={payload.get('jsonrpc')!r})"
            )
        # J-3
        if payload.get("id") != expected_id:
            raise JsonRpcProtocolError(
                f"response id {payload.get('id')!r} does not match request id "
                f"{expected_id!r} for {method!r}; responses crossed over the "
                f"transport and taking this one would attribute another call's "
                f"result to this one"
            )
        has_result = "result" in payload
        has_error = "error" in payload
        # J-2
        if has_result == has_error:
            raise JsonRpcProtocolError(
                f"response for {method!r} must carry exactly one of result/error; "
                f"got result={has_result} error={has_error}"
            )
        if has_error:
            raise JsonRpcError.from_wire(payload["error"])
        return payload["result"]


__all__ = [
    "JSONRPC_VERSION",
    "JsonRpcClient",
    "JsonRpcError",
    "JsonRpcProtocolError",
    "JsonRpcRequest",
    "Transport",
]
