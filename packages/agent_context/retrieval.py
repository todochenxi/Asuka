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


# ============================================================ M5：Fusion / Rerank
#
# §21 的 RAG 链里，`→ Fusion → Rerank →` 这两步此前是空的：
# `RetrievalPipeline.rerank` 是个可选钩子，而**全仓没有任何实现**；
# "Hybrid Search"（Vector + BM25 融合）连钩子都没有。M5 把它们补上。


@dataclass(frozen=True)
class HybridRetriever:
    """Vector + BM25 的**混合检索器**（M5）。

    它跑两个子检索器，然后用 **RRF**（Reciprocal Rank Fusion）把两串名次
    合成一串。为什么用名次融合而不是分数融合：

        · 两个检索器的分数**不同量纲**（余弦相似度 vs BM25 分），
          直接加权求和需要每个语料各调一次权重 —— 那个权重没有依据；
        · 名次是无量纲的，RRF 只有**一个**参数 `k`（默认 60，学界通用），
          且对异常分数天然鲁棒。

    ⚠️ 它**不**做去重之外的事，也**不**截断到 `limit` —— 截断归
    `RetrievalPipeline`（`kept[:limit]`）。这里多取一些（`fan_out`）再融合，
    否则两路各自只拿 limit，融合后仍然只有 limit，hybrid 毫无意义。

    ⚠️ 它**不是**安全边界：权限仍由 `PermissionFilter` 单独负责（C-9）。
    """

    retrievers: tuple[Retriever, ...]
    k: int = 60
    #: 每个子检索器取多少条（默认 `limit` 的 2 倍）。融合需要重叠。
    fan_out: int = 0

    def __post_init__(self) -> None:
        if not self.retrievers:
            raise ValueError("HybridRetriever needs at least one retriever")
        if self.k < 1:
            raise ValueError("HybridRetriever.k must be >= 1")

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        fan = self.fan_out or max(query.limit * 2, query.limit)
        fused: dict[str, float] = {}
        seen: dict[str, Chunk] = {}
        for retriever in self.retrievers:
            sub_query = RetrievalQuery(
                text=query.text,
                tenant_id=query.tenant_id,
                subject=query.subject,
                roles=query.roles,
                limit=fan,
                filters=query.filters,
            )
            for rank, chunk in enumerate(retriever.search(sub_query), start=1):
                fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + 1.0 / (
                    self.k + rank
                )
                # 同一个 chunk 被两路都检到：保留**分数字段更大的那个** score，
                # 融合名次另有 `rrf_score` 记在 attributes 里（两个量纲不混）。
                if chunk.chunk_id not in seen or chunk.score > seen[chunk.chunk_id].score:
                    seen[chunk.chunk_id] = chunk
        ranked = sorted(fused, key=lambda cid: (-fused[cid], cid))
        out: list[Chunk] = []
        for cid in ranked:
            chunk = seen[cid]
            attrs = {**chunk.attributes, "rrf_score": fused[cid]}
            out.append(
                Chunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    text=chunk.text,
                    citation=chunk.citation,
                    # `score` 用 RRF 值：它是**这次检索**的可比量，
                    # 下游按 score 排序时用它对。原始 score 仍在 attributes 里。
                    score=fused[cid],
                    attributes=attrs,
                )
            )
        return out


@dataclass(frozen=True)
class LexicalRerank:
    """一个**零依赖**的重排器（M5）：按"查询词覆盖率 + 名次"重排。

    它是 rerank 这一步的一个**真实实现**（此前只有钩子、没有实现）——
    刻意不引入交叉编码器模型（那要装依赖、要 GPU，且本项目核心层零第三方）。
    它做的是 RAG 里最朴素也最有效的一档：**把覆盖了更多查询词的片提到前面**。

    ⚠️ 它只**重排**，不增不删 —— 返回的集合与传入的集合逐条相同（只换顺序）。
    一个会悄悄丢候选的 rerank 比没有更糟。
    """

    #: 名次在总分里的权重：0 = 只看覆盖率，越大越保留原检索名次。
    rank_weight: float = 0.5

    def __call__(self, query: RetrievalQuery, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        terms = _query_terms(query.text)
        scored: list[tuple[float, int, Chunk]] = []
        for index, chunk in enumerate(chunks):
            coverage = _coverage(terms, chunk.text)
            # 名次项用 1/(index+1) 归一到 (0,1]，与覆盖率同量纲。
            rank_score = 1.0 / (index + 1)
            score = coverage + self.rank_weight * rank_score
            scored.append((score, index, chunk))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return tuple(chunk for _score, _index, chunk in scored)


def _query_terms(text: str) -> frozenset[str]:
    """查询词：小写 + 按非字母数字切 + 去掉单字符（噪声）。"""
    import re

    return frozenset(
        t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1
    )


def _coverage(terms: frozenset[str], text: str) -> float:
    if not terms:
        return 0.0
    lowered = text.lower()
    hit = sum(1 for t in terms if t in lowered)
    return hit / len(terms)


__all__ = [
    "AllowAll",
    "Chunk",
    "DenyAll",
    "HybridRetriever",
    "LexicalRerank",
    "PermissionFilter",
    "RetrievalPipeline",
    "RetrievalQuery",
    "RetrievalResult",
    "Retriever",
    "TenantFilter",
]
