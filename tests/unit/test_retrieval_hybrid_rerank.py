"""M5：Hybrid Search（Vector + BM25 融合）与 Rerank —— §21 RAG 链里那两个空步。

此前 `RetrievalPipeline.rerank` 只是个可选钩子（全仓无实现），
"Hybrid Search" 连钩子都没有。这里给它们**真实实现**，并钉住两条不变量：

    · Fusion 用名次（RRF），不是分数 —— 两个检索器分数不同量纲；
    · Rerank 只换顺序，**不增不删**候选（会悄悄丢候选的 rerank 比没有更糟）。
"""
from __future__ import annotations

import unittest

from packages.agent_context.retrieval import (
    Chunk,
    HybridRetriever,
    LexicalRerank,
    RetrievalPipeline,
    RetrievalQuery,
    RetrievalResult,
)


def _chunk(cid: str, text: str, score: float = 1.0) -> Chunk:
    return Chunk(
        chunk_id=cid, document_id="doc", text=text, citation=f"cite:{cid}", score=score
    )


class _FakeRetriever:
    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)

    def search(self, query):
        return self._chunks[: query.limit]


class _AllowAll:
    def allowed(self, chunk, query) -> bool:
        return True


class HybridRetrieverTest(unittest.TestCase):
    def test_a_chunk_in_both_lists_outranks_one_in_a_single_list(self) -> None:
        """RRF 的核心：被**两路都检到**的排在只被一路检到的前面。"""
        lexical = _FakeRetriever([_chunk("a", "x"), _chunk("b", "y")])
        dense = _FakeRetriever([_chunk("b", "y"), _chunk("c", "z")])
        hybrid = HybridRetriever((lexical, dense))

        order = [c.chunk_id for c in hybrid.search(RetrievalQuery(text="q", limit=3))]

        self.assertEqual(order[0], "b", "两路都命中 ⇒ 融合分最高")
        self.assertEqual(set(order), {"a", "b", "c"})

    def test_it_records_the_rrf_score_in_attributes(self) -> None:
        lexical = _FakeRetriever([_chunk("a", "x")])
        dense = _FakeRetriever([_chunk("a", "x")])
        hybrid = HybridRetriever((lexical, dense))
        hit = hybrid.search(RetrievalQuery(text="q", limit=1))[0]
        self.assertIn("rrf_score", hit.attributes)
        self.assertGreater(hit.attributes["rrf_score"], 0)

    def test_a_higher_k_flattens_the_rank_difference(self) -> None:
        """`k` 越大，名次差被压得越小 —— 它是唯一的旋钮（默认 60）。"""
        lexical = _FakeRetriever([_chunk("a", "x")])
        small = HybridRetriever((lexical,), k=1).search(RetrievalQuery(text="q", limit=1))[0]
        large = HybridRetriever((lexical,), k=1000).search(RetrievalQuery(text="q", limit=1))[0]
        self.assertGreater(small.attributes["rrf_score"], large.attributes["rrf_score"])

    def test_it_does_not_truncate_to_limit(self) -> None:
        """融合前要多取（fan_out）—— 否则两路各只拿 limit，融合没意义。"""
        lexical = _FakeRetriever([_chunk(f"l{i}", "x") for i in range(5)])
        dense = _FakeRetriever([_chunk(f"d{i}", "x") for i in range(5)])
        hybrid = HybridRetriever((lexical, dense), fan_out=5)
        self.assertEqual(len(hybrid.search(RetrievalQuery(text="q", limit=1))), 10)

    def test_no_retrievers_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            HybridRetriever(())


class LexicalRerankTest(unittest.TestCase):
    def test_coverage_promotes_a_lower_ranked_but_more_relevant_chunk(self) -> None:
        chunks = [_chunk("noise", "set key value"), _chunk("hit", "redis expire timeout")]
        reranked = LexicalRerank(rank_weight=0.0)(
            RetrievalQuery(text="expire timeout"), chunks
        )
        self.assertEqual([c.chunk_id for c in reranked], ["hit", "noise"])

    def test_it_never_drops_or_adds_candidates(self) -> None:
        """只换顺序 —— 一个会悄悄丢候选的 rerank 比没有更糟。"""
        chunks = [_chunk("a", "one two"), _chunk("b", "three"), _chunk("c", "four")]
        reranked = LexicalRerank()(RetrievalQuery(text="three"), chunks)
        self.assertEqual(set(c.chunk_id for c in reranked), {"a", "b", "c"})
        self.assertEqual(len(reranked), 3)


class PipelineUsesRerankTest(unittest.TestCase):
    def test_a_rerank_hook_reorders_the_kept_set(self) -> None:
        retriever = _FakeRetriever([_chunk("noise", "set"), _chunk("hit", "expire")])
        pipeline = RetrievalPipeline(
            retriever=retriever,
            permission=_AllowAll(),
            rerank=LexicalRerank(rank_weight=0.0),
        )
        result = pipeline.run(RetrievalQuery(text="expire", limit=2))
        self.assertEqual([c.chunk_id for c in result.kept], ["hit", "noise"])

    def test_permission_still_wins_after_a_rerank(self) -> None:
        """重排只影响质量，**不影响边界**（C-9）：被拒的片重排之后照样被拒。"""
        class _DenyHit:
            def allowed(self, chunk, query) -> bool:
                return chunk.chunk_id != "hit"

        retriever = _FakeRetriever([_chunk("noise", "set"), _chunk("hit", "expire")])
        pipeline = RetrievalPipeline(
            retriever=retriever,
            permission=_DenyHit(),
            rerank=LexicalRerank(rank_weight=0.0),
        )
        result = pipeline.run(RetrievalQuery(text="expire", limit=2))
        self.assertEqual([c.chunk_id for c in result.kept], ["noise"])
        self.assertEqual([c.chunk_id for c in result.denied], ["hit"])


if __name__ == "__main__":
    unittest.main()
