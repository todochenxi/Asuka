"""Adapters：把 ports.py 的接口落到具体基础设施上。

    postgres.py   PostgreSQL（阶段 5）—— 当前持久状态 / Truth
    redis.py      Redis（阶段 6）—— Lease 的快速面、取消信号、幂等结果
    kafka.py      Kafka（阶段 7）—— Outbox → Durable Event Log

规则：**Adapter 只实现端口，不定义业务语义**。
业务语义在 Domain（不变量）和 Kernel（编排）里，Adapter 换掉不影响它们。
"""
from __future__ import annotations

from .postgres import (  # noqa: F401
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresIdempotencyStore,
    PostgresOutboxStore,
)

__all__ = [
    "PostgresAttemptRepository",
    "PostgresExecutionRepository",
    "PostgresIdempotencyStore",
    "PostgresOutboxStore",
]
