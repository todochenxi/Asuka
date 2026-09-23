"""Knowledge Base 的契约：词法基线、权限 fail-closed、C-9 / C-10 走 AgentOS 的管线。

--------------------------------------------------------------------------
这里锁的三件事

1. **BM25 是"对照组"，不是"省事的替代品"**
   这个平台要比较检索器。没有词法基线，"向量检索得了 0.72" 无法解释 ——
   比一个三十年前就成熟的算法好多少？这个分母必须有。

2. **权限必须是一条真的规则，不能是 `AllowAll` 换个名字**
   语料是公开文档，"看起来人人可读"不等于"永远不需要过滤"。
   规则写成 fail-closed：**没标记 ⇒ 拒绝**。
   后果的差别是关键 —— 忘标记应该是"检不到"（可见），不是"泄漏"（不可见）。

3. **C-9 / C-10 由 AgentOS 的 `RetrievalPipeline` 保证，不由我们重写**
   `permission` 是构造期必填；没有 citation 的 chunk 进不了结果。
"""
from __future__ import annotations

import unittest

from packages.agent_context.retrieval import Chunk, RetrievalQuery

from asuka.kb import (
    BM25Index,
    BM25Retriever,
    KnowledgeBase,
    PublicCorpusFilter,
    build_knowledge_base,
    tokenize,
)


def _chunk(cid: str, text: str, *, visibility: str = "public", citation: str = "") -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=cid.split(":")[0],
        text=text,
        citation=citation or f"Redis · {cid}",
        attributes={"visibility": visibility, "unit_id": cid.split(":")[0]},
    )


_CORPUS = (
    _chunk("expire:000", "Set a timeout on key. After the timeout has expired, the key will be deleted."),
    _chunk("get:000", "Returns the string value of a key."),
    _chunk("incr:000", "Increments the number stored at key by one. Atomic counter."),
    _chunk("zadd:000", "Adds members with scores to a sorted set."),
)


class TestTokenizer(unittest.TestCase):
    def test_lowercases_and_splits(self) -> None:
        self.assertEqual(tokenize("EXPIRE key-1"), ["expire", "key", "1"])

    def test_no_stemming_on_purpose(self) -> None:
        """`set` / `setnx` 是不同的检索信号，激进归并会把它们并到一起。"""
        self.assertNotEqual(tokenize("SETNX"), tokenize("SET"))


class TestBM25(unittest.TestCase):
    def setUp(self) -> None:
        self.index = BM25Index(_CORPUS)

    def test_ranks_the_right_document_first(self) -> None:
        hits = self.index.search(tokenize("timeout on a key"), limit=3)
        self.assertEqual(hits[0].chunk_id, "expire:000")

    def test_another_query(self) -> None:
        hits = self.index.search(tokenize("atomic counter increment"), limit=3)
        self.assertEqual(hits[0].chunk_id, "incr:000")

    def test_zero_score_documents_are_not_returned(self) -> None:
        """一分没有的不能拿噪声凑数 —— 检索结果条数不是越多越好。"""
        hits = self.index.search(tokenize("timeout"), limit=10)
        self.assertEqual([h.chunk_id for h in hits], ["expire:000"])

    def test_idf_is_always_positive(self) -> None:
        """Lucene 的 IDF 形式 —— 出现在所有文档里的词不会得负分。"""
        for value in self.index._idf.values():
            self.assertGreater(value, 0.0)

    def test_scores_are_attached(self) -> None:
        hits = self.index.search(tokenize("timeout"), limit=1)
        self.assertGreater(hits[0].score, 0.0)

    def test_deterministic_tie_break(self) -> None:
        """同分时按 chunk_id 排 —— 否则两次跑出来的顺序不一样，评测就没法复现。"""
        a = [h.chunk_id for h in self.index.search(tokenize("key"), limit=10)]
        b = [h.chunk_id for h in self.index.search(tokenize("key"), limit=10)]
        self.assertEqual(a, b)


class TestPublicCorpusFilter(unittest.TestCase):
    """fail-closed：忘标记的后果必须是"检不到"，不是"泄漏"。"""

    def test_allows_public(self) -> None:
        f = PublicCorpusFilter()
        self.assertTrue(f.allowed(_chunk("a:0", "x"), RetrievalQuery(text="q")))

    def test_denies_unmarked(self) -> None:
        f = PublicCorpusFilter()
        self.assertFalse(
            f.allowed(_chunk("a:0", "x", visibility=""), RetrievalQuery(text="q"))
        )

    def test_denies_other_visibility(self) -> None:
        f = PublicCorpusFilter()
        for vis in ("internal", "private", "confidential"):
            with self.subTest(visibility=vis):
                self.assertFalse(
                    f.allowed(_chunk("a:0", "x", visibility=vis), RetrievalQuery(text="q"))
                )


class TestPipelineWiring(unittest.TestCase):
    """C-9 / C-10 是 AgentOS 保证的 —— 这里验的是**我们确实接上了**。"""

    def _kb(self, chunks) -> KnowledgeBase:
        retriever = BM25Retriever(tuple(chunks))
        from packages.agent_context.retrieval import RetrievalPipeline

        return KnowledgeBase(
            topic="t",
            pipeline=RetrievalPipeline(retriever=retriever, permission=PublicCorpusFilter()),
            retriever=retriever,
            kind="bm25",
        )

    def test_denied_chunks_are_reported_not_silently_dropped(self) -> None:
        kb = self._kb(
            [
                _chunk("public:0", "timeout on a key"),
                _chunk("secret:0", "timeout on a key", visibility="internal"),
            ]
        )
        result = kb.search("timeout", limit=10)
        self.assertEqual([c.chunk_id for c in result.kept], ["public:0"])
        self.assertEqual([c.chunk_id for c in result.denied], ["secret:0"])

    def test_unmarked_chunk_is_denied_end_to_end(self) -> None:
        kb = self._kb([_chunk("mystery:0", "timeout on a key", visibility="")])
        result = kb.search("timeout", limit=10)
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.denied), 1)

    def test_filters_restrict_the_candidate_set(self) -> None:
        kb = self._kb(_CORPUS)
        result = kb.search("key", limit=10, filters={"unit_id": "get"})
        self.assertTrue(all(c.attributes["unit_id"] == "get" for c in result.kept))

    def test_citations_helper(self) -> None:
        kb = self._kb(_CORPUS)
        self.assertEqual(kb.citations("atomic counter increment", limit=1), ["Redis · incr:000"])


class TestBuildKnowledgeBase(unittest.TestCase):
    def test_unknown_kind_rejected(self) -> None:
        from pathlib import Path

        with self.assertRaises(ValueError):
            build_knowledge_base("redis", corpus_dir=Path("."), kind="nope")

    def test_dense_without_store_rejected(self) -> None:
        from pathlib import Path

        from asuka.vectorstore import VectorStoreError

        with self.assertRaises(VectorStoreError):
            build_knowledge_base("redis", corpus_dir=Path("."), kind="dense", store=None)


if __name__ == "__main__":
    unittest.main()
