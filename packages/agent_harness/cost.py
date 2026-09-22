"""CostManager：预算是 Hardness 的一部分，不是可观测性的附属品。

基线 §23 把 CostManager 放在 Harness 里，理由是：

> 超预算必须能**阻止下一步**，而不只是事后出一张账单。

所以 CostManager 是 `before_action` 的一环：预算耗尽 → DENY。

**为什么预算耗尽是 DENY 而不是 REQUIRE_APPROVAL？**

> 审批放行的是"这个动作该不该做"，而预算是"这个 Run 还能不能继续"。
> 前者是权限问题，后者是资源边界。让人类审批来"续杯"会把成本纪律变成一张橡皮图章。
> 正确的做法是：预算不够就终止 Run（走 FAILED / HITL 升级），由人去开新 Run 并重新给预算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Mapping


@dataclass(frozen=True)
class Budget:
    max_cost: float = inf
    max_tokens: int | None = None
    max_steps: int | None = None


@dataclass
class CostManager:
    budget: Budget = field(default_factory=Budget)
    spent_cost: float = 0.0
    spent_tokens: int = 0
    steps: int = 0

    # ------------------------------------------------------------ 记账
    def charge(
        self,
        *,
        cost: float = 0.0,
        tokens: int = 0,
        steps: int = 0,
    ) -> None:
        if cost < 0 or tokens < 0 or steps < 0:
            raise ValueError("CostManager.charge() only accepts non-negative amounts")
        self.spent_cost += cost
        self.spent_tokens += tokens
        self.steps += steps

    # ------------------------------------------------------------ 判定
    @property
    def remaining_cost(self) -> float:
        return self.budget.max_cost - self.spent_cost

    @property
    def exceeded(self) -> bool:
        if self.spent_cost > self.budget.max_cost:
            return True
        if self.budget.max_tokens is not None and self.spent_tokens > self.budget.max_tokens:
            return True
        if self.budget.max_steps is not None and self.steps > self.budget.max_steps:
            return True
        return False

    def reason(self) -> str:
        if self.spent_cost > self.budget.max_cost:
            return (
                f"cost budget exhausted: spent {self.spent_cost} "
                f"> limit {self.budget.max_cost}"
            )
        if self.budget.max_tokens is not None and self.spent_tokens > self.budget.max_tokens:
            return f"token budget exhausted: {self.spent_tokens} > {self.budget.max_tokens}"
        if self.budget.max_steps is not None and self.steps > self.budget.max_steps:
            return f"step budget exhausted: {self.steps} > {self.budget.max_steps}"
        return ""

    def snapshot(self) -> Mapping[str, float | int]:
        return {
            "spent_cost": self.spent_cost,
            "spent_tokens": self.spent_tokens,
            "steps": self.steps,
            "remaining_cost": self.remaining_cost,
        }
