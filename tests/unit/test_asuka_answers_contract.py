"""答案级判据的契约。

--------------------------------------------------------------------------
这份测试守的核心是一句话：**判据必须能区分，否则它只是装饰**

所以这里不是"跑通就行"，而是逐条钉住"什么样算答到、什么样算没答到"：

  · 单词短语要**词边界**匹配 —— `set` 不能在 `subset` 里命中（假命中会让分数虚高）
  · 参考答案里的 markdown 标记要**剥掉** —— 否则声明对了也匹配不上，且失败静默
  · **没声明要点**不等于"答对了" —— 它不可测，不进任何分母
  · `pass@k` 的公式要和**穷举**对得上（公式写错是最容易静默出错的地方）

--------------------------------------------------------------------------
`oracle` 是最强的一条自检

`OracleAnswerer` 直接返回参考答案 ⇒ 要点召回**必须** 1.0。
它一红，说明"每条要点的说法确实能在参考答案里找到"这件事在**判据期**不成立 ——
而 `Dataset.validate` 只在校验期查这个。两处用的必须是**同一条匹配规则**。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from packages.agent_context.retrieval import Chunk, RetrievalPipeline

from asuka.answers import (
    Answer,
    AnswerReport,
    NullAnswerer,
    OracleAnswerer,
    evaluate_answers,
    pass_at_k,
    render_markdown,
    score_answer,
)
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter


def _chunk(cid: str, text: str, unit: str, section: str = "overview") -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · {section}",
        attributes={"unit_id": unit, "section": section, "visibility": "public"},
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


def _kb() -> KnowledgeBase:
    r = BM25Retriever(tuple(_CHUNKS))
    return KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
        retriever=r,
        kind="bm25",
    )


def _dataset(*items: TaskItem) -> Dataset:
    ds = Dataset(topic="redis", items=items or (_item(),))
    ds.resolve(_CHUNKS)
    return ds


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


# ---------------------------------------------------------------- 匹配规则


class TestRequiredPointMatching(unittest.TestCase):
    def test_word_boundary_blocks_substring_false_hit(self) -> None:
        """`set` 不能在 `subset` 里命中 —— 假命中让分数**虚高**，比漏判更危险。"""
        p = RequiredPoint("集合", ("set",))
        self.assertEqual(p.matched_by("a subset of keys"), "")
        self.assertEqual(p.matched_by("the set is unordered"), "set")

    def test_multiword_phrase_matches_as_substring(self) -> None:
        p = RequiredPoint("存活时间", ("time to live",))
        self.assertEqual(p.matched_by("the remaining Time To Live value"), "time to live")

    def test_markdown_markers_are_stripped(self) -> None:
        """参考答案是**带 markdown 的**，答案里通常没有。

        不剥掉的话，声明写得再对也会匹配不上，而失败是**静默**的 ——
        分数偏低，没有任何东西提示你"是匹配规则的问题"。
        """
        p = RequiredPoint("新增", ("number of elements that were added",))
        self.assertEqual(
            p.matched_by("the number of elements that were **added** to the set"),
            "number of elements that were added",
        )
        self.assertEqual(p.matched_by("use `SET key value`"), "")

    def test_matched_by_returns_the_phrase_not_a_bool(self) -> None:
        """要能回答"凭什么算答到了" —— 否则分数不可复核。"""
        p = RequiredPoint("设超时", ("sets a timeout", "a timeout in seconds"))
        self.assertEqual(p.matched_by("It sets a timeout, in seconds, on a key."), "sets a timeout")

    def test_empty_any_of_never_matches(self) -> None:
        self.assertEqual(RequiredPoint("空的", ()).matched_by("anything at all"), "")

    def test_case_and_whitespace_are_normalised(self) -> None:
        p = RequiredPoint("x", ("o(1)",))
        self.assertEqual(p.matched_by("Time complexity is  O(1)."), "o(1)")


# ---------------------------------------------------------------- 判分


class TestScoring(unittest.TestCase):
    def test_full_answer_hits_every_point(self) -> None:
        hits, missed, hit_by = score_answer(_item(), _item().reference_answer)
        self.assertEqual(len(hits), 2)
        self.assertEqual(missed, ())
        self.assertEqual(len(hit_by), 2)

    def test_partial_answer_gets_partial_credit(self) -> None:
        hits, missed, _ = score_answer(_item(), "EXPIRE sets a timeout on a key.")
        self.assertEqual(hits, ("设超时",))
        self.assertEqual(missed, ("到期删除",))

    def test_no_points_declared_is_unscorable_not_pass(self) -> None:
        """『没测』不等于『答对了』—— 混起来会让覆盖率看起来比实际高。"""
        item = _item(required_points=())
        from asuka.answers import AnswerScore

        s = AnswerScore(task_id=item.task_id, difficulty="simple", question="q", points_total=0)
        self.assertIsNone(s.recall)
        self.assertFalse(s.passed)

    def test_scoring_rule_is_the_same_object_as_validation(self) -> None:
        """判据与校验必须**同源**。

        两处各写一份匹配规则，改了一处就会出现"校验说声明没问题、
        判分说答不到"这种鬼故事 —— 而两边的测试都会绿。
        """
        item = _item()
        for p in item.required_points:
            with self.subTest(point=p.label):
                # 参考答案能证成这条要点（校验期用的规则）
                self.assertTrue(p.matched_by(item.reference_answer))
                # 同一条规则在判分时也命中
                hits, _, _ = score_answer(item, item.reference_answer)
                self.assertIn(p.label, hits)


# ---------------------------------------------------------------- pass@k


def _brute_force_pass_at_k(n: int, c: int, k: int) -> float:
    """穷举：前 c 个样本成功，任取 k 个，至少含一个成功的比例。"""
    if n <= 0 or k <= 0 or c <= 0:
        return 0.0
    k = min(k, n)
    subsets = list(combinations(range(n), k))
    good = sum(1 for s in subsets if any(i < c for i in s))
    return good / len(subsets)


class TestPassAtK(unittest.TestCase):
    def test_formula_matches_brute_force(self) -> None:
        """公式写错是最容易**静默**出错的地方 —— 所以和穷举逐个对。"""
        for n in range(1, 9):
            for c in range(0, n + 1):
                for k in range(1, 10):
                    with self.subTest(n=n, c=c, k=k):
                        self.assertAlmostEqual(
                            pass_at_k(n, c, k), _brute_force_pass_at_k(n, c, k), places=9
                        )

    def test_k_larger_than_n_degrades_to_pass_at_n(self) -> None:
        self.assertAlmostEqual(pass_at_k(3, 1, 10), pass_at_k(3, 1, 3))

    def test_zero_success_is_zero_and_all_success_is_one(self) -> None:
        self.assertEqual(pass_at_k(5, 0, 3), 0.0)
        self.assertEqual(pass_at_k(5, 5, 3), 1.0)

    def test_monotonic_in_k(self) -> None:
        """k 越大越容易"至少一次成功" —— 不单调一定是公式错了。"""
        vals = [pass_at_k(10, 2, k) for k in range(1, 11)]
        self.assertEqual(vals, sorted(vals))

    def test_degenerate_inputs_are_zero_not_crash(self) -> None:
        self.assertEqual(pass_at_k(0, 0, 3), 0.0)
        self.assertEqual(pass_at_k(3, 1, 0), 0.0)


# ---------------------------------------------------------------- 校准


class TestCalibration(unittest.TestCase):
    def test_oracle_scores_full_on_the_real_dataset(self) -> None:
        """最强的一条自检：参考答案对每条要点都必须命中。

        它红 = 有一条要点**无人能答**，而读者会把那个低分读成"模型不行"。
        """
        from asuka.corpus import read_chunks
        from asuka.datasets.redis import build

        repo = Path(__file__).resolve().parents[2]
        chunks_path = repo / "asuka" / "corpus" / "redis" / "chunks.jsonl"
        if not chunks_path.exists():
            self.skipTest("还没有语料")
        ds = build()
        ds.resolve(read_chunks(chunks_path))
        rep = evaluate_answers(_kb(), ds, OracleAnswerer(), top_k=3)
        bad = [
            (s.task_id, s.missed)
            for s in rep.items
            if s.points_total and not s.passed
        ]
        self.assertEqual(bad, [], f"参考答案答不到自己的要点：{bad}")
        self.assertAlmostEqual(rep.overall.mean_recall, 1.0, places=6)

    def test_null_scores_zero(self) -> None:
        """判据不能送分。"""
        rep = evaluate_answers(_kb(), _dataset(), NullAnswerer(), top_k=3)
        self.assertEqual(rep.overall.mean_recall, 0.0)
        self.assertEqual(rep.overall.pass_at_1, 0.0)

    def test_answerer_must_self_report_calibration(self) -> None:
        """不说的 answerer 会被**拒绝**，而不是被默认当成真模型。

        默认当"真模型"是最坏的选择：校准分数会被读成模型成绩，
        而报告里没有任何东西会提醒你。
        """

        class _Mum:
            name = "mum"

            def answer(self, item, contexts):  # noqa: ANN001
                return Answer(text="…")

        with self.assertRaises(ValueError) as ctx:
            evaluate_answers(_kb(), _dataset(), _Mum(), top_k=3)  # type: ignore[arg-type]
        self.assertIn("is_calibration", str(ctx.exception))

    def test_says_no_when_is_calibration_is_not_a_bool(self) -> None:
        """`is_calibration="yes"` 也算没自述 —— 真值判断不算声明。"""

        class _Sloppy:
            name = "sloppy"
            is_calibration = "yes"  # type: ignore[assignment]

            def answer(self, item, contexts):  # noqa: ANN001
                return Answer(text="…")

        with self.assertRaises(ValueError):
            evaluate_answers(_kb(), _dataset(), _Sloppy(), top_k=3)  # type: ignore[arg-type]

    def test_calibration_flag_reaches_the_report(self) -> None:
        rep = evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=3)
        self.assertTrue(rep.calibration)
        self.assertIn("校准跑，不是模型成绩", render_markdown(rep))

    def test_zero_samples_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=3, samples_per_task=0)


# ---------------------------------------------------------------- 报告


class TestAnswerReport(unittest.TestCase):
    def _rep(self, **kw):
        return evaluate_answers(_kb(), _dataset(*kw.pop("items", ())), OracleAnswerer(), top_k=3, **kw)

    def test_round_trip_preserves_numbers(self) -> None:
        rep = self._rep()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            back = AnswerReport.load(p)
        self.assertEqual(back.samples_per_task, rep.samples_per_task)
        self.assertEqual(len(back.items), len(rep.items))
        self.assertAlmostEqual(back.overall.mean_recall, rep.overall.mean_recall, places=4)

    def test_derived_aggregate_is_recomputed_not_restored(self) -> None:
        """聚合值是派生量：改了文件里的值，读回来必须报错而不是照单全收。"""
        rep = self._rep()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            raw["overall"]["mean_recall"] = 0.0 if raw["overall"]["mean_recall"] > 0.5 else 1.0
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                AnswerReport.load(p)
        self.assertIn("对不上", str(ctx.exception))

    def test_unscorable_tasks_are_named_and_excluded_from_the_denominator(self) -> None:
        """没声明要点的题**既不算 0 也不算 1**，但必须被点名。"""
        good = _item("t-1")
        blind = _item("t-2", required_points=(), evidence=(Evidence("ttl", "overview"),))
        rep = evaluate_answers(_kb(), _dataset(good, blind), OracleAnswerer(), top_k=3)
        self.assertEqual(rep.overall.tasks_unscorable, 1)
        self.assertEqual(rep.overall.n_scorable, 1)
        md = render_markdown(rep)
        self.assertIn("不可测的题", md)
        self.assertIn("t-2", md)
        self.assertNotIn("t-1`", md.split("不可测的题")[1].split("逐题明细")[0])

    def test_all_missed_lists_the_missing_points(self) -> None:
        """只说"这题错了"没法去改 —— 必须说清缺哪个要点。"""
        rep = evaluate_answers(_kb(), _dataset(), NullAnswerer(), top_k=3)
        md = render_markdown(rep)
        self.assertIn("一个要点都没答到的题", md)
        self.assertIn("设超时", md)
        self.assertIn("到期删除", md)

    def test_pass_k_row_is_hidden_when_samples_is_one(self) -> None:
        """samples=1 时 pass@1 与 pass@k 是同一个数，印两行会让人以为看错了。"""
        md = render_markdown(self._rep(samples_per_task=1))
        self.assertEqual(md.count("pass@1"), 2)  # 总体表 1 行 + 分难度表头 1 处
        self.assertNotIn("pass@2", md)

    def test_pass_k_row_appears_when_sampling(self) -> None:
        rep = self._rep(samples_per_task=3)
        md = render_markdown(rep)
        self.assertIn("pass@3", md)
        self.assertEqual(rep.overall.pass_at_k, 1.0)

    def test_samples_per_task_is_cross_checked_against_the_items(self) -> None:
        """`samples_per_task` **本身是聚合的输入**（它决定 `pass@k` 的 k）。

        改掉文件里的它，会让"用新 k 重算"和"存着的 pass@k"**一起**漂移 ——
        两边一致，别的校验全都静默通过。所以它必须和 `items` 对账。
        """
        rep = self._rep(samples_per_task=3)
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            raw["samples_per_task"] = 7
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                AnswerReport.load(p)
        self.assertIn("样本条数对不上", str(ctx.exception))

    def test_errors_do_not_silently_become_wrong_answers(self) -> None:
        """生成报错要能被数出来 —— 「管线坏了」和「答错了」是两件事。"""

        class _Flaky(OracleAnswerer):
            name = "flaky"

            def answer(self, item, contexts):  # noqa: ANN001
                return Answer(text="", error="HTTP 429")

        rep = evaluate_answers(_kb(), _dataset(), _Flaky(), top_k=3)
        self.assertEqual(rep.overall.errors, 1)
        self.assertIn("生成**报错**", render_markdown(rep))

    def test_chars_per_hit_point_exposes_verbatim_dumping(self) -> None:
        """判据是要点召回 ⇒ 抄满文档也满分。这个数就是那条缺口的**度量**。"""

        class _Dumper(OracleAnswerer):
            name = "dumper"

            def answer(self, item, contexts):  # noqa: ANN001
                return Answer(text=item.reference_answer + " filler. " * 500)

        rep = evaluate_answers(_kb(), _dataset(), _Dumper(), top_k=3)
        self.assertAlmostEqual(rep.overall.mean_recall, 1.0, places=6)
        self.assertGreater(rep.overall.chars_per_hit_point, 1000.0)

    def test_tokens_and_cost_are_carried_through(self) -> None:
        class _Priced(OracleAnswerer):
            name = "priced"

            def answer(self, item, contexts):  # noqa: ANN001
                return Answer(text=item.reference_answer, prompt_tokens=100, completion_tokens=50, cost_usd=0.002)

        rep = evaluate_answers(_kb(), _dataset(), _Priced(), top_k=3)
        self.assertEqual(rep.overall.total_tokens, 150)
        self.assertAlmostEqual(rep.overall.total_cost_usd, 0.002, places=6)
        self.assertEqual(rep.overall.mean_tokens, 150.0)


# ---------------------------------------------------------------- CLI


class TestAnswerCliIsStrictByDefault(unittest.TestCase):
    """CLI 的默认值就是**声明**，所以它要被断言，而不是靠读代码。"""

    def test_defaults(self) -> None:
        from asuka.answers import build_parser

        a = build_parser().parse_args([])
        self.assertEqual(a.answerer, "oracle")
        self.assertEqual(a.retriever, "bm25")
        self.assertEqual(a.samples, 1)
        self.assertEqual(a.embedder, "auto")

    def test_allow_non_semantic_defaults_to_false(self) -> None:
        from asuka.answers import build_parser

        self.assertFalse(build_parser().parse_args([]).allow_non_semantic)
        self.assertTrue(build_parser().parse_args(["--allow-non-semantic"]).allow_non_semantic)


if __name__ == "__main__":
    unittest.main()
