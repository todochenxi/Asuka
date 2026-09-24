"""Context 的原子单位（M17，基线 §21 / §22）。

C-1  Context ≠ Memory ≠ Knowledge ≠ Artifact —— 四者是不同类型的对象，
     不是同一个东西的四种叫法。这里用 `ContextSource` 把来源钉在类型上：
     一条进 Context 的内容**必须**声明自己来自哪一层，
     否则事后无法回答"这句话是谁说的"。

C-6  Artifact 只以 Reference 进 Context：大对象（PDF / 表格 / 图片）不内联。
     内联的后果是 Context 被一个 PDF 撑爆，而它本来只是一个引用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .tokens import HeuristicTokenizer, Tokenizer


class ContextSource(str, Enum):
    """§22 ContextEngine 的各个 Assembler 对应的来源。"""

    SYSTEM = "system"
    CONVERSATION = "conversation"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    TOOL = "tool"
    SKILL = "skill"
    RUNTIME_STATE = "runtime_state"
    METADATA = "metadata"


#: C-12：Context 的**输出顺序由语义决定**，不由 priority 决定。
#: 系统指令必须永远在最前 —— 按 priority 排序的话，一条 priority=0 的 system
#: 会被压到一堆 priority=9 的检索结果中间，模型的行为就整体变了。
#: priority 只在"装不下、该丢谁"时才起作用（见 budget.py）。
SOURCE_ORDER: tuple[ContextSource, ...] = (
    ContextSource.SYSTEM,
    ContextSource.CONVERSATION,
    ContextSource.MEMORY,
    ContextSource.KNOWLEDGE,
    ContextSource.RUNTIME_STATE,
    ContextSource.SKILL,
    ContextSource.TOOL,
    ContextSource.METADATA,
)


@dataclass(frozen=True)
class ContextItem:
    """进入 Context 的一条内容。

    `pinned=True` 表示**永不被丢弃**（系统指令、工具契约）。
    它存在的理由见 budget.py：系统指令被丢掉不是"降级"，
    是换了一个 Agent —— 那种情况下宁可直接失败。
    """

    source: ContextSource
    key: str
    text: str
    priority: int = 0                 # 越大越重要；只在取舍时用
    reference: str = ""               # C-6：Artifact / 原文的引用，不是内容本身
    pinned: bool = False
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ContextItem.key is required")
        # C-10：Knowledge 必须带 Citation。没有引用的检索结果进了 Context，
        # 模型无法标注来源，事后也无法回答"这句话是从哪份文档来的"。
        if self.source is ContextSource.KNOWLEDGE and not self.reference:
            raise ValueError("C-10: knowledge items must carry a citation (reference)")

    def tokens(self, tokenizer: Tokenizer | None = None) -> int:
        tok = tokenizer or HeuristicTokenizer()
        return tok.count(self.text)

    @property
    def qualified_key(self) -> str:
        return f"{self.source.value}:{self.key}"


def system(text: str, *, key: str = "system", priority: int = 100) -> ContextItem:
    """系统指令：默认 pinned —— 它被丢掉等于换了个 Agent。"""
    return ContextItem(
        source=ContextSource.SYSTEM, key=key, text=text, priority=priority, pinned=True
    )


def message(role: str, text: str, *, index: int = 0) -> ContextItem:
    return ContextItem(
        source=ContextSource.CONVERSATION,
        key=f"{role}#{index}",
        text=text,
        priority=50,
        attributes={"role": role},
    )


def tool_contract(name: str, text: str) -> ContextItem:
    """工具契约：模型必须看见有哪些工具，否则它无从发起 Tool Call。"""
    return ContextItem(
        source=ContextSource.TOOL,
        key=name,
        text=text,
        priority=90,
        pinned=True,          # 工具列表被截断 = 模型只能用一半的工具
    )


def knowledge_chunk(
    chunk_id: str,
    text: str,
    *,
    citation: str,
    score: float = 0.0,
    attributes: Mapping[str, Any] | None = None,
) -> ContextItem:
    attrs: dict[str, Any] = {"score": score}
    if attributes:
        attrs.update(attributes)
    return ContextItem(
        source=ContextSource.KNOWLEDGE,
        key=chunk_id,
        text=text,
        priority=30,
        reference=citation,     # C-10 的落点
        attributes=attrs,
    )
