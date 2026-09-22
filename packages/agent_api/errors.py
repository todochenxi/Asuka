"""领域异常 → HTTP 语义（M18）。

**A-2：500 只能留给"真的没预料到的"。**

这是 API 层最常被写错的一处，而且后果比看上去大：

    · 不变量违规 → 500   → 调用方以为"服务端坏了" → 重试
    · 不变量违规 → 422   → 调用方知道"这个请求不合法" → 改请求

而不变量违规**重试一万次结果完全一样**。把 `InvariantViolation` 兜成 500，
等于用 HTTP 状态码骗调用方去做一件永远无效的事，
同时把真正的 500（bug）淹没在一堆假 500 里 —— 监控也就没了意义。

映射表：

| 领域异常 | HTTP | 理由 |
|---|---|---|
| `IllegalTransition` / `TerminalStateError` | 409 | 资源存在、状态明确，只是这个转换不允许 |
| `ConcurrentStateError` | 409 | 并发写冲突；必须重读再写 |
| `StaleWriteError` | 409 | 过期持有者的写回（E-22） |
| `LeaseRequired` | 409 | 没有有效 Lease 就想写 |
| `InvariantViolation` | 422 | 请求本身不合法 |
| `RetryBudgetExhausted` | 429 | 预算耗尽，别再来了 |
| 其它 `DomainError` | 422 | |
| 未预期异常 | 500 | **只有这一类是 bug** |

**A-9：过期 ≠ 已决定。**

这几个状态在存储里都是"非 PENDING"，但它们的 HTTP 语义完全不同：

| 审批状态 | HTTP | 为什么 |
|---|---|---|
| APPROVED / REJECTED | 409 `APPROVAL_ALREADY_DECIDED` | 有人决定过了；带上 `decided_by` 让调用方自己去看 |
| EXPIRED | **410** `APPROVAL_EXPIRED` | 没人决定过它 —— 是时间判死了。409 会骗调用方去"等结果"，而结果永远不会来 |
| CANCELLED | 409 `APPROVAL_CANCELLED` | 系统主动撤销，冲突但资源还在 |

把 EXPIRED 报成 `APPROVAL_ALREADY_DECIDED`（`decided_by="timeout"`）是最容易写错的一版：
它语法上完全正确，语义上是在告诉人"你/别人已经批过了"——而审计要回答的恰恰是"到底有没有人批过"。
"""
from __future__ import annotations

from typing import Any, Mapping

from packages.agent_domain.errors import (
    ConcurrentStateError,
    DomainError,
    IllegalTransition,
    InvariantViolation,
    LeaseRequired,
    RetryBudgetExhausted,
    StaleWriteError,
)


class ApiError(Exception):
    """已映射到 HTTP 语义的错误。handler 只抛这一种。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = dict(details or {})

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class BadRequest(ApiError):
    def __init__(self, message: str, *, code: str = "BAD_REQUEST", **details: Any) -> None:
        super().__init__(code, 400, message, details=details)


class NotFound(ApiError):
    def __init__(self, message: str, *, code: str = "NOT_FOUND", **details: Any) -> None:
        super().__init__(code, 404, message, details=details)


class Conflict(ApiError):
    def __init__(self, message: str, *, code: str = "CONFLICT", **details: Any) -> None:
        super().__init__(code, 409, message, details=details)


class Gone(ApiError):
    """410：资源存在过，但现在没了，而且**不是改改请求就能修好的**。

    跟 404 的区别是"曾经有过"；跟 409 的区别是"重试一万次结果一样"。
    过期审批是这里的典型。
    """

    def __init__(self, message: str, *, code: str = "GONE", **details: Any) -> None:
        super().__init__(code, 410, message, details=details)


class Unprocessable(ApiError):
    def __init__(self, message: str, *, code: str = "UNPROCESSABLE", **details: Any) -> None:
        super().__init__(code, 422, message, details=details)


class TooManyRequests(ApiError):
    def __init__(self, message: str, *, code: str = "BUDGET_EXHAUSTED", **details: Any) -> None:
        super().__init__(code, 429, message, details=details)


def map_domain_error(err: Exception) -> ApiError:
    """把领域异常翻成 HTTP 语义。未知异常**必须**落到 500 —— 那是 bug，不是翻译失败。"""
    if isinstance(err, ApiError):
        return err
    if isinstance(err, (IllegalTransition,)):
        return Conflict(str(err), code="ILLEGAL_TRANSITION")
    if isinstance(err, ConcurrentStateError):
        # 明确提示"重读再写"：否则调用方只会傻重试
        return Conflict(str(err), code="CONCURRENT_STATE", retry_hint="re-read then retry")
    if isinstance(err, StaleWriteError):
        return Conflict(str(err), code="STALE_WRITE")
    if isinstance(err, LeaseRequired):
        return Conflict(str(err), code="LEASE_REQUIRED")
    if isinstance(err, RetryBudgetExhausted):
        return TooManyRequests(str(err))
    if isinstance(err, InvariantViolation):
        return Unprocessable(str(err), code="INVARIANT_VIOLATION")
    if isinstance(err, DomainError):
        return Unprocessable(str(err), code="DOMAIN_ERROR")
    if isinstance(err, (KeyError, LookupError)):
        return NotFound(str(err))
    if isinstance(err, ValueError):
        return BadRequest(str(err))
    # ── 只有这里是真的没预料到 ──
    return ApiError("INTERNAL", 500, f"unexpected error: {err!r}")
