"""M107 · 业务指标 → Prometheus 文本（M7 / M9）。

补的空洞：§M7 说"HPA 不能只看 CPU → 业务指标（Queue Depth）"，
但 AgentOS 此前**一个指标都吐不出来**。这一层只做格式（渲染），
取数在组合根、暴露在 `/metrics`。钉住一条不变量：

    M-1  指标值必须有限 —— NaN / ±Inf 不是"一个很大的数"，是"没有数"
"""
from __future__ import annotations

import unittest

from packages.agent_api.metrics import (
    PENDING_APPROVALS,
    PENDING_EXECUTIONS,
    RUNNING_EXECUTIONS,
    SUSPENDED_EXECUTIONS,
    Metric,
    business_metrics,
    render_prometheus,
)


class MetricTest(unittest.TestCase):
    def test_it_renders_prometheus_text(self) -> None:
        text = Metric("agentos_x", "help text", 3).render()
        self.assertEqual(
            text,
            "# HELP agentos_x help text\n# TYPE agentos_x gauge\nagentos_x 3\n",
        )

    def test_an_integer_value_is_not_rendered_as_a_float(self) -> None:
        self.assertIn("agentos_x 5\n", Metric("agentos_x", "h", 5.0).render())

    def test_a_fractional_value_is_kept(self) -> None:
        self.assertIn("agentos_x 0.5\n", Metric("agentos_x", "h", 0.5).render())

    def test_a_counter_type_is_allowed(self) -> None:
        self.assertIn("# TYPE agentos_x counter", Metric("agentos_x", "h", 1, type="counter").render())

    def test_a_bad_name_is_refused(self) -> None:
        for name in ("1bad", "has space", "has-dash", ""):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Metric(name, "h", 1)

    def test_a_bad_type_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Metric("agentos_x", "h", 1, type="histogram")

    def test_m1_a_non_finite_value_is_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Metric("agentos_x", "h", value)

    def test_m1_a_bool_is_not_a_number(self) -> None:
        with self.assertRaises(ValueError):
            Metric("agentos_x", "h", True)

    def test_a_newline_in_help_does_not_break_the_format(self) -> None:
        text = Metric("agentos_x", "line1\nline2", 1).render()
        self.assertEqual(text.count("\n"), 3, text)          # HELP/TYPE/值 三行
        self.assertIn("line1 line2", text)


class RenderTest(unittest.TestCase):
    def test_it_joins_several_metrics(self) -> None:
        text = render_prometheus((Metric("a", "ha", 1), Metric("b", "hb", 2)))
        self.assertIn("# HELP a ha", text)
        self.assertIn("# HELP b hb", text)
        self.assertIn("b 2", text)


class BusinessMetricsTest(unittest.TestCase):
    def test_the_four_business_gauges_are_exposed(self) -> None:
        metrics = business_metrics(
            pending_executions=7,
            running_executions=2,
            suspended_executions=1,
            pending_approvals=3,
        )
        by_name = {m.name: m for m in metrics}
        self.assertEqual(
            set(by_name),
            {PENDING_EXECUTIONS, RUNNING_EXECUTIONS, SUSPENDED_EXECUTIONS, PENDING_APPROVALS},
        )
        self.assertEqual(by_name[PENDING_EXECUTIONS].value, 7)
        self.assertEqual(by_name[PENDING_APPROVALS].value, 3)

    def test_the_queue_depth_metric_says_what_it_is_for(self) -> None:
        text = render_prometheus(business_metrics(pending_executions=7))
        self.assertIn(PENDING_EXECUTIONS, text)
        self.assertIn("queue depth", text.lower())


if __name__ == "__main__":
    unittest.main()
