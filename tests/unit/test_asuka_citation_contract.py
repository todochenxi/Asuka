"""引用（Citation）指标的契约 —— 六指标里的最后一个。

--------------------------------------------------------------------------
它守的核心是一句话：**答对了 ≠ 知识库起作用了**

要点召回只回答"答到了几条"，不回答"凭什么答的"。一个靠参数记忆答对的模型
和一个真读了语料答对的模型，在要点召回上**一模一样**。
对企业知识库来说这两者完全不同 —— 前者意味着**知识库根本没起作用**。

所以这份测试盯的是三件互不替代的事：

  · `fabricated`  —— 引了**没给它**的东西。这是**编造**，是硬判据不是分数
  · `grounded`    —— 引的东西给它了吗（< 1 就是有编的）
  · `依据召回` / `依据用上率` —— 该引的依据引到没有 / 给了它的引了几成

--------------------------------------------------------------------------
三条最容易静默出错的纪律，各有一组测试

**一、`None`（没自述）和 `()`（明确说没引用）是两件事**
    混起来，"没测"会读成"答得没依据"，而报告里没有任何东西会提醒你。

**二、聚合一律**逐样本求均值**，不是 Σ/Σ**
    这里曾经用 Σ/Σ，于是"依据召回 0.2955"和检索报告里**同一个量**的
    `context_recall 0.4424` 对不上 —— 同一个东西两个定义，读者只会以为自己看错了。

**三、编造探测器必须**被证明会响****
    `oracle`（全对）和 `null`（全空）都碰不到"引了没给它的东西"这条路径。
    没有 `fabricator`，`fabricated` 那一栏可能是**一段永远为空的代码**，
    而报告看起来一切正常 —— 所以有个假答案器专门去踩它。
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from packages.agent_context.retrieval import Chunk, RetrievalPipeline

from asuka.answers import (
    Answer,
    AnswerGroup,
    AnswerReport,
    AnswerScore,
    CitationVerdict,
    FabricatingAnswerer,
    NullAnswerer,
    OracleAnswerer,
    _group,
    evaluate_answers,
    render_markdown,
    score_citations,
)
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter
from asuka.trace import RunIdentity, Trace, TraceError


# ---------------------------------------------------------------- 脚手架


def _chunk(cid: str, text: str, unit: str, visibility: str = "public") -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · overview",
        attributes={"unit_id": unit, "section": "overview", "visibility": visibility},
    )


_CHUNKS = (
    _chunk("redis:expire:000", "# EXPIRE\n\nSets a timeout on a key.", "expire"),
    _chunk("redis:ttl:000", "# TTL\n\nReturns the remaining time to live.", "ttl"),
)


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


class _SilentAnswerer:
    """**不自述引用**的答案器 —— 模拟还没接引用契约的真模型。"""

    name = "silent"
    is_calibration = False

    def answer(self, item: TaskItem, contexts) -> Answer:  # type: ignore[no-untyped-def]
        return Answer(text=item.reference_answer)


def _score(
    task_id: str,
    *,
    cited: tuple[str, ...] | None,
    available: tuple[str, ...] = (),
    retrieved: tuple[str, ...] | None = None,
    evidence: tuple[str, ...] = (),
    difficulty: str = "simple",
) -> AnswerScore:
    return AnswerScore(
        task_id=task_id,
        difficulty=difficulty,
        question="q",
        points_total=1,
        hits=("h",),
        citation=score_citations(
            cited,
            available=available,
            # 大多数用例关心的不是"装配丢了没有"，默认成"一片没丢"。
            # ⚠️ 生产代码**不给**这个默认值 —— 见 `score_citations` 的 docstring。
            retrieved=available if retrieved is None else retrieved,
            evidence=evidence,
        ),
    )


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


# ---------------------------------------------------------------- 判据本身


class TestCitationVerdict(unittest.TestCase):
    def test_fabricated_is_cited_minus_available(self) -> None:
        v = score_citations(
            ["a", "b", "ghost"],
            available=["a", "b", "c"],
            retrieved=["a", "b", "c"],
            evidence=["a"],
        )
        self.assertEqual(v.fabricated, ("ghost",))
        self.assertEqual(v.grounded_rate, 2 / 3)

    def test_denied_chunks_are_not_available(self) -> None:
        """**权限拒绝过的 chunk 引用不得算有依据。**

        答案器根本看不到被拒的那些 —— 引用了它们就是编造，
        不能因为"语料里有"就放行。这条靠"只拿装配后的 id 当 available"保证：
        装配的输入是 `result.kept`，被拒的片根本进不去。
        """
        private = _chunk("redis:expire:001", "# EXPIRE internals", "expire", visibility="private")
        chunks = _CHUNKS + (private,)
        ds = _dataset(chunks=chunks)

        class _CiteEverything:
            name = "greedy"
            is_calibration = False

            def answer(self, item, contexts):  # type: ignore[no-untyped-def]
                return Answer(text=item.reference_answer, citations=("redis:expire:001",))

        rep = evaluate_answers(_kb(chunks), ds, _CiteEverything(), top_k=5)
        row = rep.items[0]
        # `redis:expire:001` 语料里**有**，但被权限过滤掉了 ⇒ 没给它 ⇒ 编造。
        self.assertIn("redis:expire:001", row.fabricated)
        self.assertEqual(row.grounded_rate, 0.0)

    def test_evidence_is_partitioned_into_four_disjoint_parts(self) -> None:
        """四段必须**恰好划分** evidence：命中 / 没检到 / 装不下 / 没引。

        不互斥的话，"该引的依据引到没有"这个比例的分母会算重，而分母算重
        只会让分数**看起来低一点**，没有任何东西会报错。

        ⚠️ 四段不是三段：`not_retrieved`（换检索器）和 `dropped`（加窗口）
        是**两件事**，合成一段会把人指向错误的修法。
        """
        v = score_citations(
            ["a", "x"],
            available=["a", "b", "x"],        # `d` 被预算丢掉了
            retrieved=["a", "b", "x", "d"],
            evidence=["a", "b", "c", "x", "d"],
        )
        self.assertEqual(v.evidence_cited, ("a", "x"))
        self.assertEqual(v.evidence_not_retrieved, ("c",))
        self.assertEqual(v.evidence_dropped, ("d",))
        self.assertEqual(v.evidence_ignored, ("b",))
        self.assertEqual(v.evidence_total, 5)
        # 四段互斥且合起来正好是 evidence —— 没有重算，也没有漏算。
        union = (
            set(v.evidence_cited)
            | set(v.evidence_not_retrieved)
            | set(v.evidence_dropped)
            | set(v.evidence_ignored)
        )
        self.assertEqual(union, {"a", "b", "c", "x", "d"})
        self.assertEqual(
            len(v.evidence_cited)
            + len(v.evidence_not_retrieved)
            + len(v.evidence_dropped)
            + len(v.evidence_ignored),
            5,
        )

    def test_dropped_by_budget_is_not_blamed_on_retrieval(self) -> None:
        """**检到了但装不进预算** ≠ **检索没检到**。

        混成一句的话，报告会让人去换检索器 —— 而该动的是窗口或 top_k。
        这个方向只会让人改错东西，分数上完全看不出来。
        """
        v = score_citations(
            [],
            available=["a"],
            retrieved=["a", "big"],
            evidence=["a", "big"],
        )
        self.assertEqual(v.evidence_not_retrieved, ())
        self.assertEqual(v.evidence_dropped, ("big",))

    def test_evidence_cited_excludes_ungrounded_evidence_ids(self) -> None:
        """引了 ground truth 里、但**没给它**的 id —— 它算编造，**不算**依据命中。

        混进来的话，一个靠记忆背出正确答案的模型会在"依据召回"上拿满分，
        而那正是这个指标要抓的东西。
        """
        v = score_citations(
            ["redis:ttl:000"],
            available=["redis:expire:000"],
            retrieved=["redis:expire:000"],
            evidence=["redis:ttl:000"],
        )
        self.assertEqual(v.fabricated, ("redis:ttl:000",))
        self.assertEqual(v.evidence_cited, ())
        self.assertEqual(v.evidence_recall, 0.0)

    def test_duplicates_are_collapsed(self) -> None:
        v = score_citations(
            ["a", "a", "b"], available=["a", "b"], retrieved=["a", "b"], evidence=["a"]
        )
        self.assertEqual(v.cited, ("a", "b"))
        self.assertEqual(v.fabricated, ())

    # ---- `None` vs `()`：这是这一节存在的理由

    def test_none_means_unmeasurable_not_zero(self) -> None:
        """`cited=None` ⇒ 三个比例全是 `None`（**不可测**），不是 0.0。

        读成 0 的话，"没自述"会变成"引用质量极差"，然后有人去改一个没坏的东西。
        """
        v = score_citations(None, available=["a"], retrieved=["a"], evidence=["a", "b"])
        self.assertFalse(v.reported)
        self.assertIsNone(v.grounded_rate)
        self.assertIsNone(v.evidence_recall)
        self.assertIsNone(v.evidence_used_rate)

    def test_none_still_attributes_what_retrieval_missed(self) -> None:
        """没自述引用**不影响**"检索漏了哪些依据" —— 那是检索的事，与生成无关。"""
        v = score_citations(None, available=["a"], retrieved=["a"], evidence=["a", "b"])
        self.assertEqual(v.evidence_not_retrieved, ("b",))

    def test_empty_tuple_means_reported_and_zero(self) -> None:
        """`()` = 明确自述"没引用任何来源" ⇒ **可测**，召回 0。

        这就是 `null` 校准器的形状。写成 `None` 会让它整题退出分母，
        下界从"引用召回 0"退化成"引用不可测" —— 校准会**静默失效**。
        """
        v = score_citations((), available=["a"], retrieved=["a"], evidence=["a", "b"])
        self.assertTrue(v.reported)
        self.assertIsNone(v.grounded_rate)  # 分母是 0 条引用 ⇒ 比例不可测
        self.assertEqual(v.evidence_recall, 0.0)
        self.assertEqual(v.evidence_used_rate, 0.0)
        self.assertEqual(v.evidence_ignored, ("a",))

    def test_used_rate_denominator_excludes_what_retrieval_did_not_give(self) -> None:
        """检索没给的**不进** `evidence_used_rate` 的分母。

        进分母的话，检索越差这个数越高 —— 方向就反了，
        而它看起来仍然像一个合理的百分比。
        """
        v = score_citations(["a"], available=["a"], retrieved=["a"], evidence=["a", "b", "c"])
        # 该引 3 条，检索只给了 1 条，它把给的这条引了 ⇒ 纯生成侧满分。
        self.assertEqual(v.evidence_used_rate, 1.0)
        self.assertAlmostEqual(v.evidence_recall or 0, 1 / 3)


# ---------------------------------------------------------------- 报告读写


class TestCitationReport(unittest.TestCase):
    def test_old_format_report_without_citation_is_refused(self) -> None:
        """缺 `citation` 键 = **旧格式**。按空读会得到"没引用任何来源"这个假结论。"""
        d = _score("t-1", cited=("a",), available=("a",)).as_dict()
        del d["citation"]
        with self.assertRaises(ValueError) as cm:
            AnswerScore.from_dict(d)
        self.assertIn("citation", str(cm.exception))

    def test_citation_survives_json_round_trip(self) -> None:
        s = _score("t-1", cited=("a", "ghost"), available=("a",), evidence=("a",))
        back = AnswerScore.from_dict(json.loads(json.dumps(s.as_dict())))
        self.assertEqual(back.citation, s.citation)
        self.assertEqual(back.fabricated, ("ghost",))

    def test_cited_null_stays_null_not_empty_list(self) -> None:
        """`None` 落盘必须还是 `null`。

        落成 `[]` 的话，读回来变成"明确说没引用" —— 一次往返就把
        "没测"变成了"测了，结果是 0"。
        """
        s = _score("t-1", cited=None, available=("a",), evidence=("a",))
        raw = json.dumps(s.as_dict())
        self.assertIn('"cited": null', raw)
        back = AnswerScore.from_dict(json.loads(raw))
        self.assertIsNone(back.citation.cited)
        self.assertFalse(back.citation.reported)


# ---------------------------------------------------------------- 聚合


class TestCitationAggregation(unittest.TestCase):
    def test_aggregates_are_per_sample_means_not_ratios_of_sums(self) -> None:
        """**逐样本求均值**，不是 Σ/Σ。

        这里曾经用 Σ/Σ，于是"依据召回 0.2955"和检索报告里**同一个量**的
        `context_recall 0.4424` 对不上。两题：一题 1/1，一题 0/4 ——
        macro = 0.5，micro = 0.2，差得足够大，选错会被这条测试抓住。
        """
        g = _group(
            [
                _score("t-1", cited=("a",), available=("a",), evidence=("a",)),
                _score("t-2", cited=(), available=("b",), evidence=("b", "c", "d", "e")),
            ],
            samples_per_task=1,
        )
        self.assertAlmostEqual(g.evidence_recall or 0, 0.5)
        self.assertNotAlmostEqual(g.evidence_recall or 0, 0.2)

    def test_samples_without_citations_leave_the_denominator(self) -> None:
        g = _group(
            [
                _score("t-1", cited=("a",), available=("a",), evidence=("a",)),
                _score("t-2", cited=None, available=("b",), evidence=("b",)),
            ],
            samples_per_task=1,
        )
        self.assertEqual(g.n_with_citations, 1)
        self.assertEqual(g.n_without_citations, 1)
        # 只有 t-1 进分母 ⇒ 1.0，而不是被 t-2 拖到 0.5。
        self.assertEqual(g.evidence_recall, 1.0)

    def test_all_silent_means_every_citation_number_is_unmeasurable(self) -> None:
        g = _group([_score("t-1", cited=None, evidence=("a",))], samples_per_task=1)
        self.assertIsNone(g.grounded_rate)
        self.assertIsNone(g.evidence_recall)
        self.assertIsNone(g.evidence_used_rate)
        self.assertEqual(g.n_without_citations, 1)

    def test_fabricated_counts_ids_and_samples_separately(self) -> None:
        """`fabricated_total` 数**条数**，`samples_with_fabrication` 数**题数**。

        只报一个的话，"编了 10 条"和"10 道题各编 1 条"读起来一样 ——
        而后者严重得多。
        """
        g = _group(
            [
                _score("t-1", cited=("g1", "g2", "g3"), available=()),
                _score("t-2", cited=("g4",), available=()),
            ],
            samples_per_task=1,
        )
        self.assertEqual(g.fabricated_total, 4)
        self.assertEqual(g.samples_with_fabrication, 2)

    def test_group_round_trips_none(self) -> None:
        g = AnswerGroup(n_with_citations=0, grounded_rate=None)
        back = AnswerGroup.from_dict(json.loads(json.dumps(g.as_dict())))
        self.assertIsNone(back.grounded_rate)
        self.assertIsNone(back.evidence_recall)


class TestCitationAggregateVerification(unittest.TestCase):
    def _report(self, **over) -> AnswerReport:
        rep = AnswerReport(
            topic="redis",
            answerer="oracle",
            retriever="bm25",
            top_k=1,
            samples_per_task=1,
            items=(
                _score("t-1", cited=("a",), available=("a",), evidence=("a",)),
                _score("t-2", cited=("a", "ghost"), available=("a",), evidence=("a",)),
            ),
        )
        rep = dataclasses.replace(rep, overall=_group(rep.items, samples_per_task=1))
        return dataclasses.replace(rep, **over) if over else rep

    def test_a_consistent_report_passes(self) -> None:
        self._report()._verify_aggregates()

    def test_citation_drift_is_caught(self) -> None:
        rep = self._report()
        bad = dataclasses.replace(rep, overall=dataclasses.replace(rep.overall, fabricated_total=0))
        with self.assertRaises(ValueError) as cm:
            bad._verify_aggregates()
        self.assertIn("fabricated_total", str(cm.exception))

    def test_none_to_number_drift_is_caught(self) -> None:
        """`None` → 数字也是漂移。

        只用 `abs(stored - again) > 1e-3` 比会把 `None` 漏过去（TypeError）
        或者直接崩 —— 两种都不是"校验通过"，但都不像"这一栏坏了"。
        """
        rep = self._report()
        bad = dataclasses.replace(rep, overall=dataclasses.replace(rep.overall, grounded_rate=None))
        with self.assertRaises(ValueError) as cm:
            bad._verify_aggregates()
        self.assertIn("grounded_rate", str(cm.exception))

    def test_drift_message_says_dont_trust_it(self) -> None:
        rep = self._report()
        bad = dataclasses.replace(rep, overall=dataclasses.replace(rep.overall, n_with_citations=99))
        with self.assertRaises(ValueError) as cm:
            bad._verify_aggregates()
        self.assertIn("不可信", str(cm.exception))


# ---------------------------------------------------------------- 校准


class TestCalibration(unittest.TestCase):
    """三个假答案器给引用判据划出上下界，并证明**编造探测器会响**。"""

    def _run(self, answerer, **kw):  # type: ignore[no-untyped-def]
        return evaluate_answers(_kb(), _dataset(), answerer, top_k=5, **kw)

    def test_oracle_is_fully_grounded(self) -> None:
        rep = self._run(OracleAnswerer())
        self.assertEqual(rep.overall.grounded_rate, 1.0)
        self.assertEqual(rep.overall.fabricated_total, 0)

    def test_oracle_uses_every_evidence_it_was_given(self) -> None:
        """oracle 引 `contexts`（拿到的），不引 ground truth。

        引 ground truth 会把"检索漏了没有"混进 `grounded_rate`，
        于是 oracle 的上界随被测系统浮动 —— 它就不再是判据的上界了。
        """
        rep = self._run(OracleAnswerer())
        self.assertEqual(rep.overall.evidence_used_rate, 1.0)
        for row in rep.items:
            self.assertEqual(row.citation.evidence_ignored, ())

    def test_oracle_evidence_recall_equals_the_retrieval_context_recall(self) -> None:
        """**同一个量只有一处定义。**

        oracle 把拿到的依据一条不漏地引了 ⇒ 它的"依据召回"就等于检索报告的
        `context_recall`。这条把两份报告的口径钉在一起：谁改了平均方式，这里就红。
        """
        from asuka.evaluate import evaluate_retrieval

        kb = _kb()
        ds = _dataset()
        ret = evaluate_retrieval(kb, ds, top_k=5)
        ans = evaluate_answers(kb, ds, OracleAnswerer(), top_k=5)
        self.assertAlmostEqual(ans.overall.evidence_recall or 0, ret.overall.recall, places=6)

    def test_null_reports_empty_citations_not_none(self) -> None:
        """下界必须是**可测的 0**，不是"不可测"。"""
        rep = self._run(NullAnswerer())
        self.assertEqual(rep.overall.n_with_citations, rep.overall.n_samples)
        self.assertEqual(rep.overall.evidence_recall, 0.0)
        self.assertEqual(rep.overall.evidence_used_rate, 0.0)

    def test_fabricator_is_caught(self) -> None:
        """**证明编造探测器会响。**

        没有这一条，`fabricated` 那一栏可能是一段永远为空的代码 ——
        `oracle` 和 `null` 都碰不到这条路径，报告看起来一切正常。
        """
        rep = self._run(FabricatingAnswerer())
        self.assertEqual(rep.overall.grounded_rate, 0.0)
        self.assertEqual(rep.overall.samples_with_fabrication, rep.overall.n_samples)
        self.assertGreater(rep.overall.fabricated_total, 0)
        self.assertTrue(all(r.fabricated for r in rep.items))

    def test_every_calibrator_self_describes(self) -> None:
        for a in (OracleAnswerer(), NullAnswerer(), FabricatingAnswerer()):
            self.assertIsInstance(a.is_calibration, bool)
            self.assertTrue(a.is_calibration)

    def test_a_silent_answerer_does_not_break_the_run(self) -> None:
        """没自述引用**不该让运行失败** —— 但它必须退出引用分母并被点名。"""
        rep = self._run(_SilentAnswerer())
        self.assertEqual(rep.overall.n_with_citations, 0)
        self.assertEqual(rep.overall.n_without_citations, rep.overall.n_samples)
        self.assertIsNone(rep.overall.grounded_rate)
        self.assertIsNone(rep.overall.evidence_recall)


# ---------------------------------------------------------------- 渲染


class TestCitationRendering(unittest.TestCase):
    def _render(self, answerer) -> str:  # type: ignore[no-untyped-def]
        return render_markdown(evaluate_answers(_kb(), _dataset(), answerer, top_k=5))

    def test_unmeasurable_is_printed_as_dash(self) -> None:
        md = self._render(_SilentAnswerer())
        self.assertIn("| 引用有依据率 grounded | — |", md)
        self.assertIn("没有自述引用", md)

    def test_fabricated_ids_are_named(self) -> None:
        md = self._render(FabricatingAnswerer())
        self.assertIn("引用了没给它的来源", md)
        self.assertIn("ghost-chunk-0001", md)

    def test_attribution_is_split_into_three_lines(self) -> None:
        """缺的依据要分**三**行说：检索没检到 / 检到了装不下 / 给了没引。

        合成一行的话，"检索没检到"会被读成"模型没用依据"，改错地方；
        而"装不下"（改配置）和"没检到"（改检索器）也是两件事。
        """
        md = self._render(FabricatingAnswerer())
        self.assertIn("检索**根本没检到**", md)
        self.assertIn("检到了但**装不进预算**", md)
        self.assertIn("给了它却**没引**", md)
        self.assertIn("> ⚠️ 第二行和第一行**必须分开**", md)

    def test_precision_is_explained_as_deliberately_absent(self) -> None:
        md = self._render(OracleAnswerer())
        self.assertIn("citation_precision", md)
        self.assertIn("奖励『少引』", md)


# ---------------------------------------------------------------- Trace


class TestCitationTrace(unittest.TestCase):
    def _trace(self, answerer, **kw):  # type: ignore[no-untyped-def]
        kb = _kb()
        ds = _dataset()
        t = Trace.start(
            RunIdentity(
                topic="redis",
                retriever="bm25",
                top_k=5,
                samples_per_task=1,
                corpus_chunks=len(_CHUNKS),
                answerer=getattr(answerer, "name", "?"),
            )
        )
        rep = evaluate_answers(kb, ds, answerer, top_k=5, trace=t, **kw)
        t.finish()
        return t, rep

    def test_generation_event_must_carry_citations(self) -> None:
        """缺 `citations` 字段 = 旧格式，必须被点名。

        用 `.get("citations", ())` 读的话，"缺字段"和"答案器没自述"
        会读成同一个东西 —— 而它们是两件事。
        """
        t = Trace.start(
            RunIdentity(topic="redis", retriever="bm25", top_k=1, samples_per_task=1,
                        corpus_chunks=1, answerer="x")
        )
        t.emit(
            "retrieval",
            task_id="t-1",
            query="q",
            kept=["a"],
            context=["a"],
            dropped_budget=[],
            context_tokens=1,
            latency_ms=0.0,
        )
        t.emit("generation", task_id="t-1", answerer="x", chars=1)  # 缺 citations
        t.emit("scoring", task_id="t-1", points_total=1, hits=["h"], missed=[], fabricated=[])
        t.finish()
        with self.assertRaises(TraceError) as cm:
            t.verify()
        self.assertIn("citations", str(cm.exception))

    def test_verify_against_recomputes_fabrication_from_two_events(self) -> None:
        """trace 那条路径从 `citations − context` **对减**得到编造，不碰 `score_citations`。

        两条路都调同一个函数的话，这个核对只是在证明"我等于我自己"。
        ⚠️ 右边是 `context`（喂进 prompt 的）不是 `kept`（检到的）——
        被预算丢掉的片模型没看见，引用了它就是编造。
        """
        t, rep = self._trace(FabricatingAnswerer())
        t.verify_against(rep)  # 不抛就算过
        self.assertGreater(rep.overall.fabricated_total, 0)

    def test_verify_against_catches_fabricated_drift(self) -> None:
        t, rep = self._trace(FabricatingAnswerer())
        bad = dataclasses.replace(
            rep, overall=dataclasses.replace(rep.overall, fabricated_total=0)
        )
        with self.assertRaises(TraceError) as cm:
            t.verify_against(bad)
        self.assertIn("fabricated_total", str(cm.exception))

    def test_verify_against_catches_silent_count_drift(self) -> None:
        t, rep = self._trace(_SilentAnswerer())
        bad = dataclasses.replace(
            rep, overall=dataclasses.replace(rep.overall, n_without_citations=0)
        )
        with self.assertRaises(TraceError) as cm:
            t.verify_against(bad)
        self.assertIn("n_without_citations", str(cm.exception))

    def test_explain_names_the_fabricated_ids(self) -> None:
        from asuka.trace import explain

        t, _ = self._trace(FabricatingAnswerer())
        text = explain(t, "t-1")
        # ⚠️ 不能只断言 `"编造" in text` —— 否定句 `"- 没有编造引用"` 里也有这两个字。
        # 那条断言**永远为真**，等于什么都没测（变红验证抓到过）。
        self.assertIn(
            "**编造**：引用了这次检索**没给它**的来源 "
            "['ghost-chunk-0001', 'ghost-chunk-0002']",
            text,
        )
        self.assertNotIn("没有编造引用", text)

    def test_explain_says_unmeasurable_when_not_reported(self) -> None:
        from asuka.trace import explain

        t, _ = self._trace(_SilentAnswerer())
        text = explain(t, "t-1")
        self.assertIn("没有自述引用", text)

    def test_trace_jsonl_keeps_null_citations(self) -> None:
        t, _ = self._trace(_SilentAnswerer())
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            raw = p.read_text(encoding="utf-8")
            self.assertIn('"citations": null', raw)
            self.assertIsNone(Trace.load(p).events[2].data["citations"])


# ---------------------------------------------------------------- CLI


class TestCitationCli(unittest.TestCase):
    def test_cli_offers_the_fabricator(self) -> None:
        from asuka.answers import build_parser

        action = next(a for a in build_parser()._actions if a.dest == "answerer")
        self.assertIn("fabricator", action.choices)
        self.assertEqual(build_parser().parse_args(["--answerer", "fabricator"]).answerer,
                         "fabricator")
        # 拼错会被 argparse 挡下，而不是静默跑成 oracle。
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--answerer", "nope"])

    def test_trace_cli_offers_the_fabricator(self) -> None:
        from asuka.trace import build_parser as trace_parser

        self.assertEqual(
            trace_parser().parse_args(["--answerer", "fabricator"]).answerer, "fabricator"
        )

    def test_cli_default_is_still_oracle(self) -> None:
        from asuka.answers import build_parser

        self.assertEqual(build_parser().parse_args([]).answerer, "oracle")


if __name__ == "__main__":
    unittest.main()
