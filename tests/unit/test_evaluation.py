"""评估平台的判定与归因（M52 / M8）。

这里测的是**判定逻辑**，不打服务 —— 真服务的往返由
`python -m apps.eval run tests/eval/smoke.json` 覆盖（真跑过）。

最要紧的一条是"两次都不通过算 unchanged"：
那决定了回归信号会不会被 backlog 噪声淹没。
"""
from __future__ import annotations

import unittest

from packages.agent_evaluation import (
    ATTR_ACTION,
    ATTR_APPROVAL,
    ATTR_DEGRADED,
    ATTR_STATUS,
    ATTR_TOOL,
    Case,
    Dataset,
    Expect,
    Facts,
    evaluate,
)
from packages.agent_evaluation.regression import compare, regressions, summarize


def _facts(**kw) -> Facts:
    base = {
        "run_id": "r1", "status": "completed", "outcome": "finished",
        "step_count": 2, "actions": ("llm_call", "tool_call"),
        "approvals": 1, "approved_by": ("evaluator",),
        "tools": ("note.write",), "side_effects": ("write",),
        "degraded": False, "fallback_count": 0,
    }
    base.update(kw)
    return Facts(**base)


def _case(expect_kw=None, case_id="c1") -> Case:
    return Case(id=case_id, request="x", expect=Expect(**(expect_kw or {})))


class TestAttribution(unittest.TestCase):
    """失败要说清"卡在哪一步"，不能只说"不通过"。"""

    def test_wrong_status(self):
        v = evaluate(_case({"status": "completed"}), _facts(status="cancelled"))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_STATUS)
        self.assertIn("cancelled", v.detail)

    def test_expected_gate_but_none_happened(self):
        v = evaluate(_case({"approval_required": True}), _facts(approvals=0))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_APPROVAL)

    def test_unexpected_gate(self):
        v = evaluate(_case({"approval_required": False}), _facts(approvals=1))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_APPROVAL)

    def test_missing_action(self):
        v = evaluate(_case({"actions": ("tool_call",)}), _facts(actions=("llm_call",)))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_ACTION)

    def test_missing_tool(self):
        v = evaluate(_case({"tools": ("note.write",)}), _facts(tools=()))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_TOOL)

    def test_model_degraded_is_caught(self):
        """降级是最难发现的一类 —— 它不报错，只是悄悄换了个模型。"""
        v = evaluate(_case({"not_degraded": True}), _facts(degraded=True))
        self.assertFalse(v.passed)
        self.assertEqual(v.attribution, ATTR_DEGRADED)

    def test_empty_expect_only_requires_termination(self):
        """什么都不要求时，只要跑到终态就算过（它本身是条冒烟用例）。"""
        self.assertTrue(evaluate(_case(), _facts()).passed)

    def test_passing_case_has_no_attribution(self):
        v = evaluate(_case({"status": "completed", "approval_required": True}), _facts())
        self.assertTrue(v.passed)
        self.assertEqual(v.attribution, "")


class TestRegressionClassification(unittest.TestCase):
    """回归信号不能被 backlog 噪声淹没。"""

    def _v(self, cid, passed, attr=""):
        return type("V", (), {"case_id": cid, "passed": passed, "attribution": attr})()

    def test_pass_to_fail_is_regressed(self):
        d = compare({"c1": True}, [self._v("c1", False, ATTR_TOOL)])
        self.assertEqual([x.kind for x in d], ["regressed"])
        self.assertEqual(len(regressions(d)), 1)

    def test_fail_to_pass_is_improved(self):
        d = compare({"c1": False}, [self._v("c1", True)])
        self.assertEqual([x.kind for x in d], ["improved"])
        self.assertEqual(regressions(d), [])

    def test_fail_to_fail_is_unchanged_not_regression(self):
        """一直失败的用例是 backlog，不是新退步。

        报成回归的话，"每轮 3 条红、其实 0 条新问题"会让人不再看回归报告 ——
        那等于把这个信号废掉。
        """
        d = compare({"c1": False}, [self._v("c1", False, ATTR_TOOL)])
        self.assertEqual([x.kind for x in d], ["unchanged"])
        self.assertEqual(regressions(d), [])

    def test_unknown_case_is_new(self):
        d = compare({}, [self._v("c1", True)])
        self.assertEqual([x.kind for x in d], ["new"])

    def test_summarize_groups_by_attribution(self):
        vs = [self._v("a", False, ATTR_TOOL), self._v("b", False, ATTR_TOOL),
              self._v("c", True)]
        s = summarize(vs)
        self.assertEqual(s["passed"], 1)
        self.assertEqual(s["failed"], 2)
        self.assertEqual(s["by_attribution"], {"missing_tool": 2})


class TestDatasetLoading(unittest.TestCase):
    def test_empty_dataset_is_refused(self):
        """空用例集会静默"全部通过" —— 那是没有评估，不是评估通过。"""
        with self.assertRaises(ValueError):
            Dataset.from_dict({"name": "x", "agent_id": "a", "cases": []})

    def test_parses_cases(self):
        d = Dataset.from_dict({
            "name": "smoke", "agent_id": "agent-it",
            "cases": [{"id": "c1", "request": "compute 6*7",
                       "expect": {"status": "completed", "tools": ["note.write"]}}],
        })
        self.assertEqual(d.name, "smoke")
        self.assertEqual(len(d.cases), 1)
        self.assertEqual(d.cases[0].expect.tools, ("note.write",))


if __name__ == "__main__":
    unittest.main()
