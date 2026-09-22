"""ID 生成器。

约定：所有 ID 是带前缀的字符串（`run_xxx` / `task_xxx` / `exec_xxx` / ...），
方便在日志、Trace、Kafka 里一眼看出对象类型。

不变量相关：
- Idempotency Key = execution_id（跨 Attempt 稳定），见 `idempotency_key_for`。
"""
from __future__ import annotations

import uuid
from typing import Final

_PREFIX_SEPARATOR: Final[str] = "_"


def new_id(prefix: str) -> str:
    """生成一个带前缀的随机 ID。"""
    if not prefix or _PREFIX_SEPARATOR in prefix:
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}{_PREFIX_SEPARATOR}{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    return new_id("run")


def new_step_id() -> str:
    return new_id("step")


def new_task_id() -> str:
    return new_id("task")


def new_execution_id() -> str:
    return new_id("exec")


def new_attempt_id() -> str:
    return new_id("att")


def new_lease_id() -> str:
    return new_id("lease")


def new_checkpoint_id() -> str:
    return new_id("ckpt")


def new_event_id() -> str:
    return new_id("evt")


def new_observation_id() -> str:
    return new_id("obs")


def idempotency_key_for(execution_id: str) -> str:
    """E-21：幂等 Key 就是 execution_id 本身。

    它必须在同一 Execution 的所有 Attempt 间保持不变 —— 每次重试换新 key，
    重试就完全失去"防重复副作用"的意义。
    """
    if not execution_id.startswith("exec" + _PREFIX_SEPARATOR):
        raise ValueError(f"execution_id must start with 'exec_': {execution_id!r}")
    return execution_id
