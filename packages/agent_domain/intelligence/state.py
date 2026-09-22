"""Agent State：Agent 当前认为世界是什么样。

关键区分：

    Observation  事实（append-only，不可变）
    State        系统解释后的认知状态（可变，但只能经 Reducer 变更，带 version）

X-7   Observation 必须经 StateReducer 才能更新 State
X-10  同一 run 的 State 写入必须串行化（version 校验 + 冲突重放）
I-3   State 只能通过 apply(observation, reducer) 变更

不要把 Kernel 的执行状态全塞进 AgentState —— State 是"认知"，不是"执行台账"。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

from ..errors import ConcurrentStateError, InvariantViolation
from .goal import Goal
from .observation import Observation
from .plan import Plan

# 允许在 apply 之外直接写的内部字段（版本号 / 锁标记）
_INTERNAL_FIELDS = frozenset({"_sealed", "version", "_reducing"})


@dataclass
class State:
    run_id: str
    goal: Goal
    current_plan: Plan | None = None
    active_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    runtime_status: str = "RUNNING"
    version: int = 1
    _sealed: bool = field(default=False, repr=False, compare=False)
    _reducing: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("State.run_id is required")
        object.__setattr__(self, "_sealed", True)

    # ------------------------------------------------------------ I-3 保护
    def __setattr__(self, name: str, value: Any) -> None:
        if (
            getattr(self, "_sealed", False)
            and name not in _INTERNAL_FIELDS
            and not getattr(self, "_reducing", False)
        ):
            raise InvariantViolation(
                "I-3: State can only be changed via apply(observation, reducer); "
                f"direct assignment to '{name}' is forbidden"
            )
        object.__setattr__(self, name, value)

    @contextmanager
    def reducing(self) -> Iterator[None]:
        """Reducer 的写入窗口。

        少这一层，Reducer 就写不了 `current_plan` 这类**不可变**字段 ——
        只能偷偷改 list / dict（可变容器绕过了 `__setattr__`），
        那等于 I-3 只对一半字段生效。窗口只由 `apply()` 打开，
        业务代码依然不能直写 State。
        """
        prev = getattr(self, "_reducing", False)
        object.__setattr__(self, "_reducing", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_reducing", prev)

    # ------------------------------------------------------------ 唯一入口
    def apply(
        self,
        obs: Observation,
        reducer: "StateReducer",
        expected_version: int | None = None,
    ) -> None:
        """唯一的状态更新入口。

        并发语义（X-10）：
            - 同一 run_id 的 apply 必须串行化（per-run 有序队列）
            - expected_version 不为空时必须等于当前 version，否则抛 ConcurrentStateError
            - 冲突时调用方重读最新 State 并重放 Reducer，不允许覆盖
        """
        if obs.run_id != self.run_id:
            raise InvariantViolation(
                f"X-10: observation from run {obs.run_id} cannot be applied to run {self.run_id}"
            )
        if expected_version is not None and expected_version != self.version:
            raise ConcurrentStateError(
                f"X-10: state version mismatch: expected={expected_version}, actual={self.version}"
            )

        self.observations.append(obs)      # 事实：append-only
        with self.reducing():
            reducer.reduce(self, obs)      # 解释：写入 variables / current_plan / ...
        self.version += 1

    # ------------------------------------------------------------ 只读视图
    def observation_stream(self) -> Iterator[Observation]:
        yield from self.observations

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "version": self.version,
            "runtime_status": self.runtime_status,
            "active_tasks": tuple(self.active_tasks),
            "completed_tasks": tuple(self.completed_tasks),
            "variables": dict(self.variables),
            "observation_count": len(self.observations),
        }


class StateReducer(Protocol):
    """Observation（事实）→ State（解释）的唯一解释器。

    少了这一层，Observation 和 State 会重新混起来。
    """

    def reduce(self, state: State, obs: Observation) -> None: ...


class PassthroughReducer:
    """默认 Reducer：只把 observation 的关键信息写进 variables，不做业务解释。"""

    def reduce(self, state: State, obs: Observation) -> None:
        state.variables[f"obs:{obs.observation_id}"] = obs.summary
