"""回归对比的契约 —— 判的是**新退步**，不是绝对通过率。

--------------------------------------------------------------------------
锁四件事

**一、"两次都不通过"归 `unchanged`，不是回归**

这是 `packages.agent_evaluation.regression` 的核心语义，本模块只复用不重写。
但它**很容易被"顺手改进"成 `failed`** —— 那样每轮都会有一堆红，
真正的退步就淹在里面了（"每轮 3 条红、其实 0 个新问题"）。
所以拿测试把它钉住，不让下一个人好心改坏。

**二、题级口径：通过 = 这道题的**全部采样**都通过**

`AnswerScore.passed` 是**一条样本**的判定；回归对比是**一道题对一道题**。
口径必须写死、并印在报告上 —— 否则 `3/3 → 1/3` 会被读成"没事"。

反过来这个口径下**一次采样翻转就算回归**，所以报告里必须同时印前后比例。
判定给结论，比例给分辨力。

**三、不可测的题不进对比**

`points_total == 0` ⇒ `passed` 恒 `False` ⇒ 直接拿去比，一道**没测**的题
会以"两次都不通过"的形状落进 `unchanged` —— 读起来是"已知问题"，
真相是"压根没测"。

**四、配置变了，就不是"这次 vs 上次"**

和 `compare` 同一道门：不可比就拒绝，且**一次报全部原因**。
⚠️ `corpus_chunks` 是**计数不是指纹**（语料重切后条数可能恰好不变）——
这一条是**已知缺口**，报告里写着，不假装它被守住了。
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from packages.agent_context.items import ContextItem
from packages.agent_context.retrieval import Chunk, RetrievalPipeline

from asuka.answers import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_RESERVED_FOR_OUTPUT,
    Answer,
    AnswerReport,
    evaluate_answers,
)
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter
from asuka.regression import (
    ATTR_ERROR,
    ATTR_FLAKY,
    ATTR_WRONG,
    PASS_DEFINITION,
    as_dict,
    baseline,
    check_comparable,
    count_kinds,
    deltas,
    looks_reversed,
    main,
    only_in_before,
    render_markdown,
    task_verdicts,
    unmeasurable,
)
from asuka.textutil import CHARS_PER_TOKEN


# ---------------------------------------------------------------- 脚手架


def _chunk(cid: str, text: str, unit: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · overview",
        attributes={"unit_id": unit, "section": "overview", "visibility": "public"},
    )


_CHUNKS = (
    _chunk("redis:expire:000", "# EXPIRE\n\nEXPIRE sets a timeout on a key.", "expire"),
    _chunk("redis:ttl:000", "# TTL\n\nTTL returns the remaining time to live.", "ttl"),
    _chunk(
        "redis:persist:000",
        "# PERSIST\n\nPERSIST removes the expiration from a key.",
        "persist",
    ),
)

_TASKS = ("t-expire", "t-ttl", "t-persist")
#: 一道**没有必答要点**的题 —— `points_total == 0`，不可测。
_BLIND = "t-blind"

_QUESTIONS = {
    "t-expire": "What does EXPIRE do?",
    "t-ttl": "What does TTL return?",
    "t-persist": "What does PERSIST do?",
    _BLIND: "What is the meaning of life?",
}
_ANSWERS = {
    "t-expire": "EXPIRE sets a timeout on a key.",
    "t-ttl": "TTL returns the remaining time to live.",
    "t-persist": "PERSIST removes the expiration from a key.",
}
_POINTS = {
    "t-expire": ("sets a timeout",),
    "t-ttl": ("remaining time to live",),
    "t-persist": ("removes the expiration",),
}


@dataclass
class _Scripted:
    """按**剧本**答题的校准答案器。

    用它把"哪道题过、哪道不过"变成输入，而不是碰运气 ——
    回归对比的测试必须能精确构造"上次过、这次不过"。
    剧本值是**一个序列**（按采样次序取），用来构造"有的过有的不过"。
    """

    plan: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: `task_id → N`：**前 N 个采样**报错，其余按剧本。
    #: 用来构造"同一道题既报错又通过" —— 那是 N3（归因优先级）唯一能区分的场景。
    fail_error: Mapping[str, int] = field(default_factory=dict)
    #: `True` ⇒ **不自述引用**（`citations=None`）⇒ 引用指标全部不可测。
    #: 用来验"不可测印 `—` 而不是 0"。
    mute_citations: bool = False
    name: str = "scripted"
    is_calibration: bool = True
    _seen: dict[str, int] = field(default_factory=dict)

    def answer(self, item: TaskItem, contexts: tuple[ContextItem, ...]) -> Answer:
        n = self._seen.get(item.task_id, 0)
        self._seen[item.task_id] = n + 1
        if self.fail_error.get(item.task_id, 0) > n:
            return Answer(text="", error="boom")
        seq = self.plan.get(item.task_id) or ("",)
        return Answer(
            text=seq[min(n, len(seq) - 1)],
            citations=None if self.mute_citations else tuple(i.key for i in contexts),
        )


def _dataset(tasks: tuple[str, ...], *, blind: bool = False) -> Dataset:
    ids = tasks + ((_BLIND,) if blind else ())
    items = []
    for tid in ids:
        unit = "expire" if tid in ("t-expire", _BLIND) else tid.split("-")[1]
        items.append(
            TaskItem(
                task_id=tid,
                question=_QUESTIONS[tid],
                reference_answer=_ANSWERS.get(tid, "42"),
                source_document=f"redis:{unit}",
                difficulty="simple",
                evidence=(Evidence(unit, "overview"),),
                required_points=(
                    () if tid == _BLIND else (RequiredPoint("要点", _POINTS[tid]),)
                ),
            )
        )
    ds = Dataset(topic="redis", items=tuple(items))
    ds.resolve(_CHUNKS)
    return ds


def _run(
    plan: dict[str, tuple[str, ...]],
    *,
    samples: int = 1,
    blind: bool = False,
    fail_error: Mapping[str, int] = {},
    mute_citations: bool = False,
    top_k: int = 3,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
    answerer_name: str = "scripted",
    real_run: bool = False,
) -> AnswerReport:
    """题库 = **剧本里提到的那几道题**（外加 `blind` 那道不可测的）。

    ⚠️ 剧本控制的是**答案**，不是题库。第一版把两者混在一起，
    于是"上次有这道题、这次没有"根本构造不出来（题库一直是那三道）。
    """
    r = BM25Retriever(tuple(_CHUNKS))
    kb = KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
        retriever=r,
        kind="bm25",
    )
    return evaluate_answers(
        kb,
        _dataset(tuple(plan), blind=blind),
        _Scripted(
            plan=plan,
            fail_error=fail_error,
            mute_citations=mute_citations,
            name=answerer_name,
            is_calibration=not real_run,
        ),
        top_k=top_k,
        samples_per_task=samples,
        corpus_chunks=len(_CHUNKS),
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )


def _all_pass(tasks: tuple[str, ...] = _TASKS) -> dict[str, tuple[str, ...]]:
    return {t: (_ANSWERS[t],) for t in tasks}


def _all_fail(tasks: tuple[str, ...] = _TASKS) -> dict[str, tuple[str, ...]]:
    return {t: ("I don't know.",) for t in tasks}


def _drop(plan: dict[str, tuple[str, ...]], *tasks: str) -> dict[str, tuple[str, ...]]:
    out = dict(plan)
    for t in tasks:
        out[t] = ("I don't know.",)
    return out


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


# ---------------------------------------------------------------- 题级判定


class TestTaskVerdicts(unittest.TestCase):
    """`AnswerScore`（一条样本）→ `Verdict`（一道题）。这是本模块自己的那一层。"""

    def test_one_verdict_per_scorable_task(self) -> None:
        vs = task_verdicts(_run(_all_pass()))
        self.assertEqual(sorted(v.case_id for v in vs), sorted(_TASKS))

    def test_a_task_passes_only_when_every_sample_passes(self) -> None:
        """**口径**：通过 = 全部采样都通过。

        用"至少一次通过"的话，`3/3 → 1/3` 会被读成 `unchanged` ——
        一道正在塌的题被记成"没事"。
        """
        # 3 次采样：过、过、不过 ⇒ 2/3 ⇒ **不算通过**
        plan = _all_pass()
        plan["t-ttl"] = (_ANSWERS["t-ttl"], _ANSWERS["t-ttl"], "I don't know.")
        rep = _run(plan, samples=3)
        vs = {v.case_id: v for v in task_verdicts(rep)}
        self.assertFalse(vs["t-ttl"].passed)
        self.assertTrue(vs["t-expire"].passed)

    def test_never_passing_is_attributed_to_the_answer(self) -> None:
        vs = {v.case_id: v for v in task_verdicts(_run(_all_fail()))}
        self.assertEqual(vs["t-ttl"].attribution, ATTR_WRONG)

    def test_sometimes_passing_is_attributed_to_instability(self) -> None:
        """**"不稳定"和"答不对"是两件事**，该动的地方也不同。"""
        plan = _all_pass()
        plan["t-ttl"] = (_ANSWERS["t-ttl"], "I don't know.")
        vs = {v.case_id: v for v in task_verdicts(_run(plan, samples=2))}
        self.assertEqual(vs["t-ttl"].attribution, ATTR_FLAKY)

    def test_a_generation_error_outranks_everything_else(self) -> None:
        """报错是**管线**问题，不是"答得不够好" —— 该动的地方完全不同。

        ⚠️ 要抓 N3（归因优先级反了），必须构造"同一道题有的采样报错、
        有的采样通过"：那样正确代码归 `ATTR_ERROR`，变异（优先看通过）归
        `ATTR_FLAKY`，两者才分得开。`fail_error={"t-ttl": 1}` 让 t-ttl 的第
        0 个采样报错、第 1 个采样按剧本通过。
        """
        plan = _all_pass()
        plan["t-ttl"] = (_ANSWERS["t-ttl"], _ANSWERS["t-ttl"])  # 两个采样都"会过"
        vs = {
            v.case_id: v
            for v in task_verdicts(_run(plan, samples=2, fail_error={"t-ttl": 1}))
        }
        self.assertEqual(vs["t-ttl"].attribution, ATTR_ERROR)

    def test_a_passing_task_has_no_attribution(self) -> None:
        vs = {v.case_id: v for v in task_verdicts(_run(_all_pass()))}
        self.assertEqual(vs["t-expire"].attribution, "")

    def test_unmeasurable_tasks_are_left_out_of_the_verdicts(self) -> None:
        """`points_total == 0` ⇒ `passed` 恒 `False`。

        直接拿去比，它就会以"两次都不通过"的形状落进 `unchanged` ——
        读起来是"已知问题"，真相是"压根没测"。
        """
        rep = _run(_all_pass(), blind=True)
        self.assertIn(_BLIND, unmeasurable(rep))
        self.assertNotIn(_BLIND, [v.case_id for v in task_verdicts(rep)])

    def test_unmeasurable_is_reported_not_swallowed(self) -> None:
        """不可测的题**必须被点名** —— 不点名就等于它们不存在。"""
        rep = _run(_all_pass(), blind=True)
        self.assertEqual(unmeasurable(rep), [_BLIND])
        self.assertIn("不可测的题", render_markdown(rep, rep))

    def test_baseline_is_derived_from_the_same_definition(self) -> None:
        """两侧**必须同源**。

        各算一份的话，"上次过没过"和"这次过没过"会用两套口径 ——
        而 `compare()` 是拿这两个映射对减的，对减出来的差**全是口径的差**。

        ⚠️ 用"先不过后过"的顺序：`{s.task_id: s.passed for s in items}`
        这种"最后一条采样代表这道题"的写法在这里会给出 `True`，
        而正确口径（全部采样通过）是 `False`。
        用 `samples=1` 的话两种写法结果一样 —— 那就测不出东西。
        """
        plan = _all_pass()
        plan["t-ttl"] = ("I don't know.", _ANSWERS["t-ttl"])
        rep = _run(plan, samples=2)
        self.assertFalse(baseline(rep)["t-ttl"], "最后一条采样不能代表这道题")
        self.assertEqual(baseline(rep), {v.case_id: v.passed for v in task_verdicts(rep)})

    def test_nothing_unmeasurable_means_an_empty_list(self) -> None:
        self.assertEqual(unmeasurable(_run(_all_pass())), [])


# ---------------------------------------------------------------- 回归语义


class TestClassification(unittest.TestCase):
    """复用 `agent_evaluation.regression` 的那四条规则。"""

    def test_pass_then_fail_is_a_regression(self) -> None:
        ds = deltas(_run(_all_pass()), _run(_drop(_all_pass(), "t-ttl")))
        kinds = {d.case_id: d.kind for d in ds}
        self.assertEqual(kinds["t-ttl"], "regressed")
        self.assertEqual(kinds["t-expire"], "unchanged")

    def test_fail_then_pass_is_an_improvement(self) -> None:
        ds = deltas(_run(_all_fail()), _run(_all_pass()))
        self.assertTrue(all(d.kind == "improved" for d in ds), [d.kind for d in ds])

    def test_failing_in_both_runs_is_unchanged_not_a_regression(self) -> None:
        """**本模块最要紧的一条。**

        "两次都不通过"是 **backlog**，不是回归。报成回归的话，
        回归信号会淹没在噪声里 —— 那正是"每轮 3 条红、其实 0 个新问题"
        这种疲惫感的来源。
        """
        ds = deltas(_run(_all_fail()), _run(_all_fail()))
        self.assertTrue(all(d.kind == "unchanged" for d in ds), [d.kind for d in ds])
        self.assertEqual(count_kinds(ds)["regressed"], 0)

    def test_passing_in_both_runs_is_unchanged(self) -> None:
        ds = deltas(_run(_all_pass()), _run(_all_pass()))
        self.assertTrue(all(d.kind == "unchanged" for d in ds))

    def test_a_case_absent_from_the_baseline_is_new(self) -> None:
        before = _run(_all_pass(("t-expire", "t-ttl")))
        after = _run(_all_pass())
        kinds = {d.case_id: d.kind for d in deltas(before, after)}
        self.assertEqual(kinds["t-persist"], "new")
        self.assertEqual(kinds["t-expire"], "unchanged")

    def test_new_cases_are_not_regressions(self) -> None:
        """题库加了题**不是退步** —— 加题会让"通过率"掉，那是分母的事。"""
        before = _run(_all_pass(("t-expire", "t-ttl")))
        after = _run(_all_pass())
        self.assertEqual(count_kinds(deltas(before, after))["regressed"], 0)

    def test_a_one_sample_flip_is_a_regression_and_the_ratio_says_so(self) -> None:
        """口径的代价：一次采样翻转就算回归。

        所以**必须**同时印前后比例，让读者自己判这是抖动还是塌了 ——
        判定给结论，比例给分辨力。
        """
        before = _run({t: (_ANSWERS[t],) * 3 for t in _TASKS}, samples=3)
        plan = {t: (_ANSWERS[t],) * 3 for t in _TASKS}
        plan["t-ttl"] = (_ANSWERS["t-ttl"], _ANSWERS["t-ttl"], "I don't know.")
        after = _run(plan, samples=3)
        ds = deltas(before, after)
        kinds = {d.case_id: d.kind for d in ds}
        self.assertEqual(kinds["t-ttl"], "regressed")
        md = render_markdown(before, after)
        self.assertIn("3/3", md)
        self.assertIn("2/3", md)

    def test_a_task_that_was_never_measured_produces_no_delta(self) -> None:
        """不可测的题**不产生任何 Delta** —— 它不在对比里。"""
        ds = deltas(_run(_all_pass(), blind=True), _run(_all_pass(), blind=True))
        self.assertNotIn(_BLIND, [d.case_id for d in ds])

    def test_removed_cases_are_named_even_though_compare_cannot_see_them(self) -> None:
        """`compare()` 只遍历**这次**的结果 ⇒ 上次有的题会静默消失。"""
        before = _run(_all_pass())
        after = _run(_all_pass(("t-expire", "t-ttl")))
        self.assertEqual(only_in_before(before, after), ["t-persist"])
        self.assertNotIn("t-persist", [d.case_id for d in deltas(before, after)])


# ---------------------------------------------------------------- 可比性


class TestComparability(unittest.TestCase):
    """配置变了就不是"这次 vs 上次"。**一次报全部原因**。"""

    def test_two_identical_runs_are_comparable(self) -> None:
        self.assertEqual(check_comparable(_run(_all_pass()), _run(_all_pass())), [])

    def test_refuses_different_answerer(self) -> None:
        a = _run(_all_pass())
        b = _run(_all_pass(), answerer_name="other")
        self.assertTrue(any("答案器" in p for p in check_comparable(a, b)))

    def test_refuses_different_top_k(self) -> None:
        a = _run(_all_pass(), top_k=3)
        b = _run(_all_pass(), top_k=2)
        self.assertTrue(any("top_k" in p for p in check_comparable(a, b)))

    def test_refuses_different_context_budget(self) -> None:
        a = _run(_all_pass(), context_budget=DEFAULT_CONTEXT_BUDGET)
        # ⚠️ 不能只改窗口：`reserved_for_output` 必须 < total（内核硬约束）。
        b = _run(_all_pass(), context_budget=4096)
        self.assertTrue(any("context_budget" in p for p in check_comparable(a, b)))

    def test_refuses_different_reserved_for_output(self) -> None:
        a = _run(_all_pass())
        b = _run(_all_pass(), reserved_for_output=0)
        self.assertTrue(any("reserved_for_output" in p for p in check_comparable(a, b)))

    def test_refuses_different_chars_per_token(self) -> None:
        """它是**语料的属性** —— 变了说明语料或假设变了。"""
        a = _run(_all_pass())
        b = _run(_all_pass(), chars_per_token=3)
        self.assertTrue(any("chars_per_token" in p for p in check_comparable(a, b)))

    def test_refuses_different_topic(self) -> None:
        a = _run(_all_pass())
        b = dataclasses.replace(a, topic="kubernetes")
        self.assertTrue(any("topic" in p for p in check_comparable(a, b)))

    def test_refuses_different_retriever(self) -> None:
        a = _run(_all_pass())
        b = dataclasses.replace(a, retriever="dense")
        self.assertTrue(any("检索器" in p for p in check_comparable(a, b)))

    def test_refuses_different_corpus_size(self) -> None:
        a = _run(_all_pass())
        b = dataclasses.replace(a, corpus_chunks=99)
        self.assertTrue(any("corpus_chunks" in p for p in check_comparable(a, b)))

    def test_refuses_calibration_mixed_with_a_real_run(self) -> None:
        """拿判据校准和模型成绩比，比出来的差是指标的差。"""
        a = _run(_all_pass())
        b = dataclasses.replace(a, calibration=not a.calibration)
        self.assertTrue(any("calibration" in p for p in check_comparable(a, b)))

    def test_refuses_a_rewritten_question(self) -> None:
        """题号相同、问句不同 —— `compare()` 看不见这一层。

        它只比 `case_id` 和 `passed`，照样会给出 `regressed`，而那毫无意义：
        同一道题号下是两道不同的题。
        """
        a = _run(_all_pass())
        bad = dataclasses.replace(a.items[0], question="完全换了一个问句？")
        b = dataclasses.replace(a, items=(bad,) + a.items[1:])
        self.assertTrue(any("问句" in p for p in check_comparable(a, b)))

    def test_refuses_changed_required_points(self) -> None:
        """『通过』的含义变了（原来是全中这一组，现在是全中另一组）。"""
        a = _run(_all_pass())
        bad = dataclasses.replace(a.items[0], points_total=a.items[0].points_total + 1)
        b = dataclasses.replace(a, items=(bad,) + a.items[1:])
        self.assertTrue(any("必答要点" in p for p in check_comparable(a, b)))

    def test_reports_every_problem_at_once(self) -> None:
        """只报第一个的话，人会改一项再跑一次 —— 那会把人训练成"多跑几次"。"""
        a = _run(_all_pass())
        b = dataclasses.replace(a, top_k=9, topic="other", corpus_chunks=1)
        problems = check_comparable(a, b)
        self.assertGreaterEqual(len(problems), 3, problems)

    def test_adding_questions_is_not_a_reason_to_refuse(self) -> None:
        """题库加题不是不可比 —— 它只是让新题落进 `new`。"""
        before = _run(_all_pass(("t-expire", "t-ttl")))
        after = _run(_all_pass())
        self.assertEqual(check_comparable(before, after), [])

    def test_removing_questions_is_not_a_reason_to_refuse_either(self) -> None:
        before = _run(_all_pass())
        after = _run(_all_pass(("t-expire", "t-ttl")))
        self.assertEqual(check_comparable(before, after), [])

    def test_reversed_arguments_are_flagged(self) -> None:
        """传反了 ⇒ 所有 regressed/improved 的方向都会反过来。必须说。"""
        a = _run(_all_pass())
        b = dataclasses.replace(a, generated_at="2020-01-01T00:00:00+0800")
        self.assertTrue(looks_reversed(a, b))
        self.assertFalse(looks_reversed(b, a))

    def test_reversed_flag_is_not_a_refusal(self) -> None:
        """只是**告警**，不是拒绝 —— 拒绝会在误报时挡住一次合法的对比。"""
        a = _run(_all_pass())
        b = dataclasses.replace(a, generated_at="2020-01-01T00:00:00+0800")
        self.assertEqual(check_comparable(a, b), [])
        self.assertIn("传反", render_markdown(a, b))

    def test_a_changed_prompt_is_refused(self) -> None:
        """**提示词是被测系统的一部分**：换了它 ⇒ 变的是系统，不是它退步了。

        它决定模型会不会自述引用、也决定答案的详略与覆盖面 ——
        不拦住的话，「换 prompt 的效果」会被读成「系统退步/进步」。
        """
        a = dataclasses.replace(_run(_all_pass()), prompt_id="v1-aaaaaaaa")
        b = dataclasses.replace(_run(_all_pass()), prompt_id="v2-bbbbbbbb")
        self.assertTrue(any("提示词" in p for p in check_comparable(a, b)))

    def test_an_unrecorded_prompt_is_refused_not_assumed_equal(self) -> None:
        """老报告没记 `prompt_id` ⇒ **无法确认相同**。

        ⚠️ 把空当成「没有差异」放行就是又一次静默。宁可拒绝。
        """
        a = dataclasses.replace(_run(_all_pass()), prompt_id="")
        b = dataclasses.replace(_run(_all_pass()), prompt_id="v2-bbbbbbbb")
        self.assertTrue(any("提示词" in p for p in check_comparable(a, b)))

    def test_the_same_prompt_is_comparable(self) -> None:
        """同版本 ⇒ 可比（别把门做成一律拒绝，那样它就没人用了）。"""
        a = dataclasses.replace(_run(_all_pass()), prompt_id="v2-bbbbbbbb")
        b = dataclasses.replace(_run(_all_pass()), prompt_id="v2-bbbbbbbb")
        self.assertEqual(check_comparable(a, b), [])


# ---------------------------------------------------------------- 渲染


class TestRendering(unittest.TestCase):
    def test_refusal_prints_no_metric_numbers(self) -> None:
        """拒绝时**一个指标数字都不能出现** —— 出现了就会被读成结论。"""
        a = _run(_all_pass())
        b = dataclasses.replace(a, top_k=9)
        md = render_markdown(a, b)
        self.assertIn("不可比", md)
        self.assertNotIn("| 指标 | 方向 |", md)
        self.assertNotIn("要点召回", md)
        self.assertNotIn("## 总览", md)

    def test_the_report_states_which_definition_of_passed_was_used(self) -> None:
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertIn(PASS_DEFINITION, md)
        self.assertIn("全部采样都通过", md)

    def test_regressions_are_the_headline(self) -> None:
        md = render_markdown(_run(_all_pass()), _run(_drop(_all_pass(), "t-ttl")))
        self.assertIn("回归", md)
        self.assertIn("t-ttl", md)
        # 回归那一段必须在"改善 / 已知问题"之前 —— 报警的东西不能埋在下面。
        self.assertLess(md.index("## ⚠️ 回归"), md.index("## 改善"))

    def test_backlog_is_labeled_and_not_counted_as_regression(self) -> None:
        """两次都不过的题**必须**出现在报告里，但**不能**被叫成回归。"""
        md = render_markdown(_run(_all_fail()), _run(_all_fail()))
        self.assertIn("已知问题", md)
        self.assertIn("backlog", md)
        self.assertIn("不是这次退步", md)
        self.assertIn("**没有回归。**", md)
        # 总览里 regressed 必须是 0
        self.assertIn("| **regressed**（上次过 → 这次不过） | **0** |", md)

    def test_clean_run_says_so_without_claiming_everything_is_fine(self) -> None:
        """「没有回归」**不等于**「一切都好」—— 后者是个会误导人的句号。"""
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertIn("**没有回归。**", md)
        self.assertIn("不等于", md)

    def test_lists_removed_cases(self) -> None:
        before = _run(_all_pass())
        after = _run(_all_pass(("t-expire", "t-ttl")))
        md = render_markdown(before, after)
        self.assertIn("只在基线里", md)
        self.assertIn("t-persist", md)
        self.assertIn("静默消失", md)

    def test_names_unmeasurable_tasks(self) -> None:
        md = render_markdown(_run(_all_pass(), blind=True), _run(_all_pass(), blind=True))
        self.assertIn("不可测的题", md)
        self.assertIn(_BLIND, md)

    def test_new_cases_get_their_own_section(self) -> None:
        before = _run(_all_pass(("t-expire", "t-ttl")))
        md = render_markdown(before, _run(_all_pass()))
        self.assertIn("新增的题", md)
        self.assertIn("不算退步", md)

    def test_a_regressed_task_is_not_listed_as_backlog(self) -> None:
        """backlog 段**只**装"两次都不通过"的题 —— 真退步的题不能混进去。

        N17 的变异把筛选条件改成了 `kind == "regressed"`，于是真退步的题
        也会被列进"已知问题"段，把"退步"和"backlog"稀释成同一种东西。
        正确代码下，三道题全都真退步时，backlog 段应是空的（"没有。"）。
        """
        before = _run(_all_pass())
        after = _run(_all_fail())
        md = render_markdown(before, after)
        backlog = md.split("## 已知问题")[1]
        self.assertIn("没有。", backlog)
        self.assertNotIn("t-expire", backlog)

    def test_metric_drift_table_has_both_sides(self) -> None:
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertIn("## 指标漂移", md)
        self.assertIn("| 指标 | 方向 | 之前 | 之后 |", md)
        self.assertIn("要点召回", md)

    def test_metric_drift_says_it_is_a_different_dimension(self) -> None:
        """判定（过/不过）与度量（分数多少）是两个维度 —— 必须说清，
        否则读者会以为漂移表参与了 regressed 的判定。"""
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertIn("两个维度", md)
        self.assertIn("不参与", md)

    def test_pass_at_k_row_is_hidden_when_samples_is_one(self) -> None:
        """`samples == 1` 时它和 pass@1 是同一个数 —— 印两遍会让人以为看错了。"""
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertNotIn("pass@k", md)

    def test_pass_at_k_row_appears_when_samples_above_one(self) -> None:
        plan = {t: (_ANSWERS[t],) * 2 for t in _TASKS}
        md = render_markdown(_run(plan, samples=2), _run(plan, samples=2))
        self.assertIn("pass@k", md)

    def test_unmeasurable_metric_prints_a_dash_not_a_zero(self) -> None:
        """**『没测』不许印成 0。**

        `— → 0.5941` 读起来是"从 0 涨上来了"，而真相是"上次没测"。
        """
        before = _run(_all_fail(), mute_citations=True)   # 不自述引用 ⇒ 不可测
        after = _run(_all_pass())
        self.assertIsNone(before.overall.grounded_rate, "这题应当不可测")
        md = render_markdown(before, after)
        row = next(l for l in md.splitlines() if l.startswith("| 引用有依据率"))
        self.assertIn("—", row)

    def test_cost_row_is_omitted_when_both_sides_are_unmeasured(self) -> None:
        """两边都没测时印两个 0，会被读成"这次没花钱"。"""
        md = render_markdown(_run(_all_pass()), _run(_all_pass()))
        self.assertNotIn("总成本 USD", md)
        self.assertIn("不等于", md)

    def test_cost_is_called_unmeasured_not_free_on_a_real_run(self) -> None:
        """**『没测』不等于『免费』** —— 不印那一行还不够，必须**说出来**。

        非校准跑但成本全是 0，只说明"没人填成本"或"单价没接上"，
        不是"这次没花钱"。沉默的话读者只会看到一个缺行。
        """
        a = _run(_all_pass(), real_run=True)
        self.assertFalse(a.calibration, "这一跑应当是**非校准**")
        md = render_markdown(a, a)
        self.assertNotIn("总成本 USD", md)
        self.assertIn("两边都没测", md)
        self.assertIn("不等于", md)

    def test_which_two_files_were_compared_is_on_the_report(self) -> None:
        """光看 answerer/retriever 读者不知道比的是**哪两次**。"""
        md = render_markdown(
            _run(_all_pass()), _run(_all_pass()),
            before_path="a.json", after_path="b.json",
        )
        self.assertIn("a.json", md)
        self.assertIn("b.json", md)


# ---------------------------------------------------------------- JSON


class TestAsDict(unittest.TestCase):
    def test_json_carries_the_same_classification(self) -> None:
        before = _run(_all_pass())
        after = _run(_drop(_all_pass(), "t-ttl"))
        d = as_dict(before, after)
        self.assertTrue(d["comparable"])
        self.assertEqual(d["regressions"], ["t-ttl"])
        self.assertEqual(d["counts"]["regressed"], 1)
        kinds = {x["case_id"]: x["kind"] for x in d["deltas"]}
        self.assertEqual(kinds["t-ttl"], "regressed")

    def test_json_carries_the_attribution(self) -> None:
        """下游只拿 JSON 的时候，少一层归因就等于少一个该动的地方。"""
        d = as_dict(_run(_all_pass()), _run(_drop(_all_pass(), "t-ttl")))
        hit = next(x for x in d["deltas"] if x["case_id"] == "t-ttl")
        self.assertEqual(hit["attribution"], ATTR_WRONG)

    def test_json_names_removed_and_unmeasurable(self) -> None:
        d = as_dict(_run(_all_pass(), blind=True), _run(_all_pass(("t-expire", "t-ttl")), blind=True))
        self.assertEqual(d["only_in_before"], ["t-persist"])
        self.assertEqual(d["unmeasurable"], [_BLIND])

    def test_json_refuses_without_numbers(self) -> None:
        a = _run(_all_pass())
        d = as_dict(a, dataclasses.replace(a, top_k=9))
        self.assertFalse(d["comparable"])
        self.assertTrue(d["problems"])
        self.assertNotIn("deltas", d)
        self.assertNotIn("counts", d)

    def test_json_keeps_none_as_none(self) -> None:
        """不可测在 JSON 里也必须是 `None`，不是 `0.0`。"""
        d = as_dict(_run(_all_fail(), mute_citations=True), _run(_all_pass()))
        self.assertIsNone(d["metrics"]["grounded_rate"]["before"])


# ---------------------------------------------------------------- CLI


class TestCli(unittest.TestCase):
    def _save(self, d: Path, name: str, rep: AnswerReport) -> Path:
        p = d / name
        rep.save(p)
        return p

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_when_comparable(self) -> None:
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass()))
            b = self._save(d, "b.json", _run(_drop(_all_pass(), "t-ttl")))
            code, out, _ = self._run_cli([str(a), str(b)])
        self.assertEqual(code, 0)
        self.assertIn("回归", out)

    def test_exit_two_when_not_comparable(self) -> None:
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass(), top_k=3))
            b = self._save(d, "b.json", _run(_all_pass(), top_k=2))
            code, out, err = self._run_cli([str(a), str(b)])
        self.assertEqual(code, 2)
        self.assertIn("不可比", err)
        self.assertEqual(out, "")   # 拒绝时 stdout 一个数字都不给

    def test_writes_markdown_and_json(self) -> None:
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass()))
            b = self._save(d, "b.json", _run(_drop(_all_pass(), "t-ttl")))
            md, js = d / "out.md", d / "out.json"
            code, _, _ = self._run_cli(
                [str(a), str(b), "--out", str(md), "--json", str(js)]
            )
            self.assertEqual(code, 0)
            self.assertIn("回归", md.read_text(encoding="utf-8"))
            payload = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(payload["regressions"], ["t-ttl"])
        # 报告里必须能看出比的是哪两份文件
        self.assertEqual(payload["before"]["path"], str(a))
        self.assertEqual(payload["after"]["path"], str(b))

    def test_refusal_still_writes_the_requested_files(self) -> None:
        """`--out` 是**显式给的** flag —— 静默忽略它属于"承诺了却没交付"。

        "这次为什么不能比"本身就是该留档的结论。但 `→ 路径` 走 **stderr**，
        stdout 保持"一个数字都不给"。
        """
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass(), top_k=3))
            b = self._save(d, "b.json", _run(_all_pass(), top_k=2))
            md, js = d / "refused.md", d / "refused.json"
            code, out, err = self._run_cli(
                [str(a), str(b), "--out", str(md), "--json", str(js)]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out, "", "stdout 不许有任何东西")
            self.assertTrue(md.exists(), "拒绝时也要把报告写下来")
            self.assertIn("不可比", md.read_text(encoding="utf-8"))
            payload = json.loads(js.read_text(encoding="utf-8"))
        self.assertFalse(payload["comparable"])
        self.assertTrue(payload["problems"])

    def test_exit_three_on_unreadable_report(self) -> None:
        """读不回来是**输入问题**，不是程序缺陷 —— 印一句话，不吐 traceback。"""
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass()))
            bad = d / "bad.json"
            bad.write_text("{ not json", encoding="utf-8")
            code, _, err = self._run_cli([str(a), str(bad)])
        self.assertEqual(code, 3)
        self.assertIn("读不回来", err)
        self.assertNotIn("Traceback", err)

    def test_exit_three_on_old_format_report(self) -> None:
        """旧格式报告缺 `citation` / `context_tokens`，`load()` 拒绝并说清后果。"""
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass()))
            old = d / "old.json"
            raw = json.loads(a.read_text(encoding="utf-8"))
            del raw["items"][0]["citation"]
            old.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            code, _, err = self._run_cli([str(a), str(old)])
        self.assertEqual(code, 3)
        self.assertIn("旧格式", err)

    def test_allow_non_comparable_still_exits_two(self) -> None:
        """逃生门只给排查用 —— 退出码仍然是 2，CI 不会把它当通过。"""
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass(), top_k=3))
            b = self._save(d, "b.json", _run(_all_pass(), top_k=2))
            code, out, _ = self._run_cli([str(a), str(b), "--allow-non-comparable"])
        self.assertEqual(code, 2)
        self.assertIn("不可比", out)

    def test_cli_does_not_rewrite_the_reports(self) -> None:
        """对比是**只读**的 —— 它不该改动被对比的报告。"""
        with _Tmp() as d:
            a = self._save(d, "a.json", _run(_all_pass()))
            b = self._save(d, "b.json", _run(_all_pass()))
            before = (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))
            self._run_cli([str(a), str(b)])
            after = (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
