"""child_run_consumer 进程（M30）。

把 `child_run.completed` / `.failed` / `.cancelled` 消费回来，
交回父 Run —— "派得出去、认得回来"里"认得回来"的那一半。

判定逻辑在 `packages/agent_runtime/child_wake.py::ChildRunWaker`，
本包只负责"什么时候去问、以及什么时候可以说'消费完了'"。
"""
from .app import (
    CHILD_RUN_EVENTS,
    ChildRunConsumerApp,
    ChildRunConsumerConfig,
)

__all__ = ["CHILD_RUN_EVENTS", "ChildRunConsumerApp", "ChildRunConsumerConfig"]
