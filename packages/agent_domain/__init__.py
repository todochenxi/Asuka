"""AgentOS · agent_domain（M15 阶段 1）

纯 Python，零基础设施依赖：不 import fastapi / redis / kafka / sqlalchemy。

目录：
    intelligence/   Goal / State / Decision / Action / Observation / Plan
    execution/      Task / Execution / Attempt / Lease / Checkpoint / StateMachine / Aggregate
    events/         Event（与 Observation 严格区分）

原则：不变量写在对象内部，违反即抛异常；状态只能经 StateMachine.transition() 变更。
"""
from __future__ import annotations

from . import business, errors, events, execution, ids, intelligence  # noqa: F401

__version__ = "0.1.0"

__all__ = ["business", "errors", "events", "execution", "ids", "intelligence"]
