"""Idempotency：防止重复副作用。

核心原则：

    Lease / Lock        防止并发
    Idempotency Key     防止重复副作用
    Fencing Token       防止过期持有者回写

Key 作用域 = execution_id，跨 Attempt 稳定（E-21）。

最危险的是 EXTERNAL_UNKNOWN：外部调用了但结果未知（Worker 崩了）。
这种情况**不能盲重试**，只能先用同一个 key 回查外部系统的真实状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import FailureClass

from .ports import IdempotencyStore

UNKNOWN = "__unknown__"


@dataclass
class IdempotencyGuard:
    store: IdempotencyStore

    def execute(
        self,
        key: str,
        fn: Callable[[], Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], bool]:
        """用同一个 key 执行外部副作用。

        返回 `(result, executed_now)`：
            executed_now=True   本次真的执行了
            executed_now=False  命中幂等记录，直接返回首次结果（没有重复副作用）
        """
        existing = self.store.get(key)
        if existing is not None:
            return existing, False
        try:
            result = dict(fn())
        except Exception:
            # 结果未知：留下标记，后续不许盲重试
            self.store.put(key, {"status": UNKNOWN})
            raise
        self.store.put(key, result)
        return result, True

    def resolve_unknown(
        self,
        key: str,
        probe: Callable[[], Mapping[str, Any] | None],
    ) -> Mapping[str, Any] | None:
        """EXTERNAL_UNKNOWN 的回查：查不到就返回 None（仍不可盲重试）。"""
        existing = self.store.get(key)
        if existing is None:
            return None
        if existing.get("status") != UNKNOWN:
            return existing
        probed = probe()
        if probed is not None:
            self.store.put(key, dict(probed))
        return probed

    @staticmethod
    def assert_not_blind_retry(failure_class: FailureClass) -> None:
        if failure_class is FailureClass.EXTERNAL_UNKNOWN:
            raise InvariantViolation(
                "EXTERNAL_UNKNOWN must not be retried blindly; "
                "resolve the real external state via idempotency key first"
            )
