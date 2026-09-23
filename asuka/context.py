"""Context 装配：把检到的片装成**受预算约束**的、真正喂给模型的那一份。

--------------------------------------------------------------------------
为什么这一步不能省：`top_k` 是**条数**，不是**窗口占用**

`kb.search(limit=10)` 说的是"给我 10 片"。10 片是多少 token？不知道。
而模型的窗口是按 token 算的 —— 10 片长文档可以轻松超过 8192。
**一个按条数控制的检索器会静默地把请求撑爆**，而失败发生在模型那一侧
（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧。评测跑得好好的，线上全崩。

所以装配这一步要回答两个问题：

    装得下吗？          → C-3：Token Budget 是**硬约束**，装不下就丢
    丢了哪些、为什么？  → C-4：**静默截断是 bug**，每条被丢的都要留 `DroppedItem`

--------------------------------------------------------------------------
复用 `packages/agent_context`，不自己写一套

    RetrievalPipeline.to_context_items()   C-1：Chunk（Knowledge）→ ContextItem（Context）
    budget.allocate()                      C-3 / C-4
    tokens.HeuristicTokenizer              Tokenizer 端口

⚠️ **不用** `ContextAssembler` 整体，理由要说清（不说的话读的人以为漏了）：
它要 `run_id`，并把 `ContextSnapshot` 存进 `ContextSnapshotStore`（C-2：一次调用一份快照）。
Asuka 的审计凭证是 `asuka.trace`；而且评测里的 "run" 是 `task_id × 采样序号`，
**不是** AgentOS 的 `AgentRun`。把两套 run 语义缝在一起，比各管各的更危险。

--------------------------------------------------------------------------
⭐ 为什么要把检索名次写进 `priority`

`allocate()` 的取舍顺序是：

    sorted(items, key=(not pinned, -priority, key))

知识片既不是 pinned、`knowledge_chunk()` 给的 `priority` 又是**同一个常数**，
于是"装不下先丢谁"退化成**按 chunk_id 的字母序** —— 而 RAG 里该丢谁
必须由**相关性**决定。字母序丢掉第一名、留下最后一名，是**静默**的：
分数照出，只是低了一点。

`priority` 的语义就是"越大越重要；只在取舍时用"，所以这里把检索名次写进去 ——
**用**内核给的旋钮，不是绕过它。（`allocate` 返回的 `kept` 也就因此是按相关性排的。）

--------------------------------------------------------------------------
⚠️ `chars_per_token` 是**语料的属性**，必须显式声明并印出来

同一段文本，`chars_per_token=4`（英文经验值）和 `3`（中文保守）差 **33%**。
两处各写一个数字 ⇒ 同一个 chunk 在语料清单里和在装配时算出的 token 数不一样，
而**两边都不会报错**。所以这里只认一个常量（`textutil.CHARS_PER_TOKEN`），
并且它**进报告** —— "这个分母是谁划的"必须能被读出来。

内核 `HeuristicTokenizer` 默认 **3**（保守：低估 ⇒ 超窗口 ⇒ 调用彻底失败，
比高估危险得多）。Asuka 的语料是英文技术文档，所以**显式**传 4。
两边不一样**不是 bug** —— 前提是**都说出来了**。
"""
from __future__ import annotations

import dataclasses

from packages.agent_context.budget import DroppedItem, TokenBudget, allocate
from packages.agent_context.items import ContextItem
from packages.agent_context.retrieval import RetrievalPipeline, RetrievalResult
from packages.agent_context.tokens import HeuristicTokenizer

from .textutil import CHARS_PER_TOKEN

#: 一次模型调用的上下文窗口（token）。默认取一个常见的 8k。
#:
#: ⚠️ 这是**输入**不是常量事实 —— 换个模型就换个数。所以它进报告：
#: "丢了 3 片"这个事实，脱离"预算多少"无法解释。
DEFAULT_CONTEXT_BUDGET = 8192

#: 给模型输出留的位置。
#:
#: 不留的话会出现"上下文刚好塞满窗口，模型一个字都吐不出来"——
#: 那在很多服务端是 `CONTEXT_LENGTH_EXCEEDED`，不是"输出被截断"。
DEFAULT_RESERVED_FOR_OUTPUT = 1024

#: 名次 → priority 的基数。只要比 `top_k` 大得多，具体值无所谓。
_RANK_BASE = 1_000_000


@dataclasses.dataclass(frozen=True)
class AssembledContext:
    """装配结果：**真正喂给模型的那一份** + 被预算丢掉的 + 为什么。

    ⚠️ `items` 是 C-3 之后的集合。它和 `RetrievalResult.kept` **不是一回事** ——
    后者只过了 C-9（权限）和 C-10（citation），没过预算。
    引用判据里的 `available` 必须用**这里**的 id：被预算丢掉的片
    模型**没看见**，引用了它就是编造。
    """

    items: tuple[ContextItem, ...] = ()
    dropped: tuple[DroppedItem, ...] = ()
    total_tokens: int = 0
    budget_total: int = 0
    budget_available: int = 0
    chars_per_token: int = CHARS_PER_TOKEN

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        """喂进去的片，按**相关性**排（`allocate` 按 priority 降序返回）。"""
        return tuple(i.key for i in self.items)

    @property
    def dropped_ids(self) -> tuple[str, ...]:
        return tuple(d.key for d in self.dropped)

    @property
    def dropped_tokens(self) -> int:
        return sum(d.tokens for d in self.dropped)

    @property
    def dropped_reasons(self) -> tuple[tuple[str, int, str], ...]:
        """`(chunk_id, tokens, 原因)` —— C-4 要求每条被丢的都能被事后回答。"""
        return tuple((d.key, d.tokens, d.reason) for d in self.dropped)


def assemble(
    pipeline: RetrievalPipeline,
    result: RetrievalResult,
    *,
    budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> AssembledContext:
    """`RetrievalResult.kept` → 受预算约束的 `ContextItem` 元组。

    取片顺序：**先按检索名次给 priority，再交给 `allocate` 取舍**（见模块 docstring）。
    """
    items = pipeline.to_context_items(result)
    # 名次 0 最重要。同分片在 `result.kept` 里已经按 `(-score, chunk_id)` 排过，
    # 所以这里按**位置**发 priority 与按 score 发是等价的，且不依赖浮点比较。
    ranked = tuple(
        dataclasses.replace(item, priority=_RANK_BASE - rank)
        for rank, item in enumerate(items)
    )

    tb = TokenBudget(total=budget, reserved_for_output=reserved_for_output)
    plan = allocate(ranked, tb, tokenizer=HeuristicTokenizer(chars_per_token))

    return AssembledContext(
        items=plan.kept,
        dropped=plan.dropped,
        total_tokens=plan.total_tokens,
        budget_total=tb.total,
        budget_available=tb.available,
        chars_per_token=chars_per_token,
    )
