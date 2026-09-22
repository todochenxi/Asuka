"""Token Budget 与取舍（M17，基线 §22）。

§22 的原则：

> Context 不是简单字符串拼接，而是受优先级、相关性和 Token Budget 约束的资源分配问题。

C-3  Token Budget 是**硬约束**：装不下就丢，不允许超。
C-4  **静默截断是 bug**：被丢掉的每一条都要留下 `DroppedItem` + 原因。
     模型看不到某些内容这件事，必须能被事后回答 ——
     "它当时没看见工具列表"和"它看见了但没用"是完全不同的故障。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .items import ContextItem
from .tokens import HeuristicTokenizer, Tokenizer


class ContextBudgetError(Exception):
    """**装不下 pinned 内容** —— 这不是降级场景，是配置错误。

    系统指令或工具契约都放不进上下文窗口，说明这个"模型 + 预算"组合
    根本不可用。继续跑下去只会得到一个行为完全不同的 Agent，
    而且**不会报错**。所以宁可在这里炸掉。
    """

    def __init__(self, message: str, *, needed: int, available: int) -> None:
        super().__init__(message)
        self.needed = needed
        self.available = available


@dataclass(frozen=True)
class TokenBudget:
    total: int
    reserved_for_output: int = 0
    """给模型输出留的位置。

    不留的话会出现"上下文刚好塞满窗口，模型一个字都吐不出来"——
    而这在很多服务端是 `CONTEXT_LENGTH_EXCEEDED`，不是"输出被截断"。
    """

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError("TokenBudget.total must be > 0")
        if self.reserved_for_output < 0:
            raise ValueError("reserved_for_output must be >= 0")
        if self.reserved_for_output >= self.total:
            raise ValueError("reserved_for_output must be < total")

    @property
    def available(self) -> int:
        return self.total - self.reserved_for_output


@dataclass(frozen=True)
class DroppedItem:
    """C-4：被丢掉的内容 + 原因。它是 ContextSnapshot 的一部分。"""

    key: str
    source: str
    tokens: int
    reason: str


@dataclass(frozen=True)
class ContextPlan:
    kept: tuple[ContextItem, ...] = ()
    dropped: tuple[DroppedItem, ...] = ()
    total_tokens: int = 0

    @property
    def dropped_tokens(self) -> int:
        return sum(d.tokens for d in self.dropped)


def allocate(
    items: Sequence[ContextItem],
    budget: TokenBudget,
    *,
    tokenizer: Tokenizer | None = None,
) -> ContextPlan:
    """在预算内挑出要保留的内容。

    **确定性是硬要求**（C-4 的另一半）：同样的输入必须得到同样的输出，
    因为 Snapshot 是审计记录 —— 两次重建得到不同结果的话，
    "模型当时看到了什么"这个问题就没有答案了。
    所以排序键里带上了 `key`，杜绝依赖 dict / 集合的遍历顺序。

    取舍顺序：**先放 pinned，再按 priority 降序**。
    """
    tok = tokenizer or HeuristicTokenizer()
    available = budget.available

    scored = [(item, item.tokens(tok)) for item in items]

    # pinned 优先，然后 priority 降序，最后按 key 保证确定性
    ordered = sorted(
        scored,
        key=lambda pair: (not pair[0].pinned, -pair[0].priority, pair[0].key),
    )

    kept: list[ContextItem] = []
    dropped: list[DroppedItem] = []
    used = 0

    for item, cost in ordered:
        if used + cost <= available:
            kept.append(item)
            used += cost
            continue
        if item.pinned:
            # C-3 的硬边界：pinned 装不下 → 直接失败，不静默降级
            raise ContextBudgetError(
                f"pinned context item {item.qualified_key!r} ({cost} tokens) "
                f"does not fit: {used}/{available} used, budget total={budget.total}",
                needed=used + cost,
                available=available,
            )
        dropped.append(
            DroppedItem(
                key=item.key,
                source=item.source.value,
                tokens=cost,
                reason=f"token budget exhausted ({used}/{available})",
            )
        )

    return ContextPlan(kept=tuple(kept), dropped=tuple(dropped), total_tokens=used)
