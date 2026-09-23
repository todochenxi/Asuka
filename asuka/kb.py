"""Knowledge Base：检索器 + 权限 + 管线。

--------------------------------------------------------------------------
复用，不另造

AgentOS 已经把这条链的**形状**定好了（`packages/agent_context/retrieval.py`）：

    Query → Permission Filter → 检索 → Fusion → Rerank → Top-K → Citation → Context

其中两条是**不变量**，我们照单全收：

    C-9   Retrieval 必须过 Permission Filter —— 不过滤的检索就是越权
    C-10  没有 Citation 的 Chunk 不进 Context

所以这里不重新设计管线，只**填三个洞**：

    BM25Retriever      词法检索器（零依赖，**对照组**）
    QdrantRetriever    向量检索器（dense，bge-m3）
    PublicCorpusFilter 权限规则（**fail-closed**，见下）

--------------------------------------------------------------------------
为什么 `PublicCorpusFilter` 不是 `AllowAll` 换个名字

`agent_context` 里已经有一个 `AllowAll`，docstring 写得很刺眼：
"只在测试里用。生产里用它等于把权限关掉 —— 所以类名必须刺眼。"

我们的语料是**公开官方文档**，看起来"人人可读"就等于不需要过滤 ——
但那是把"这次恰好是公开的"写死成"永远不需要权限"。
一旦往语料里加一份内部文档，`AllowAll` 不会拦、不会报、不会记，
它只是**老老实实把不该看的内容喂给了模型**（这正是 C-9 那段话的原意）。

所以规则写成一条**真的规则**：

    只有 `attributes["visibility"] == "public"` 的 chunk 可读。
    **没有标记的一律拒绝**（fail-closed）。

后果的差别是关键的：忘标记 ⇒ **检不到**（可见的失败），而不是**泄漏**（不可见的失败）。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from packages.agent_context.retrieval import (
    Chunk,
    PermissionFilter,
    RetrievalPipeline,
    RetrievalQuery,
    Retriever,
)

from .corpus import read_chunks
from .embedding import Embedder, build_embedder
from .vectorstore import (
    QdrantStore,
    VectorStoreError,
    assert_embedder_matches,
    load_index_manifest,
)

# ---------------------------------------------------------------- 分词

#: 词法检索的分词：小写 + 按非字母数字切。
#: **刻意不做词干还原** —— 技术文档里的 `EXPIRE` / `expire` / `expires`
#: 是三个不同的检索信号，激进归并会把 `SET` 和 `SETNX` 也并到一起。
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: 英文停用词。**这不是可选的优化，是 BM25 的必要条件**（实测见下）。
#:
#: 洞的形状：redis.io 有几节标题是**问句形状**的，例如
#:
#:     ### What key is served first? What client? What element? Priority ordering details.
#:
#: 于是 `what` 在这份语料里**极其罕见** ⇒ idf 高达 **4.56**（比 `multi` 的 3.04 还高）。
#: 问 "What is MULTI used for in Redis?" 时，那一片靠 `what` 的 tf=6 拿了
#: **7.68 / 14.68 的分**（过半），而真正该第一的 `multi:001` 掉到**第 7 名**。
#:
#: ⇒ 停用词不是"去掉几个没用的词"，是**防止罕见的功能词冒充高信息量词**。
#: 这个失效模式在别的语料里一样成立：只要文档里有问句标题，它就会出现。
_STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are aren as at be because been
    before being below between both but by can cannot could couldn did didn do does
    doesn doing don down during each few for from further had hadn has hasn have haven
    having he her here hers herself him himself his how i if in into is isn it its
    itself just let me more most mustn my myself no nor not of off on once only or
    other ought our ours ourselves out over own same shan she should shouldn so some
    such than that the their theirs them themselves then there these they this those
    through to too under until up very was wasn we were weren what when where which
    while who whom why will with won would wouldn you your yours yourself yourselves
    """.split()
)


def tokenize(text: str) -> list[str]:
    """小写 → 切词 → 去停用词。"""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


# ---------------------------------------------------------------- BM25


@dataclass
class BM25Index:
    """一份内存里的 BM25 倒排索引。零依赖。

    `k1` / `b` 用标准默认（1.5 / 0.75，Lucene 同源）。
    IDF 用 Lucene 的 `log(1 + (N - df + 0.5)/(df + 0.5))` ——
    **恒为正**，所以"出现在所有文档里的词"不会得到负分（经典 BM25 会）。
    """

    chunks: tuple[Chunk, ...]
    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, dict[int, int]] = field(default_factory=dict, repr=False)
    _lengths: list[int] = field(default_factory=list, repr=False)
    _avgdl: float = 0.0
    _idf: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._postings = {}
        self._lengths = []
        for i, chunk in enumerate(self.chunks):
            toks = tokenize(chunk.text)
            self._lengths.append(len(toks))
            for term, tf in Counter(toks).items():
                self._postings.setdefault(term, {})[i] = tf
        n = len(self.chunks) or 1
        self._avgdl = (sum(self._lengths) / n) if self._lengths else 0.0
        for term, posting in self._postings.items():
            df = len(posting)
            self._idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    # ------------------------------------------------------------ 打分

    def score(self, query_tokens: Sequence[str]) -> list[float]:
        scores = [0.0] * len(self.chunks)
        avgdl = self._avgdl or 1.0
        for term in query_tokens:
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf[term]
            for i, tf in posting.items():
                dl = self._lengths[i]
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                scores[i] += idf * tf * (self.k1 + 1.0) / denom
        return scores

    def search(
        self, query_tokens: Sequence[str], *, limit: int, where: Any | None = None
    ) -> list[Chunk]:
        scores = self.score(query_tokens)
        ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], self.chunks[i].chunk_id))
        out: list[Chunk] = []
        for i in ranked:
            if scores[i] <= 0.0:
                break                      # 一分没有的，不算命中（别拿噪声凑数）
            chunk = self.chunks[i]
            if where is not None and not where(chunk):
                continue
            out.append(_with_score(chunk, scores[i]))
            if len(out) >= limit:
                break
        return out


@dataclass
class BM25Retriever:
    """词法检索器。实现 `agent_context.retrieval.Retriever`。

    它是这个评测平台的**对照组**：没有它，"向量检索得了 0.72" 无法解释 ——
    比一个三十年前就成熟的算法好多少？这个分母必须有。
    """

    chunks: tuple[Chunk, ...]
    index: BM25Index = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.index = BM25Index(self.chunks)

    @classmethod
    def from_corpus(cls, corpus_dir: Path, topic: str) -> "BM25Retriever":
        return cls(tuple(read_chunks(corpus_dir / topic / "chunks.jsonl")))

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        where = _matcher(query.filters)
        return self.index.search(tokenize(query.text), limit=query.limit, where=where)


# ---------------------------------------------------------------- dense


@dataclass
class QdrantRetriever:
    """向量检索器（dense）。实现 `agent_context.retrieval.Retriever`。

    ⚠️ **它自己会拒绝**：查询用的 embedder 必须与建索引时同一个签名
    （见 `vectorstore.assert_embedder_matches`）。对不上直接抛错，不返回结果 ——
    维度一样、语义不同的两个模型之间，分数是没有意义的。
    """

    store: QdrantStore
    collection: str
    embedder: Embedder
    score_threshold: float | None = None

    @classmethod
    def from_corpus(
        cls,
        corpus_dir: Path,
        topic: str,
        *,
        store: QdrantStore,
        embedder: Embedder | None = None,
        allow_non_semantic: bool = False,
    ) -> "QdrantRetriever":
        emb = embedder or build_embedder()
        manifest = load_index_manifest(corpus_dir, topic)
        name = assert_embedder_matches(
            manifest, emb.info, allow_non_semantic=allow_non_semantic
        )
        return cls(store=store, collection=name, embedder=emb)

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        vec = self.embedder.embed([query.text])[0]
        hits = self.store.search(
            self.collection,
            vec,
            top_k=query.limit,
            score_threshold=self.score_threshold,
            query_filter=_qdrant_filter(query.filters),
        )
        return [
            Chunk(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                text=h.text,
                citation=h.citation,
                score=h.score,
                attributes=dict(h.payload),
            )
            for h in hits
        ]


# ---------------------------------------------------------------- 过滤


@dataclass(frozen=True)
class PublicCorpusFilter:
    """C-9 的落地：只有显式标记为 `public` 的 chunk 可读。

    **fail-closed** —— 没标记 ⇒ 拒绝。理由见模块 docstring：
    忘标记的后果应该是"检不到"，不是"泄漏"。
    """

    allow: frozenset[str] = frozenset({"public"})

    def allowed(self, chunk: Chunk, query: RetrievalQuery) -> bool:
        return str(chunk.attributes.get("visibility", "")) in self.allow


# ---------------------------------------------------------------- 组装


def _with_score(chunk: Chunk, score: float) -> Chunk:
    """`Chunk` 是 frozen 的 —— 换分数要重建，不能就地改（这是对的，别绕过）。"""
    return Chunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        text=chunk.text,
        citation=chunk.citation,
        score=score,
        attributes=chunk.attributes,
    )


def _matcher(filters: Mapping[str, Any] | None):
    """把 `filters` 变成谓词。值是元组/列表时按"任一匹配"。

    用 chunk 自己的 attributes 判 —— 这是**检索器内部**的优化，
    不是权限（权限由 `PermissionFilter` 单独负责，C-9 说得很清楚）。
    """
    if not filters:
        return None
    items = list(filters.items())

    def where(chunk: Chunk) -> bool:
        for key, want in items:
            have = chunk.attributes.get(key)
            if isinstance(want, (list, tuple, set, frozenset)):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True

    return where


def _qdrant_filter(filters: Mapping[str, Any] | None) -> Any | None:
    """`filters` → Qdrant 的 push-down filter（**只是优化**，兜底仍靠 pipeline）。"""
    if not filters:
        return None
    try:
        from qdrant_client import models  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    must: list[Any] = []
    for key, want in filters.items():
        values = list(want) if isinstance(want, (list, tuple, set, frozenset)) else [want]
        must.append(
            models.FieldCondition(key=key, match=models.MatchAny(any=[str(v) for v in values]))
        )
    return models.Filter(must=must) if must else None


@dataclass
class KnowledgeBase:
    """一个 topic 的可检索知识库。**管线用 AgentOS 的**，不是自造的。"""

    topic: str
    pipeline: RetrievalPipeline
    retriever: Retriever
    kind: str = ""                       # bm25 / dense

    def search(self, text: str, *, limit: int = 10, filters: Mapping[str, Any] | None = None):
        """返回 `(kept, denied, dropped_no_citation)` 三段 —— 拒绝也要能看见。"""
        result = self.pipeline.run(
            RetrievalQuery(text=text, limit=limit, filters=dict(filters or {}))
        )
        return result

    def citations(self, text: str, *, limit: int = 10) -> list[str]:
        return [c.citation for c in self.search(text, limit=limit).kept]


def build_knowledge_base(
    topic: str,
    *,
    corpus_dir: Path,
    kind: str = "bm25",
    store: QdrantStore | None = None,
    embedder: Embedder | None = None,
    allow_non_semantic: bool = False,
    permission: PermissionFilter | None = None,
) -> KnowledgeBase:
    """造一个知识库。

    `kind='bm25'`  → 词法基线（零依赖，不需要 Qdrant）
    `kind='dense'` → 向量检索（需要 Qdrant + 索引 + 同一个 embedder）

    `permission` 默认 `PublicCorpusFilter`（fail-closed）。
    **不提供 `None` 表示"不过滤"这个选项** —— C-9 说 permission 是构造期必填。
    """
    perm = permission or PublicCorpusFilter()
    if kind == "bm25":
        retriever: Retriever = BM25Retriever.from_corpus(corpus_dir, topic)
    elif kind == "dense":
        if store is None:
            raise VectorStoreError("dense 检索需要 QdrantStore")
        retriever = QdrantRetriever.from_corpus(
            corpus_dir,
            topic,
            store=store,
            embedder=embedder,
            allow_non_semantic=allow_non_semantic,
        )
    else:
        raise ValueError(f"unknown retriever kind: {kind!r} (bm25/dense)")

    return KnowledgeBase(
        topic=topic,
        pipeline=RetrievalPipeline(retriever=retriever, permission=perm),
        retriever=retriever,
        kind=kind,
    )
