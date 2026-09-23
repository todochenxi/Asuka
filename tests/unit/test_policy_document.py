"""M101：Policy-as-Code（OPA 风格）—— 策略是一份**数据**（基线 §23 / M9）。

此前策略只能由 Python 代码构造，改策略 = 改代码 + 重新部署。这里补上
"数据 → 决策点"那一段，并钉住三条不变量：

    P-1  未知 effect / 字段 / action_type 在**加载期拒绝**（不静默忽略）
    P-2  `default` 必须显式写下来（fail-closed 不是可以省的默认值）
    P-3  决策点与执行点分离（本模块只产出 PolicyEngine，不碰 Harness）
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_harness import (
    PolicyContext,
    PolicyDocument,
    PolicyDocumentError,
    PolicyEngine,
    Verdict,
)


def act(
    action_type: ActionType = ActionType.TOOL_CALL,
    tool: str = "",
    risk: RiskLevel = RiskLevel.LOW,
) -> Action:
    payload = {"tool": tool} if tool else {}
    return Action(run_id="r1", action_type=action_type, payload=payload, risk_level=risk)


class LoadTest(unittest.TestCase):
    def test_a_document_compiles_into_a_decision_point(self) -> None:
        doc = PolicyDocument.from_mapping(
            {
                "default": "deny",
                "rules": [
                    {
                        "name": "read-only-tools",
                        "effect": "allow",
                        "when": {"action_types": ["tool_call"], "tools": ["kb.search"]},
                    },
                    {
                        "name": "writes-need-a-human",
                        "effect": "require_approval",
                        "reason": "write tools are gated",
                        "when": {"tools": ["kb.write"]},
                    },
                ],
            }
        )
        engine = doc.compile()
        self.assertIsInstance(engine, PolicyEngine)

        allowed = engine.evaluate(act(tool="kb.search"))
        self.assertIs(allowed.verdict, Verdict.ALLOW)
        self.assertEqual(allowed.rule, "read-only-tools")

        gated = engine.evaluate(act(tool="kb.write"))
        self.assertIs(gated.verdict, Verdict.REQUIRE_APPROVAL)
        self.assertEqual(gated.rule, "writes-need-a-human")

        # 没有规则命中 → 文档显式声明的 default（deny）
        self.assertIs(engine.evaluate(act(tool="other")).verdict, Verdict.DENY)

    def test_attributes_conditions_match_the_context(self) -> None:
        doc = PolicyDocument.from_mapping(
            {
                "default": "allow",
                "rules": [
                    {
                        "name": "prod-freeze",
                        "effect": "deny",
                        "reason": "prod is frozen",
                        "when": {"attributes": {"env": "prod"}},
                    }
                ],
            }
        )
        engine = doc.compile()
        self.assertIs(
            engine.evaluate(act(), PolicyContext(attributes={"env": "prod"})).verdict,
            Verdict.DENY,
        )
        self.assertIs(
            engine.evaluate(act(), PolicyContext(attributes={"env": "dev"})).verdict,
            Verdict.ALLOW,
        )

    def test_priority_decides_the_winner(self) -> None:
        doc = PolicyDocument.from_mapping(
            {
                "default": "deny",
                "rules": [
                    {"name": "low", "effect": "allow", "priority": 1, "when": {"tools": ["t"]}},
                    {
                        "name": "high",
                        "effect": "deny",
                        "reason": "wins",
                        "priority": 9,
                        "when": {"tools": ["t"]},
                    },
                ],
            }
        )
        self.assertEqual(doc.compile().evaluate(act(tool="t")).rule, "high")


class StrictLoadTest(unittest.TestCase):
    """P-1 / P-2：读不懂的文档宁可加载失败，也不许被"尽量理解"。"""

    def _bad(self, data: dict) -> PolicyDocumentError:
        with self.assertRaises(PolicyDocumentError) as ctx:
            PolicyDocument.from_mapping(data)
        return ctx.exception

    def test_an_unknown_effect_is_refused(self) -> None:
        err = self._bad(
            {"default": "deny", "rules": [{"name": "r", "effect": "audit"}]}
        )
        self.assertIn("audit", str(err))

    def test_an_unknown_top_level_key_is_refused(self) -> None:
        err = self._bad({"default": "deny", "rule": []})
        self.assertIn("rule", str(err))

    def test_a_missing_default_is_refused(self) -> None:
        """P-2：省略 default = 悄悄把 fail-closed 变成 fail-open。"""
        err = self._bad({"rules": []})
        self.assertIn("default", str(err))

    def test_an_unknown_when_key_is_refused(self) -> None:
        """少写一个 s（`tool` 而非 `tools`）不许退化成"没有约束"。"""
        err = self._bad(
            {"default": "deny", "rules": [{"name": "r", "effect": "allow", "when": {"tool": ["x"]}}]}
        )
        self.assertIn("tool", str(err))

    def test_an_unknown_action_type_is_refused(self) -> None:
        err = self._bad(
            {
                "default": "deny",
                "rules": [
                    {"name": "r", "effect": "allow", "when": {"action_types": ["teleport"]}}
                ],
            }
        )
        self.assertIn("teleport", str(err))

    def test_an_unknown_min_risk_is_refused(self) -> None:
        err = self._bad(
            {
                "default": "deny",
                "rules": [{"name": "r", "effect": "allow", "when": {"min_risk": "critical"}}],
            }
        )
        self.assertIn("critical", str(err))

    def test_a_rule_without_a_name_is_refused(self) -> None:
        self.assertIn("name", str(self._bad({"default": "deny", "rules": [{"effect": "allow"}]})))

    def test_duplicate_rule_names_are_refused(self) -> None:
        err = self._bad(
            {
                "default": "deny",
                "rules": [
                    {"name": "same", "effect": "allow"},
                    {"name": "same", "effect": "deny", "reason": "x"},
                ],
            }
        )
        self.assertIn("duplicate", str(err).lower())

    def test_a_deny_without_a_reason_is_refused(self) -> None:
        """H-1 在加载期就兑现 —— 没有 reason 的拒绝读不出"为什么"。"""
        err = self._bad(
            {"default": "deny", "rules": [{"name": "r", "effect": "deny"}]}
        )
        self.assertIn("reason", str(err))

    def test_a_non_mapping_document_is_refused(self) -> None:
        with self.assertRaises(PolicyDocumentError):
            PolicyDocument.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
