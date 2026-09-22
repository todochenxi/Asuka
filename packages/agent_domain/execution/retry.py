"""Retry 判据：Kernel Failure Class + RetryPolicy。

Retry 回答的是"失败以后要不要再试一次"，与以下两件事严格区分：

    Recovery         出了故障以后，如何把 Execution 救回来（Lease 过期 / Worker 崩溃）
    Agent Replanning 当前策略失败以后，Agent 下一步该怎么办（换 Tool / 换路径）

Kernel 的 Failure Class ≠ §30 的 Evolution Failure Taxonomy：
前者决定"这次要不要重试"，后者决定"这个 Agent 该怎么改"。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class FailureClass(str, Enum):
    TRANSIENT = "transient"                 # Timeout / RateLimit / 5xx / Network
    RESOURCE = "resource"                   # Worker OOM / 配额耗尽
    LEASE_EXPIRED = "lease_expired"         # Lease 过期 / Worker 失联
    PERMANENT = "permanent"                 # 参数非法 / 权限拒绝 / Tool 不存在
    EXTERNAL_UNKNOWN = "external_unknown"   # 外部副作用结果未知
    POLICY_DENIED = "policy_denied"         # Guardrail / Policy 拒绝


#: 可重试的失败类别。
#: EXTERNAL_UNKNOWN **不可**盲重试 —— 只能靠 Idempotency Key 回查外部真实状态。
RETRYABLE_FAILURE_CLASSES = frozenset(
    {FailureClass.TRANSIENT, FailureClass.RESOURCE, FailureClass.LEASE_EXPIRED}
)


@dataclass(frozen=True)
class RetryPolicy:
    """定义在 Task，执行在 Execution，计数在 Attempt。"""

    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    retry_budget: int | None = None         # Run 级总重试次数上限（防重试风暴）

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be positive")
        if self.retry_budget is not None and self.retry_budget < 0:
            raise ValueError("retry_budget must be >= 0")

    def is_retryable(self, failure_class: FailureClass) -> bool:
        return failure_class in RETRYABLE_FAILURE_CLASSES

    def should_retry(
        self,
        *,
        attempt_no: int,
        failure_class: FailureClass,
        retries_used_in_run: int = 0,
    ) -> bool:
        if not self.is_retryable(failure_class):
            return False
        if attempt_no >= self.max_attempts:
            return False
        if self.retry_budget is not None and retries_used_in_run >= self.retry_budget:
            return False
        return True

    def next_delay(self, attempt_no: int, *, jitter: bool = True) -> float:
        """exponential backoff + jitter。"""
        delay = min(
            self.backoff_base_seconds * (2 ** max(0, attempt_no - 1)),
            self.backoff_max_seconds,
        )
        if jitter:
            delay = random.uniform(0.0, delay)
        return round(delay, 3)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    failure_class: FailureClass
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("ErrorInfo.code is required")
        if not isinstance(self.failure_class, FailureClass):
            raise ValueError("ErrorInfo.failure_class must be a FailureClass")
        object.__setattr__(self, "details", dict(self.details))

    @property
    def retryable(self) -> bool:
        return self.failure_class in RETRYABLE_FAILURE_CLASSES
