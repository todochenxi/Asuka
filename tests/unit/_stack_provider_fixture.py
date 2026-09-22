"""一个**形状正确但不收 `child_registry`** 的 provider（M26）。

为什么要有这个文件而不是在测试里现搓一个 lambda：
`load_stack_factory` 走的是 `importlib.import_module` + `getattr`，
和真实部署是同一条装载路径。测试必须走这条路径 ——
绕过它去直接调 `load_stack_factory(provider)` 的话，
被替身替掉的恰好是"装载"这一层（PR-23）。

它代表的是 **M26 之前所有 provider 的形状**：收 `config`，不收登记处。
"""
from __future__ import annotations

from typing import Any

SEEN: list[Any] = []


def build_stack_factory(config: Any) -> Any:
    """不收 `child_registry` 的 provider —— 组合根必须**拒绝**它。"""

    def make_stack(agent_id: str, approvals: Any) -> Any:
        raise AssertionError("this fixture never builds a real stack")

    return make_stack


def build_stack_factory_with_registry(config: Any, *, child_registry: Any = None) -> Any:
    """对照：收下登记处的 provider —— 组合根必须**放行**它。"""
    SEEN.append(child_registry)

    def make_stack(agent_id: str, approvals: Any) -> Any:
        raise AssertionError("this fixture never builds a real stack")

    return make_stack


__all__ = ["SEEN", "build_stack_factory", "build_stack_factory_with_registry"]
