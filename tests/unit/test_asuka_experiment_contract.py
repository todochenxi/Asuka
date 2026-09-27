"""Asuka 实验环：噪声门槛 + 三态结论。判据是"它敢说分辨不出"。"""
from __future__ import annotations

import unittest

from asuka.answers import AnswerGroup, AnswerReport
from asuka.experiment import (
    BETTER,
    INDISTINGUISHABLE,
    WORSE,
    Arm,
    ExperimentError,
    check_comparable,
    classify,
    verdicts,
)


def _report(*, retriever="bm25", top_k=10, samples=3, prompt_id="v1-abc",
            recall=0.2, pass1=0.0, topic="redis", tasks=("t1", "t2")) -> AnswerReport:
    from asuka.answers import AnswerScore

    return AnswerReport(
        topic=topic, answerer="agentos", retriever=retriever, top_k=top_k,
        samples_per_task=samples, prompt_id=prompt_id, corpus_chunks=100,
        items=tuple(
            AnswerScore(task_id=t, difficulty="simple", question="q", points_total=2)
            for t in tasks
        ),
        overall=AnswerGroup(mean_recall=recall, pass_at_1=pass1, pass_at_k=pass1 + 0.02),
    )


class ClassifyTest(unittest.TestCase):
    def test_a_delta_above_noise_is_a_real_change(self) -> None:
        self.assertEqual(classify(+0.10, 0.033), BETTER)
        self.assertEqual(classify(-0.10, 0.033), WORSE)

    def test_a_delta_within_noise_is_indistinguishable(self) -> None:
        self.assertEqual(classify(+0.02, 0.033), INDISTINGUISHABLE)
        self.assertEqual(classify(-0.02, 0.033), INDISTINGUISHABLE)

    def test_a_delta_exactly_at_the_threshold_is_indistinguishable(self) -> None:
        """等于门槛不算"超过"—— 严格大于才算。"""
        self.assertEqual(classify(0.033, 0.033), INDISTINGUISHABLE)

    def test_a_negative_noise_is_refused(self) -> None:
        with self.assertRaises(ExperimentError):
            classify(0.1, -0.1)


class ComparableTest(unittest.TestCase):
    def test_only_the_vary_field_may_differ(self) -> None:
        arms = [Arm("a", _report(retriever="bm25")), Arm("b", _report(retriever="dense"))]
        self.assertEqual(check_comparable(arms, vary="retriever"), [])

    def test_a_second_difference_is_refused_and_named(self) -> None:
        arms = [Arm("a", _report(retriever="bm25")), Arm("b", _report(retriever="dense", samples=1))]
        problems = check_comparable(arms, vary="retriever")
        self.assertTrue(any("samples_per_task" in p for p in problems), problems)

    def test_a_different_task_set_is_refused(self) -> None:
        arms = [Arm("a", _report()), Arm("b", _report(retriever="dense", tasks=("t1", "t3")))]
        problems = check_comparable(arms, vary="retriever")
        self.assertTrue(any("题集" in p for p in problems), problems)

    def test_varying_an_unknown_field_is_refused(self) -> None:
        arms = [Arm("a", _report()), Arm("b", _report())]
        with self.assertRaises(ExperimentError):
            check_comparable(arms, vary="not_a_field")

    def test_one_arm_is_not_an_experiment(self) -> None:
        with self.assertRaises(ExperimentError):
            check_comparable([Arm("a", _report())], vary="retriever")


class VerdictTest(unittest.TestCase):
    def test_a_big_delta_with_noise_is_better(self) -> None:
        arms = [Arm("base", _report(recall=0.20)), Arm("v2", _report(recall=0.40, retriever="x"))]
        vs = verdicts(arms, "base", noise=0.033, vary="retriever")
        recall = [v for v in vs if v.metric == "mean_recall"][0]
        self.assertEqual(recall.conclusion, BETTER)
        self.assertAlmostEqual(recall.delta, 0.20)

    def test_a_small_delta_with_noise_is_indistinguishable(self) -> None:
        arms = [Arm("base", _report(recall=0.20)), Arm("v2", _report(recall=0.22, retriever="x"))]
        vs = verdicts(arms, "base", noise=0.033, vary="retriever")
        recall = [v for v in vs if v.metric == "mean_recall"][0]
        self.assertEqual(recall.conclusion, INDISTINGUISHABLE)

    def test_without_noise_it_only_reports_deltas(self) -> None:
        arms = [Arm("base", _report(recall=0.20)), Arm("v2", _report(recall=0.40, retriever="x"))]
        vs = verdicts(arms, "base", noise=None, vary="retriever")
        recall = [v for v in vs if v.metric == "mean_recall"][0]
        self.assertEqual(recall.conclusion, "unmeasurable")
        self.assertAlmostEqual(recall.delta, 0.20)  # 差值照样印

    def test_a_missing_baseline_is_refused(self) -> None:
        arms = [Arm("a", _report())]
        with self.assertRaises(ExperimentError):
            verdicts(arms, "nope", noise=0.03, vary="retriever")

    def test_a_missing_metric_is_unmeasurable_not_zero(self) -> None:
        """缺数不许当 0（同 answers._fmt_opt）。`AnswerGroup` 是 frozen，用 dataclasses.replace。"""
        from dataclasses import replace

        base = _report(recall=0.20)
        arm = _report(recall=0.20, retriever="x")
        arm = replace(arm, overall=replace(arm.overall, mean_recall=None))
        vs = verdicts([Arm("base", base), Arm("v2", arm)], "base", noise=0.033, vary="retriever")
        recall = [v for v in vs if v.metric == "mean_recall"][0]
        self.assertEqual(recall.conclusion, "unmeasurable")
        self.assertIsNone(recall.delta)


class RenderTest(unittest.TestCase):
    def test_the_report_states_the_noise_threshold_and_the_third_state(self) -> None:
        from asuka.experiment import render_markdown

        arms = [Arm("base", _report(recall=0.20)), Arm("v2", _report(recall=0.22, retriever="x"))]
        vs = verdicts(arms, "base", noise=0.033, vary="retriever")
        md = render_markdown(arms, vs, baseline_label="base", noise=0.033)
        self.assertIn("0.033", md)
        self.assertIn("分辨不出", md)
        # 它**声明**不输出最优，且正文里不出现"最优配置"这种建议
        self.assertIn("绝不输出「最优配置」", md)
        self.assertNotIn("推荐", md)

    def test_without_noise_it_says_so(self) -> None:
        from asuka.experiment import render_markdown

        arms = [Arm("base", _report()), Arm("v2", _report(retriever="x"))]
        vs = verdicts(arms, "base", noise=None, vary="retriever")
        md = render_markdown(arms, vs, baseline_label="base", noise=None)
        self.assertIn("未提供", md)


if __name__ == "__main__":
    unittest.main()
