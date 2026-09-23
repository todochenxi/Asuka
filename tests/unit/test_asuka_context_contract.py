"""Context 装配的契约 —— C-1 / C-3 / C-4 在 Asuka 里的落点。

--------------------------------------------------------------------------
这一步为什么必须有：`top_k` 是**条数**，不是**窗口占用**

`kb.search(limit=10)` 说的是"给我 10 片"。10 片是多少 token？不知道。
而模型的窗口是按 token 算的 —— 10 片长文档可以轻松超过 8192。
**一个按条数控制的检索器会静默地把请求撑爆**，而失败发生在模型那一侧
（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧：评测跑得好好的，线上全崩。

所以这份测试盯的是四件事，每一件都对应一个"读起来像没事"的失效：

**一、C-1：跨过 `Chunk → ContextItem` 这条边**
    `Chunk` 有 `.chunk_id`，`ContextItem` 有 `.key`。混用会在运行期抛
    `AttributeError` —— 但那是**撞上的**，不是被守住的。

**二、C-3：预算装不下就丢，不许超**
    "差不多装得下"是不可接受的：低估比高估危险得多（超窗口 ⇒ 调用彻底失败）。

**三、C-4：静默截断是 bug**
    每条被丢的都要留下 `(chunk_id, tokens, 原因)`。
    "模型当时没看见这一片"必须能被事后回答。

**四、取舍顺序按相关性，不按 `chunk_id` 字母序**
    `knowledge_chunk()` 给所有知识片的 `priority` 是**同一个常数**，
    于是内核 `allocate()` 的排序键 `(not pinned, -priority, key)` 退化成按
    `key` 字母序 —— 名次与字母序**反向**时会丢掉第一名、留下最后一名，
    而且**静默**：分数照出，只是低了一点。
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from packages.agent_context.items import ContextItem
from packages.agent_context.retrieval import Chunk, RetrievalPipeline, RetrievalResult
from packages.agent_context.tokens import HeuristicTokenizer

from asuka.answers import (
    Answer,
    OracleAnswerer,
    build_parser,
    evaluate_answers,
    render_markdown,
)
from asuka.context import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_RESERVED_FOR_OUTPUT,
    AssembledContext,
    assemble,
)
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter
from asuka.textutil import CHARS_PER_TOKEN, estimate_tokens
from asuka.trace import Event, RunIdentity, Trace, TraceError


# ---------------------------------------------------------------- 脚手架


def _chunk(cid: str, text: str, unit: str, visibility: str = "public") -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · overview",
        attributes={"unit_id": unit, "section": "overview", "visibility": visibility},
    )


#: 两片短文档 —— 默认预算下**一片都丢不掉**，所以"丢"这件事必须显式构造。
_CHUNKS = (
    _chunk("redis:expire:000", "# EXPIRE\n\nSets a timeout on a key.", "expire"),
    _chunk("redis:ttl:000", "# TTL\n\nReturns the remaining time to live.", "ttl"),
)

#: 一片**装不进任何小预算**的长文档。4000 字符 ÷ 4 ≈ 1000 tokens。
_LONG = _chunk("redis:expire:000", "# EXPIRE\n\n" + ("x" * 4000), "expire")


def _two_matching() -> tuple[Chunk, ...]:
    """两片**都**能被检索到 —— 名次 1 的长到装不下，名次 2 的短。

    ⚠️ 必须让两片都命中问句。`_CHUNKS` 里 `redis:ttl:000` 对
    `What does EXPIRE do?` 的 BM25 得分是 **0**，压根不进 `kept` ——
    只命中一片的话，"丢"这件事**根本构造不出来**（实测 `kept` 只有 1 片）。
    """
    return (
        _chunk("redis:expire:000", "# EXPIRE\n\n" + ("EXPIRE timeout. " * 120), "expire"),
        _chunk("redis:ttl:000", "# TTL\n\nEXPIRE sets the TTL of a key.", "ttl"),
    )


#: 让"装不下"必然发生的窗口：名次 1 约 483 tokens，名次 2 约 9 tokens。
_TIGHT = 100


def _item(task_id: str = "t-1", **over) -> TaskItem:
    base = dict(
        task_id=task_id,
        question="What does EXPIRE do?",
        reference_answer="EXPIRE sets a timeout on a key, so the key is deleted.",
        source_document="redis:expire",
        difficulty="simple",
        evidence=(Evidence("expire", "overview"),),
        required_points=(
            RequiredPoint("设超时", ("sets a timeout",)),
            RequiredPoint("到期删除", ("deleted",)),
        ),
    )
    base.update(over)
    return TaskItem(**base)  # type: ignore[arg-type]


def _kb(chunks=_CHUNKS) -> KnowledgeBase:
    r = BM25Retriever(tuple(chunks))
    return KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
        retriever=r,
        kind="bm25",
    )


def _dataset(*items: TaskItem, chunks=_CHUNKS) -> Dataset:
    ds = Dataset(topic="redis", items=items or (_item(),))
    ds.resolve(chunks)
    return ds


def _identity(**over) -> RunIdentity:
    base = dict(
        topic="redis",
        retriever="bm25",
        top_k=5,
        samples_per_task=1,
        corpus_chunks=len(_CHUNKS),
        answerer="oracle",
    )
    base.update(over)
    return RunIdentity(**base)  # type: ignore[arg-type]


class _RecordingAnswerer:
    """把收到的 `contexts` 原样记下来 —— 用来验 C-1 那条边的**类型**。"""

    name = "recording"
    is_calibration = False

    def __init__(self) -> None:
        self.seen: list[object] = []

    def answer(self, item: TaskItem, contexts) -> Answer:  # type: ignore[no-untyped-def]
        self.seen = list(contexts)
        return Answer(text=item.reference_answer, citations=tuple(i.key for i in contexts))


class _CitesGiven:
    """引**指定的一批 id**，不管检索给没给它 —— 用来踩"编造"那条路径。"""

    name = "cites-given"
    is_calibration = False

    def __init__(self, ids: tuple[str, ...]) -> None:
        self.ids = ids

    def answer(self, item: TaskItem, contexts) -> Answer:  # type: ignore[no-untyped-def]
        return Answer(text=item.reference_answer, citations=self.ids)


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


# ---------------------------------------------------------------- C-1 转换


class TestChunkToContextItem(unittest.TestCase):
    """跨过 `Chunk → ContextItem` 这条边之后，下游看到的必须是后者。"""

    def test_the_three_identity_fields_survive_the_conversion(self) -> None:
        """`chunk_id → key`、`text → text`、`citation → reference`。

        任何一条断了，引用判据就对不上号 —— 而它对不上号的方式是
        **静默地全都算编造**（`key` 不在 `available` 里）。
        """
        kb = _kb()
        result = kb.search("What does EXPIRE do?", limit=5)
        a = assemble(kb.pipeline, result)
        self.assertTrue(a.items)
        by_key = {i.key: i for i in a.items}
        for c in result.kept:
            item = by_key[c.chunk_id]
            self.assertEqual(item.text, c.text)
            self.assertEqual(item.reference, c.citation)
            self.assertEqual(item.attributes["score"], c.score)

    def test_the_answerer_receives_context_items_not_chunks(self) -> None:
        """C-1 的落点。`Chunk` 有 `.chunk_id`；`ContextItem` 有 `.key`。

        混用会在运行期抛 `AttributeError` —— 但那是**撞上的**，不是被守住的。
        （真踩过：装配接上之后 `OracleAnswerer` 还在读 `c.chunk_id`。）
        """
        probe = _RecordingAnswerer()
        evaluate_answers(_kb(), _dataset(), probe, top_k=5)
        self.assertTrue(probe.seen)
        for c in probe.seen:
            self.assertIsInstance(c, ContextItem)
            self.assertFalse(hasattr(c, "chunk_id"))

    def test_knowledge_items_still_carry_a_citation(self) -> None:
        """C-10：进 Context 的知识片必须带引用，否则事后答不出"这句从哪来的"。"""
        kb = _kb()
        a = assemble(kb.pipeline, kb.search("What does EXPIRE do?", limit=5))
        self.assertTrue(all(i.reference for i in a.items))


# ---------------------------------------------------------------- C-3 预算


class TestTokenBudgetIsHard(unittest.TestCase):
    def test_reserved_for_output_is_not_available(self) -> None:
        """留给输出的那部分**不能**被上下文吃掉。

        吃掉的话会出现"上下文刚好塞满窗口，模型一个字都吐不出来"——
        那在很多服务端是 `CONTEXT_LENGTH_EXCEEDED`，不是"输出被截断"。
        """
        kb = _kb()
        a = assemble(
            kb.pipeline,
            kb.search("What does EXPIRE do?", limit=5),
            budget=1000,
            reserved_for_output=400,
        )
        self.assertEqual(a.budget_total, 1000)
        self.assertEqual(a.budget_available, 600)

    def test_nothing_exceeds_the_budget_at_any_size(self) -> None:
        """扫一串预算，**没有一个**能超 —— 这是"硬约束"的意思。"""
        kb = _kb()
        result = kb.search("What does EXPIRE do?", limit=5)
        for budget in (1, 2, 5, 9, 10, 11, 50, 200, 8192):
            with self.subTest(budget=budget):
                a = assemble(kb.pipeline, result, budget=budget, reserved_for_output=0)
                self.assertLessEqual(a.total_tokens, a.budget_available)

    def test_a_budget_that_cannot_fit_anything_drops_everything(self) -> None:
        """装不下就**全丢**，不截断、也不"差不多放进去"。"""
        kb = _kb()
        a = assemble(
            kb.pipeline, kb.search("What does EXPIRE do?", limit=5),
            budget=3, reserved_for_output=0,
        )
        self.assertEqual(a.items, ())
        self.assertEqual(a.total_tokens, 0)
        self.assertTrue(a.dropped)

    def test_a_chunk_bigger_than_the_whole_window_is_dropped_whole(self) -> None:
        """超窗口的片**整片丢**，不切一半塞进去。

        截断会让模型看到半片内容，而"它到底看到了多少"事后无法回答 ——
        C-4 的另一面。
        """
        kb = _kb((_LONG, _chunk("redis:ttl:000", "# TTL\n\nRemaining time.", "ttl")))
        a = assemble(
            kb.pipeline, kb.search("What does EXPIRE do?", limit=5),
            budget=100, reserved_for_output=0,
        )
        self.assertIn("redis:expire:000", a.dropped_ids)
        self.assertNotIn("redis:expire:000", a.chunk_ids)
        # 被丢的片**一个 token 都没进**占用 —— 不是"进去了一部分"。
        self.assertLessEqual(a.total_tokens, a.budget_available)
        self.assertTrue(all(i.key != "redis:expire:000" for i in a.items))

    def test_the_default_budget_drops_nothing_on_a_small_corpus(self) -> None:
        kb = _kb()
        a = assemble(kb.pipeline, kb.search("What does EXPIRE do?", limit=5))
        self.assertEqual(a.dropped, ())
        self.assertEqual(a.budget_total, DEFAULT_CONTEXT_BUDGET)
        self.assertEqual(
            a.budget_available, DEFAULT_CONTEXT_BUDGET - DEFAULT_RESERVED_FOR_OUTPUT
        )


# ---------------------------------------------------------------- C-4 留痕


class TestDroppedItemsAreRecorded(unittest.TestCase):
    def _tight(self, budget: int = _TIGHT) -> AssembledContext:
        chunks = _two_matching()
        kb = _kb(chunks)
        return assemble(
            kb.pipeline, kb.search("What does EXPIRE do?", limit=5),
            budget=budget, reserved_for_output=0,
        )

    def test_every_dropped_item_carries_id_tokens_and_a_reason(self) -> None:
        """C-4：静默截断是 bug。三条信息缺一不可 ——

        没有 id 就不知道丢的是谁；没有 tokens 就不知道值不值得为它加预算；
        没有原因就不知道是"塞不进剩下的空间"还是别的。
        """
        a = self._tight()
        self.assertTrue(a.dropped)
        for cid, toks, reason in a.dropped_reasons:
            with self.subTest(chunk=cid):
                self.assertTrue(cid)
                self.assertGreater(toks, 0)
                self.assertTrue(reason)

    def test_kept_and_dropped_partition_what_retrieval_found(self) -> None:
        """`kept ⊎ dropped` 必须**恰好**等于检到的那些。

        少一个 ⇒ 有片**无声消失**了（模型没看见，而报告里没有它）；
        多一个 ⇒ 凭空多出没检到的东西。两种都不会报错。
        """
        chunks = _two_matching()
        kb = _kb(chunks)
        result = kb.search("What does EXPIRE do?", limit=5)
        a = assemble(kb.pipeline, result, budget=_TIGHT, reserved_for_output=0)
        self.assertTrue(a.dropped, "这个窗口本来应该装不下名次 1 的那片")
        self.assertEqual(set(a.chunk_ids) & set(a.dropped_ids), set())
        self.assertEqual(
            set(a.chunk_ids) | set(a.dropped_ids),
            {c.chunk_id for c in result.kept},
        )

    def test_dropped_tokens_are_not_counted_as_used(self) -> None:
        """被丢的片的 token **一个都不进**占用 —— 不是"进去了一部分"。"""
        a = self._tight()
        kept_tokens = sum(i.tokens(HeuristicTokenizer(CHARS_PER_TOKEN)) for i in a.items)
        self.assertEqual(a.total_tokens, kept_tokens)
        self.assertGreater(a.dropped_tokens, 0)
        self.assertLess(a.total_tokens, a.dropped_tokens)

    def test_context_size_counts_what_was_fed_in_not_what_was_retrieved(self) -> None:
        """`context_size` 必须是**喂进去的片数**，不是检索条数。

        ⚠️ 这两者在"一片没丢"时**相等** —— 所以只有构造一次真丢片才测得出来。
        （变红验证抓到的缺口：N18 把 `context_size` 换成 `len(result.kept)`，
        18 条变异里唯一没红的就是它 —— 没有任何用例在丢片场景下看这个字段。）
        多报会让读者以为窗口装得比实际满："10 片"vs 实际 1 片。
        """
        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()

        retrieved = len(t.retrieval_kept()["t-1"])
        fed_in = len(t.retrieval_context()["t-1"])
        self.assertEqual(fed_in, 1)
        self.assertLess(fed_in, retrieved)          # 这个场景**必须**真的丢了东西

        row = rep.items[0]
        self.assertEqual(row.context_size, fed_in)
        self.assertLess(row.context_size, retrieved)
        self.assertEqual(rep.overall.context_size, float(fed_in))
        # 报告里印的也是这个数 —— 字段对了但没印出来，读的人还是被误导。
        self.assertIn(f"· {float(fed_in):.1f} 片 ·", render_markdown(rep))

    def test_dropped_reasons_land_in_the_report_and_in_the_trace(self) -> None:
        """装配的留痕要**穿到两个产物**去，否则它只活在这一次调用里。"""
        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()

        row = rep.items[0]
        self.assertTrue(row.context_dropped)
        self.assertGreater(row.context_dropped_tokens, 0)
        self.assertEqual(row.context_dropped, t.context_dropped()["t-1"])
        self.assertEqual(rep.overall.context_dropped_total, len(row.context_dropped))
        self.assertEqual(rep.overall.samples_with_context_drops, 1)


# ---------------------------------------------------------------- 取舍顺序


class TestRelevanceDecidesWhoGetsDropped(unittest.TestCase):
    """装不下时丢谁，必须由**相关性**决定，不是 `chunk_id` 的字母序。"""

    def _three_equal(self) -> tuple[RetrievalPipeline, RetrievalResult]:
        """三片等长，名次与字母序**反向**：rank1 的 id 排在最后。"""
        kb = _kb()
        rank1 = _chunk("zzz:rank1:000", "x" * 400, "rank1")
        rank2 = _chunk("mmm:rank2:000", "x" * 400, "rank2")
        rank3 = _chunk("aaa:rank3:000", "x" * 400, "rank3")
        return kb.pipeline, RetrievalResult(kept=(rank1, rank2, rank3))

    def test_priority_is_the_rank_not_the_chunk_id(self) -> None:
        """预算只够两片，而名次与字母序反向。

        字母序会留下 `aaa`（rank3）、丢掉 `zzz`（rank1）—— **静默**：
        分数照出，只是低了一点。这条红了就说明 `priority` 又被写回常数了。
        """
        pipeline, result = self._three_equal()
        a = assemble(pipeline, result, budget=200, reserved_for_output=0)
        self.assertEqual(a.chunk_ids, ("zzz:rank1:000", "mmm:rank2:000"))
        self.assertEqual(a.dropped_ids, ("aaa:rank3:000",))

    def test_priority_rewriting_is_visible_in_the_item_itself(self) -> None:
        """名次写进了 `priority`（越大越重要）—— 用的是内核给的旋钮，不是绕过它。"""
        pipeline, result = self._three_equal()
        a = assemble(pipeline, result, budget=200, reserved_for_output=0)
        self.assertEqual([i.priority for i in a.items], sorted(
            (i.priority for i in a.items), reverse=True
        ))
        self.assertGreater(a.items[0].priority, a.items[-1].priority)

    def test_the_kernel_default_priority_is_a_constant(self) -> None:
        """这条钉的是**前提**：内核 `knowledge_chunk()` 给的 priority 全都一样。

        哪天内核改成"按 score 发 priority"，`asuka/context.py` 里那段改写
        就成了重复劳动 —— 这条会红，提醒去删掉它（而不是留两份互相漂移的定义）。
        """
        from packages.agent_context.items import knowledge_chunk

        prios = {knowledge_chunk(f"c{i}", "t", citation="ref").priority for i in range(5)}
        self.assertEqual(len(prios), 1)

    def test_ranking_is_stable_and_deterministic(self) -> None:
        """同样的输入必须得到同样的输出 —— 否则"模型当时看到了什么"没有答案。"""
        pipeline, result = self._three_equal()
        first = assemble(pipeline, result, budget=200, reserved_for_output=0)
        second = assemble(pipeline, result, budget=200, reserved_for_output=0)
        self.assertEqual(first.chunk_ids, second.chunk_ids)
        self.assertEqual(first.dropped_ids, second.dropped_ids)


# ---------------------------------------------------------------- 两个集合


class TestRetrievedVersusAvailable(unittest.TestCase):
    """`retrieved`（检到了）与 `available`（模型看见了）是**两个**集合。"""

    def _dropped_evidence_run(self, **kw):
        """构造：该引的依据**检到了但装不进预算**。"""
        chunks = _two_matching()
        return evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            context_budget=_TIGHT, reserved_for_output=0, **kw
        )

    def test_evidence_that_did_not_fit_is_blamed_on_the_budget(self) -> None:
        """`evidence_dropped` 有、`evidence_not_retrieved` 空 —— 归因指向**预算**。

        合成一个集合的话，报告会让人去换检索器 —— 而该动的是窗口或 top_k。
        """
        rep = self._dropped_evidence_run()
        row = rep.items[0]
        self.assertEqual(row.citation.evidence_not_retrieved, ())
        self.assertIn("redis:expire:000", row.citation.evidence_dropped)

    def test_citing_a_budget_dropped_chunk_is_fabrication(self) -> None:
        """**这条是 `available` 必须用装配后 id 的端到端证明。**

        被预算丢掉的片模型**没看见** —— 引用了它就是编造。
        用 `retrieved` 当 `available` 的话，这条会被读成"有依据"，
        而分数只会变好看，没有任何东西会报错。
        """
        chunks = _two_matching()
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks),
            _CitesGiven(("redis:expire:000",)), top_k=5,
            context_budget=_TIGHT, reserved_for_output=0,
        )
        row = rep.items[0]
        self.assertIn("redis:expire:000", row.fabricated)
        self.assertEqual(row.grounded_rate, 0.0)

    def test_the_same_citation_is_grounded_with_a_bigger_window(self) -> None:
        """同一句话、同一次引用，窗口够大就是**有依据**的。

        没有这条对照，上面那条可能是"引用判据本来就全算编造"——
        那样它证明不了任何事。
        """
        chunks = _two_matching()
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks),
            _CitesGiven(("redis:expire:000",)), top_k=5,
            context_budget=8192, reserved_for_output=0,
        )
        row = rep.items[0]
        self.assertEqual(row.fabricated, ())
        self.assertEqual(row.grounded_rate, 1.0)

    def test_the_retrieval_event_records_both_sets(self) -> None:
        """trace 里两个字段名不同、含义不同 —— 混成一个就分不清"检到"和"看见"。"""
        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()
        ev = next(e for e in t.events if e.kind == "retrieval")
        self.assertIn("redis:expire:000", ev.data["kept"])
        self.assertNotIn("redis:expire:000", ev.data["context"])
        self.assertIn("redis:ttl:000", ev.data["context"])
        self.assertEqual(
            [d[0] for d in ev.data["dropped_budget"]], ["redis:expire:000"]
        )

    def test_denied_chunks_never_reach_the_context(self) -> None:
        """C-9：被权限拒绝的片连 `retrieved` 都进不去，更进不了 `context`。"""
        private = _chunk(
            "redis:expire:001", "# EXPIRE internals", "expire", visibility="private"
        )
        chunks = _CHUNKS + (private,)
        kb = _kb(chunks)
        result = kb.search("What does EXPIRE do?", limit=5)
        a = assemble(kb.pipeline, result)
        self.assertNotIn("redis:expire:001", a.chunk_ids)
        self.assertNotIn("redis:expire:001", {c.chunk_id for c in result.kept})


# ---------------------------------------------------------------- tokenizer


class TestTokenizerAssumptionIsDeclared(unittest.TestCase):
    """`chars_per_token` 是**语料的属性**，必须显式声明并印出来。"""

    def test_estimate_tokens_delegates_to_the_kernel_tokenizer(self) -> None:
        """同一族算法**只实现一处**。

        两处各写一遍，改了一处就会出现"语料清单说 120 token、装配说 160 token"，
        而**两边都不报错**。
        """
        for text in ("", "a", "hello world", "x" * 399, "x" * 400):
            for cpt in (3, 4):
                with self.subTest(text=len(text), cpt=cpt):
                    self.assertEqual(
                        estimate_tokens(text, chars_per_token=cpt),
                        HeuristicTokenizer(cpt).count(text),
                    )

    def test_the_two_assumptions_differ_by_a_third(self) -> None:
        """4（英文）与 3（中文保守）差 33% —— 这个差必须**看得见**，不是"差不多"。"""
        self.assertEqual(estimate_tokens("x" * 300, chars_per_token=4), 75)
        self.assertEqual(estimate_tokens("x" * 300, chars_per_token=3), 100)
        self.assertAlmostEqual(100 / 75, 4 / 3, places=6)

    def test_the_corpus_layer_and_the_assembly_layer_agree(self) -> None:
        """语料层（默认 `CHARS_PER_TOKEN`）与装配层算出的 token 数必须一致。

        不一致时没人能解释"预算为什么在这里就满了"—— 因为两个数都自称是 token 数。
        """
        kb = _kb()
        a = assemble(kb.pipeline, kb.search("What does EXPIRE do?", limit=5))
        self.assertEqual(
            a.total_tokens, sum(estimate_tokens(i.text) for i in a.items)
        )

    def test_a_different_assumption_changes_the_result_visibly(self) -> None:
        """假设换了，结果就得跟着换，而且要能**被读出来**。"""
        kb = _kb()
        result = kb.search("What does EXPIRE do?", limit=5)
        a4 = assemble(kb.pipeline, result, chars_per_token=4)
        a3 = assemble(kb.pipeline, result, chars_per_token=3)
        self.assertGreater(a3.total_tokens, a4.total_tokens)
        self.assertEqual((a3.chars_per_token, a4.chars_per_token), (3, 4))

    def test_the_assumption_lands_in_the_report_and_the_markdown(self) -> None:
        rep = evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5)
        self.assertEqual(rep.chars_per_token, CHARS_PER_TOKEN)
        self.assertEqual(rep.context_budget, DEFAULT_CONTEXT_BUDGET)
        md = render_markdown(rep)
        self.assertIn(f"每 token {CHARS_PER_TOKEN} 字符", md)


# ---------------------------------------------------------------- 报告与 CLI


class TestContextIsVisibleInTheReport(unittest.TestCase):
    def test_the_context_section_states_window_usage_and_drops(self) -> None:
        md = render_markdown(evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5))
        self.assertIn("## 上下文（Context）", md)
        self.assertIn(f"- 窗口 {DEFAULT_CONTEXT_BUDGET} tokens", md)
        self.assertIn(f"可放 **{DEFAULT_CONTEXT_BUDGET - DEFAULT_RESERVED_FOR_OUTPUT}**", md)
        self.assertIn("被预算丢掉 **0** 片", md)

    def test_the_report_says_why_top_k_is_not_enough(self) -> None:
        """读者最容易犯的错是"我把 top_k 调小就不会超了"——
        报告必须说清 `top_k` 是条数、窗口是 token。"""
        md = render_markdown(evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5))
        self.assertIn("`top_k` 是**条数**，不是窗口占用", md)

    def test_dropped_chunks_are_listed_with_their_reason(self) -> None:
        chunks = _two_matching()
        md = render_markdown(
            evaluate_answers(
                _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
                context_budget=_TIGHT, reserved_for_output=0,
            )
        )
        self.assertIn("### 被预算丢掉的片", md)
        self.assertIn("`redis:expire:000`", md)
        self.assertIn("token budget exhausted", md)

    def test_an_empty_drop_list_is_not_printed(self) -> None:
        """没丢就**不要**印一个空小节 —— 空表格读起来像"这一项没测"。"""
        md = render_markdown(evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5))
        self.assertNotIn("### 被预算丢掉的片", md)

    def test_the_per_task_table_has_a_context_column(self) -> None:
        md = render_markdown(evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5))
        self.assertIn("| ctx |", md)
        self.assertIn("`ctx` = 喂进去的上下文 token（受预算约束）", md)

    def test_old_reports_without_context_tokens_are_refused(self) -> None:
        """缺 `context_tokens` 键 = **旧格式**。

        按 0 读会得到"这次一片上下文都没喂"这个假结论。
        """
        from asuka.answers import AnswerScore

        rep = evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=5)
        d = rep.as_dict()
        del d["items"][0]["context_tokens"]
        with self.assertRaises(ValueError) as cm:
            type(rep).from_dict(d, verify=False)
        self.assertIn("context_tokens", str(cm.exception))


class TestContextCli(unittest.TestCase):
    def test_both_clis_expose_the_assembly_knobs(self) -> None:
        """装配参数**必须能从命令行改** —— 窗口是"这次拿什么模型跑"的属性。"""
        from asuka.trace import build_parser as trace_parser

        for parser in (build_parser(), trace_parser()):
            with self.subTest(parser=parser.prog):
                a = parser.parse_args([])
                self.assertEqual(a.context_budget, DEFAULT_CONTEXT_BUDGET)
                self.assertEqual(a.reserved_for_output, DEFAULT_RESERVED_FOR_OUTPUT)
                self.assertEqual(a.chars_per_token, CHARS_PER_TOKEN)

    def test_the_budget_can_be_tightened_from_the_command_line(self) -> None:
        a = build_parser().parse_args(
            ["--context-budget", "400", "--reserved-for-output", "0", "--chars-per-token", "3"]
        )
        self.assertEqual(a.context_budget, 400)
        self.assertEqual(a.reserved_for_output, 0)
        self.assertEqual(a.chars_per_token, 3)

    def test_the_help_text_does_not_leak_a_raw_percent_escape(self) -> None:
        """argparse 的 help **不做插值** —— `33%%` 会原样印出来。

        这是踩过的：`%%` 是 `%` 格式化的转义，而 argparse 走的是 `str.format`
        那一套，写 `%%` 会印成 `%%`。
        """
        for parser in (build_parser(),):
            for action in parser._actions:
                if action.dest == "chars_per_token":
                    self.assertIn("33%", action.help)
                    self.assertNotIn("%%", action.help)


# ---------------------------------------------------------------- trace


class TestContextInTheTrace(unittest.TestCase):
    def _run(self, *, budget: int = DEFAULT_CONTEXT_BUDGET, reserved: int = 0, chunks=_CHUNKS):
        t = Trace.start(
            _identity(context_budget=budget, reserved_for_output=reserved)
        )
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            trace=t, context_budget=budget, reserved_for_output=reserved,
        )
        t.finish()
        return t, rep

    def test_identity_carries_the_assembly_parameters(self) -> None:
        """⚠️ 窗口也是**输入的同一性**。

        "丢了 3 片"脱离"窗口多大"无法解释；两条窗口不同的 trace
        放一起比引用指标就是在比两件事。
        """
        t, _ = self._run(budget=400, reserved=100)
        self.assertEqual(t.identity.context_budget, 400)
        self.assertEqual(t.identity.reserved_for_output, 100)
        self.assertEqual(t.identity.chars_per_token, CHARS_PER_TOKEN)
        ev = t.events[0]
        for field in ("context_budget", "reserved_for_output", "chars_per_token"):
            self.assertIn(field, ev.data)

    def test_a_run_started_without_the_budget_is_refused(self) -> None:
        """`run.started` 缺装配字段 = 旧格式，必须被点名。"""
        t = Trace.start(_identity())
        t.events[0].data.pop("context_budget")
        t.finish()
        with self.assertRaises(TraceError) as cm:
            t.verify()
        self.assertIn("context_budget", str(cm.exception))

    def test_the_report_and_the_identity_must_agree_on_the_budget(self) -> None:
        """两处各写一个数 ⇒ "丢了 3 片"和"窗口 8192"可能不是同一次跑出来的，
        而**没有任何东西会报错** —— 报告读起来仍然自洽。"""
        t, rep = self._run()
        bad = dataclasses.replace(rep, context_budget=1234)
        with self.assertRaises(TraceError) as cm:
            t.verify_against(bad)
        self.assertIn("context_budget", str(cm.exception))

    def test_cross_check_recomputes_fabrication_from_context_not_kept(self) -> None:
        """⚠️ 对减的右边是 `context`。

        用 `kept` 的话，被预算丢掉的片会被算成"给过它" —— 编造读成有依据，
        方向**只会让分数变好看**。
        """
        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        rep = evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks),
            _CitesGiven(("redis:expire:000",)), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()
        t.verify_against(rep)          # 不抛就算过
        self.assertGreater(rep.overall.fabricated_total, 0)

        # 假装报告说"没有编造" —— 交叉核对必须抓到。
        bad = dataclasses.replace(
            rep, overall=dataclasses.replace(rep.overall, fabricated_total=0)
        )
        with self.assertRaises(TraceError) as cm:
            t.verify_against(bad)
        self.assertIn("fabricated_total", str(cm.exception))

    def test_explain_shows_what_the_model_did_not_see(self) -> None:
        """审计视图要能回答"这一片它到底看见没有"。

        只列 `kept` 的话，被预算丢掉的那些读起来像"给它了"。
        """
        from asuka.trace import explain

        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks), OracleAnswerer(), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()
        md = explain(t, "t-1")
        self.assertIn("被预算丢掉，模型没看见", md)
        self.assertIn("装配后喂进 prompt", md)
        self.assertIn("token budget exhausted", md)

    def test_explain_blames_the_budget_not_the_retriever(self) -> None:
        """该引的依据"检到了但装不进" ⇒ 记**预算**头上，不是检索头上。"""
        from asuka.trace import explain

        chunks = _two_matching()
        t = Trace.start(_identity(context_budget=_TIGHT, reserved_for_output=0))
        evaluate_answers(
            _kb(chunks), _dataset(chunks=chunks),
            _CitesGiven(()), top_k=5,
            trace=t, context_budget=_TIGHT, reserved_for_output=0,
        )
        t.finish()
        md = explain(t, "t-1")
        self.assertIn("检到了但装不进预算", md)
        self.assertIn("记预算头上", md)

    def test_a_trace_without_the_assembly_fields_is_refused(self) -> None:
        """缺 `context` / `dropped_budget` / `context_tokens` = 旧格式。"""
        t = Trace.start(_identity())
        t.events.append(
            Event(
                kind="retrieval", seq=len(t.events), ts="",
                data={"task_id": "t-1", "query": "q", "kept": [], "latency_ms": 0.0},
            )
        )
        t.finish()
        with self.assertRaises(TraceError) as cm:
            t.verify()
        msg = str(cm.exception)
        for field in ("context", "dropped_budget", "context_tokens"):
            self.assertIn(field, msg)

    def test_a_trace_round_trips_the_assembly_parameters(self) -> None:
        t, _ = self._run(budget=512, reserved=64)
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            back = Trace.load(p)
        self.assertEqual(back.identity, t.identity)
        self.assertEqual(back.identity.context_budget, 512)
        self.assertEqual(back.identity.reserved_for_output, 64)


if __name__ == "__main__":
    unittest.main()
