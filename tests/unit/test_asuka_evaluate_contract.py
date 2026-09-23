"""Evaluation 的契约：检索指标、停用词回归、以及**recall 上限必须被报出来**。

--------------------------------------------------------------------------
锁三件事

**一、停用词不是"优化"，是 BM25 的必要条件（实测回归）**

redis.io 有几节标题是**问句形状**的：

    ### What key is served first? What client? What element? Priority ordering details.

于是 `what` 在这份语料里**极其罕见** ⇒ idf 高达 **4.56**（比 `multi` 的 3.04 还高）。
问 "What is MULTI used for in Redis?" 时，那一片靠 `what` 的 tf=6 拿了
**7.68 / 14.68 = 过半的分**，而真正该第一的 `multi:001` 掉到**第 7 名**。

⇒ 失效模式是：**罕见的功能词冒充高信息量词**。
只要文档里有问句标题，它就会出现 —— 所以这条要钉在测试里。

**二、recall 的上限必须一起报**

一道题声明 7 处 evidence 而 `top_k=5`，recall 上限就是 5/7 = 0.71，
**无论检索多好都到不了 1.0**。不报上限，读者会把"上限 0.71、实际 0.44"
误读成"检索很差"，而真相可能是"已接近满分"。
⇒ 一个数字的**分母是谁划的**，必须能被读出来。

**三、指标要能被手算复核**

测试里用 3~4 条 chunk 的小语料，指标值**手算得出来**。
拿一个算不出期望值的语料去测指标，测的其实是"代码没崩"。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from packages.agent_context.retrieval import Chunk, RetrievalQuery

from asuka.dataset import Dataset, Evidence, TaskItem
from asuka.evaluate import build_parser, evaluate_retrieval, render_markdown
from asuka.kb import BM25Index, BM25Retriever, PublicCorpusFilter, tokenize

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = _REPO / "asuka" / "corpus" / "redis" / "chunks.jsonl"
_DATASETS = _REPO / "asuka" / "datasets"


# ---------------------------------------------------------------- 语料


def _chunk(cid: str, text: str, *, unit: str = "", section: str = "") -> Chunk:
    u = unit or cid.split(":")[1]
    s = section or "overview"
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{u}",
        text=text,
        citation=f"Redis · {u.upper()} · {s}",
        attributes={"unit_id": u, "section": s, "visibility": "public"},
    )


#: 复刻那个失效模式：blpop 有一片标题是问句，multi 的概述很短
_TRAP = (
    _chunk(
        "redis:blpop:000",
        "# BLPOP\n\nBLPOP is a blocking list pop. It pops from the head of a list.",
        unit="blpop",
        section="overview",
    ),
    _chunk(
        "redis:blpop:006",
        "# What key is served first? What client? What element? Priority ordering details.\n\n"
        "* What key is served first? What client? What element? What if two keys become ready?",
        unit="blpop",
        section="What key is served first? What client? What element? Priority ordering details.",
    ),
    _chunk(
        "redis:multi:001",
        "# MULTI\n\nMarks the start of a transaction. Queued commands are run by EXEC.",
        unit="multi",
        section="overview",
    ),
    _chunk(
        "redis:get:000",
        "# GET\n\nReturns the string value of a key.",
        unit="get",
        section="overview",
    ),
)


class TestStopwords(unittest.TestCase):
    def test_function_words_are_dropped(self) -> None:
        self.assertEqual(tokenize("What is the value of a key"), ["value", "key"])

    def test_content_words_survive(self) -> None:
        self.assertEqual(tokenize("EXPIRE key seconds NX"), ["expire", "key", "seconds", "nx"])

    def test_the_trap_is_closed(self) -> None:
        """回归：问句标题不能靠 `what` 拿分，`multi:001` 必须第一。"""
        idx = BM25Index(_TRAP)
        hits = idx.search(tokenize("What is MULTI used for in Redis?"), limit=3)
        self.assertEqual(hits[0].chunk_id, "redis:multi:001")

    def test_what_is_not_a_discriminator(self) -> None:
        """`what` 不该出现在任何一片的得分里 —— 它是停用词。"""
        idx = BM25Index(_TRAP)
        self.assertNotIn("what", idx._postings)


# ---------------------------------------------------------------- 指标


def _kb(chunks) -> BM25Retriever:
    return BM25Retriever(tuple(chunks))


def _item(task_id: str, question: str, evidence, *, difficulty: str = "simple") -> TaskItem:
    return TaskItem(
        task_id=task_id,
        question=question,
        reference_answer="x" * 80,
        source_document="redis:multi",
        difficulty=difficulty,
        evidence=tuple(evidence),
    )


class TestRetrievalMetrics(unittest.TestCase):
    """3 片语料，指标手算得出来。"""

    def _run(self, items, *, top_k=5, chunks=_TRAP):
        ds = Dataset(topic="redis", items=tuple(items))
        ds.resolve(chunks)
        from asuka.kb import KnowledgeBase
        from packages.agent_context.retrieval import RetrievalPipeline

        r = _kb(chunks)
        kb = KnowledgeBase(
            topic="redis",
            pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
            retriever=r,
            kind="bm25",
        )
        return evaluate_retrieval(kb, ds, top_k=top_k, corpus_chunks=len(chunks))

    def test_perfect_retrieval(self) -> None:
        rep = self._run([_item("t1", "MULTI transaction EXEC", [Evidence("multi", "overview")])])
        s = rep.items[0]
        self.assertEqual(s.first_hit_rank, 1)
        self.assertEqual(s.recall, 1.0)
        self.assertEqual(s.precision, 1.0)
        self.assertEqual(s.reciprocal_rank, 1.0)

    def test_total_miss(self) -> None:
        """问一个语料里完全没有的东西 —— recall/precision 都该是 0，且**不算命中**。"""
        rep = self._run([_item("t1", "zzzz qqqq", [Evidence("get", "overview")])])
        s = rep.items[0]
        self.assertFalse(s.hit)
        self.assertEqual(s.recall, 0.0)
        self.assertEqual(s.first_hit_rank, 0)
        self.assertEqual(s.reciprocal_rank, 0.0)

    def test_recall_is_fraction_of_evidence(self) -> None:
        """2 处 evidence 只检到 1 处 → recall = 0.5（手算可复核）。"""
        rep = self._run(
            [
                _item(
                    "t1",
                    "MULTI transaction",
                    [Evidence("multi", "overview"), Evidence("get", "overview")],
                )
            ]
        )
        self.assertAlmostEqual(rep.items[0].recall, 0.5, places=6)

    def test_aggregate_is_the_mean(self) -> None:
        rep = self._run(
            [
                _item("t1", "MULTI transaction EXEC", [Evidence("multi", "overview")]),
                _item("t2", "zzzz qqqq", [Evidence("get", "overview")]),
            ]
        )
        self.assertEqual(rep.overall.n, 2)
        self.assertAlmostEqual(rep.overall.recall, 0.5, places=6)
        self.assertAlmostEqual(rep.overall.hit_rate, 0.5, places=6)

    def test_by_difficulty_split(self) -> None:
        rep = self._run(
            [
                _item("t1", "MULTI transaction EXEC", [Evidence("multi", "overview")], difficulty="simple"),
                _item("t2", "zzzz qqqq", [Evidence("get", "overview")], difficulty="hard"),
            ]
        )
        self.assertEqual(rep.by_difficulty["simple"].hit_rate, 1.0)
        self.assertEqual(rep.by_difficulty["hard"].hit_rate, 0.0)


class TestRecallCeiling(unittest.TestCase):
    """一个数字的**分母是谁划的**，必须能被读出来。"""

    def _run(self, items, *, top_k, chunks=_TRAP):
        ds = Dataset(topic="redis", items=tuple(items))
        ds.resolve(chunks)
        from asuka.kb import KnowledgeBase
        from packages.agent_context.retrieval import RetrievalPipeline

        r = _kb(chunks)
        kb = KnowledgeBase(
            topic="redis",
            pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
            retriever=r,
            kind="bm25",
        )
        return evaluate_retrieval(kb, ds, top_k=top_k, corpus_chunks=len(chunks))

    def test_ceiling_is_one_when_evidence_fits(self) -> None:
        rep = self._run(
            [_item("t1", "MULTI", [Evidence("multi", "overview")])], top_k=5
        )
        self.assertEqual(rep.overall.recall_ceiling, 1.0)
        self.assertEqual(rep.overall.items_over_topk, 0)

    def test_ceiling_drops_when_evidence_exceeds_top_k(self) -> None:
        """3 处 evidence、top_k=1 → 上限 1/3。**不可能**到 1.0。"""
        rep = self._run(
            [
                _item(
                    "t1",
                    "MULTI GET",
                    [
                        Evidence("multi", "overview"),
                        Evidence("get", "overview"),
                        Evidence("blpop", "overview"),
                    ],
                )
            ],
            top_k=1,
        )
        self.assertAlmostEqual(rep.overall.recall_ceiling, 1 / 3, places=6)
        self.assertEqual(rep.overall.items_over_topk, 1)

    def test_ceiling_is_reported_in_dict(self) -> None:
        rep = self._run(
            [_item("t1", "MULTI", [Evidence("multi", "overview")])], top_k=1
        )
        self.assertIn("context_recall_ceiling", rep.overall.as_dict())

    def test_markdown_warns_about_the_ceiling(self) -> None:
        rep = self._run(
            [
                _item(
                    "t1",
                    "MULTI GET",
                    [
                        Evidence("multi", "overview"),
                        Evidence("get", "overview"),
                        Evidence("blpop", "overview"),
                    ],
                )
            ],
            top_k=1,
        )
        md = render_markdown(rep)
        self.assertIn("上限", md)
        self.assertIn("不可能", md)

    def test_markdown_lists_total_misses(self) -> None:
        rep = self._run(
            [_item("t1", "zzzz qqqq", [Evidence("get", "overview")])], top_k=3
        )
        md = render_markdown(rep)
        self.assertIn("完全没检到", md)
        self.assertIn("t1", md)


class TestPreconditions(unittest.TestCase):
    def test_unresolved_dataset_is_rejected(self) -> None:
        """没有 ground truth 就没有分母 —— 必须拒绝，不能算出一个看着像真的数。"""
        ds = Dataset(topic="redis", items=(_item("t1", "MULTI", [Evidence("multi", "")]),))
        from asuka.kb import KnowledgeBase
        from packages.agent_context.retrieval import RetrievalPipeline

        r = _kb(_TRAP)
        kb = KnowledgeBase(
            topic="redis",
            pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
            retriever=r,
            kind="bm25",
        )
        with self.assertRaises(ValueError) as ctx:
            evaluate_retrieval(kb, ds, top_k=3)
        self.assertIn("resolve", str(ctx.exception))


@unittest.skipUnless(_CORPUS.exists() and (_DATASETS / "redis.jsonl").exists(), "没有语料/任务集")
class TestRealCorpusRegression(unittest.TestCase):
    """在**真实语料**上钉住停用词那个失效模式。"""

    @classmethod
    def setUpClass(cls) -> None:
        from asuka.corpus import read_chunks
        from asuka.dataset import load_dataset

        cls.chunks = read_chunks(_CORPUS)
        cls.ds = load_dataset(_DATASETS, "redis")
        cls.ds.resolve(cls.chunks)
        cls.index = BM25Index(tuple(cls.chunks))

    def test_multi_query_finds_multi_doc(self) -> None:
        """修前 `multi:001` 排第 7 —— 这条测试就是那个回归的哨兵。"""
        hits = self.index.search(tokenize("What is MULTI used for in Redis?"), limit=5)
        ids = [h.chunk_id for h in hits]
        self.assertIn(
            "redis:multi:001",
            ids,
            f"MULTI 的问题没把 MULTI 文档检进前 5：{ids}",
        )

    def test_question_shaped_heading_does_not_dominate(self) -> None:
        """问句标题那片（blpop:006）不该靠 `what` 霸榜。"""
        hits = self.index.search(tokenize("What is MULTI used for in Redis?"), limit=1)
        self.assertNotEqual(hits[0].chunk_id, "redis:blpop:006")

    def test_bm25_beats_random_by_a_wide_margin(self) -> None:
        """词法基线至少要**明显**好于随机 —— 否则这个"对照组"没有意义。"""
        from asuka.kb import KnowledgeBase
        from packages.agent_context.retrieval import RetrievalPipeline

        r = BM25Retriever(tuple(self.chunks))
        kb = KnowledgeBase(
            topic="redis",
            pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
            retriever=r,
            kind="bm25",
        )
        rep = evaluate_retrieval(kb, self.ds, top_k=10, corpus_chunks=len(self.chunks))
        self.assertGreater(rep.overall.hit_rate, 0.5, "hit_rate 太低，多半是检索坏了")
        # 难度单调性：简单题的命中率不该低于困难题
        self.assertGreaterEqual(
            rep.by_difficulty["simple"].hit_rate,
            rep.by_difficulty["hard"].hit_rate,
        )


class TestEvaluateCliIsStrictByDefault(unittest.TestCase):
    """CLI 的**默认值**也是契约。

    `--allow-non-semantic` 一旦默认 True，就等于**默认允许**用不承载语义的索引
    去评检索质量 —— 分数会好看，且毫无意义。这种默认值必须钉住。
    """

    def test_non_semantic_is_off_by_default(self) -> None:
        args = build_parser().parse_args(["redis"])
        self.assertFalse(
            args.allow_non_semantic,
            "默认必须是 False：冒烟要显式说出口，不能是默认",
        )

    def test_opt_in_flag_actually_works(self) -> None:
        args = build_parser().parse_args(["redis", "--allow-non-semantic"])
        self.assertTrue(args.allow_non_semantic)

    def test_retriever_defaults_to_bm25(self) -> None:
        """默认是零依赖的词法基线 —— 它不需要 Qdrant、不需要权重。"""
        self.assertEqual(build_parser().parse_args([]).retriever, "bm25")

    def test_dense_path_can_name_a_local_model(self) -> None:
        """本地权重这条路必须能从 CLI 选中（`auto` 需要 API key，选不到它）。"""
        args = build_parser().parse_args(
            ["redis", "--retriever", "dense", "--embedder", "local",
             "--model-path", ".asuka-models/bge-m3"]
        )
        self.assertEqual(args.embedder, "local")
        self.assertEqual(args.model_path, ".asuka-models/bge-m3")


if __name__ == "__main__":
    unittest.main()
