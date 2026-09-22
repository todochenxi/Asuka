"""Retrieval（M17，基线 §21 的 RAG 链）。

```text
Query → Query Rewrite → Permission Filter → Vector + BM25
      → Fusion → Rerank → Top-K → Citation → Context
```

C-9  **Retrieval 必须过 Permission Filter —— 不过滤的检索就是越权。**

这是 Agent 系统里最容易被忽略的一条：向量库里存了全公司的文档，
检索时如果不带权限过滤，Agent 就成了"绕过权限的搜索框"。
而且它不会报错 —— 它只是老老实实把不该看的内容喂给了模型。

所以 `RetrievalPipeline.permission` 是**构造期必填**，没有"先不过滤"这个选项。

过滤器要跑两遍：

    · push-down：交给 Retriever 在检索时过滤（高效，但**换一个 Retriever 就没了**）
    · verify：pipeline 拿到结果后再过一遍（兜底）

只靠前者 = 权限由 Retriever 的实现决定。这是把安全边界外包给了存储层。

C-10  没有 Citation 的 Chunk 不进 Context（见 items.py 的校验）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .items import ContextItem, ContextSource, knowledge_chunk


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    tenant_id: str = ""
    subject: str = ""                 # 检索者（user / agent id）
    roles: tuple[str, ...] = ()
    limit: int = 10
    filters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("RetrievalQuery.text is required")


@dataclass(frozen=True)
class Chunk:
    """一条检索结果。`citation` 必填 —— 它是 C-10 的载体。"""

    chunk_id: str
    document_id: str
    text: str
    citation: str
    score: float = 0.0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.citation:
            raise ValueError("C-10: Chunk.citation is required")


class Retriever(Protocol):
    """存储层检索能力（Qdrant / BM25 / 混合）。

    ⚠️ 它**不是**安全边界 —— 权限过滤由 `PermissionFilter` 单独负责。
    """

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]: ...


class PermissionFilter(Protocol):
    """C-9：这条 Chunk 能不能给这个检索者看。"""

    def allowed(self, chunk: Chunk, query: RetrievalQuery) -> bool: ...


class AllowAll:
    """只在测试里用。生产里用它等于把权限关掉 —— 所以类名必须刺眼。"""

    def allowed(self, chunk: Chunk, query: RetrievalQuery) -> bool:
        return True


class DenyAll:
    def allowed(self, chunk: Chunk, query: RetrievalQuery) -> bool:
        return False


@dataclass(frozen=True)
class TenantFilter:
    """最小可用的权限过滤器：tenant 不匹配就拒绝。

    真实系统接 OPA / IAM，但**接口形状一致** ——
    所以"过滤发生在检索之后"这件事在测试里就能被验证。
    """

    def allowed(self, chunk: Chunk, query: RetrievalQuery) -> bool:
        owner = chunk.attributes.get("tenant_id", "")
        if not query.tenant_id:
            return False                       # 没有 tenant 的检索一律拒绝
        return owner == query.tenant_id


@dataclass(frozen=True)
class RetrievalResult:
    kept: tuple[Chunk, ...] = ()
    denied: tuple[Chunk, ...] = ()
    dropped_no_citation: tuple[Chunk, ...] = ()


@dataclass
class RetrievalPipeline:
    """§21 那条 RAG 链的**可执行骨架**。

    `permission` 必填（C-9）。`rerank` 可选 —— 重排只影响质量，不影响边界。
    """

    retriever: Retriever
    permission: PermissionFilter
    rerank: Any | None = None

    def run(self, query: RetrievalQuery) -> RetrievalResult:
        candidates = list(self.retriever.search(query))
        if self.rerank is not None:
            candidates = list(self.rerank(query, candidates))

        kept: list[Chunk] = []
        denied: list[Chunk] = []
        no_citation: list[Chunk] = []

        for chunk in candidates:
            if not chunk.citation:
                no_citation.append(chunk)
                continue
            if not self.permission.allowed(chunk, query):
                denied.append(chunk)
                continue
            kept.append(chunk)

        kept.sort(key=lambda c: (-c.score, c.chunk_id))
        return RetrievalResult(
            kept=tuple(kept[: query.limit]),
            denied=tuple(denied),
            dropped_no_citation=tuple(no_citation),
        )

    def to_context_items(self, result: RetrievalResult) -> tuple[ContextItem, ...]:
        """C-1 的落点：Chunk（Knowledge）→ ContextItem（Context）的显式转换。"""
        return tuple(
            knowledge_chunk(
                c.chunk_id, c.text, citation=c.citation, score=c.score
            )
            for c in result.kept
        )
