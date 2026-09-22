"""领域异常。

原则：不变量写在对象内部，违反即抛异常，而不是靠调用方自觉。
"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域异常的基类。"""


class InvariantViolation(DomainError):
    """违反领域不变量。"""


class IllegalTransition(DomainError):
    """非法状态转换。"""


class TerminalStateError(IllegalTransition):
    """终态不可逆（E-2）。"""


class ConcurrentStateError(DomainError):
    """语义层面的并发写冲突（X-10）。

    调用方应重读最新 State 并重放 Reducer，不允许覆盖写入。
    """


class StaleWriteError(DomainError):
    """过期持有者的写回（E-22）。

    Worker 的 Lease 已过期（fencing_token 落后），它的写入必须被拒绝，
    否则会污染已经被新 Worker 接管的 Execution。
    """


class LeaseRequired(DomainError):
    """操作需要有效 Lease，但当前没有（E-7）。"""


class RetryBudgetExhausted(DomainError):
    """重试次数或重试预算耗尽。"""
