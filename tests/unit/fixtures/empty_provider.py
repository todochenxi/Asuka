"""给覆盖校验用的反例 provider（M23）。

它们不是"Fake 执行器"，是**装配期就该被拒的坏输入**：
空表、裸 dict 工具表。组合根必须在启动前把它们挡下来，
而不是等第一个 Task 来了才炸。
"""
from __future__ import annotations

from typing import Any


def build(_config: Any = None) -> dict[str, Any]:
    """返回一个空执行器表 —— 什么都不会的 worker。"""
    return {}


def dict_tools(_config: Any = None) -> dict[str, Any]:
    """返回一个裸 dict 当工具表 —— 能"跑"，但绕过了 ToolRuntime（T-2 失效）。"""
    return {"echo": lambda args: {"echo": args.get("text", "")}}


__all__ = ["build", "dict_tools"]
