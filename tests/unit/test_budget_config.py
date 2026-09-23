"""M96：Cost 预算与 Memory 持久 —— 把最后两条"接了但没通电"的线通上。

    AGENTOS_MAX_COST / MAX_TOKENS / MAX_STEPS  → Budget → Harness（超了 → DENY）
    Memory 事实源                               → PostgresMemoryStore（跨重启）

⚠️ 没配预算时行为不变（`Budget()` = 不限）—— 同 M95 的接线底线。
"""
from __future__ import annotations

import unittest

from apps._bootstrap import ConfigurationError, RuntimeConfig
from packages.agent_harness.cost import Budget


def _cfg(**env: str) -> RuntimeConfig:
    base = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
    base.update(env)
    return RuntimeConfig.from_env(base)


class BudgetConfigTest(unittest.TestCase):
    def test_no_limits_configured_is_unlimited(self) -> None:
        """控制组：没配任何一项 ⇒ 默认 `Budget()`（不限），行为与 M96 之前一致。"""
        self.assertEqual(_cfg().budget(), Budget())

    def test_each_limit_is_read_from_the_environment(self) -> None:
        budget = _cfg(
            AGENTOS_MAX_COST="0.5", AGENTOS_MAX_TOKENS="1000", AGENTOS_MAX_STEPS="3"
        ).budget()
        self.assertEqual(budget.max_cost, 0.5)
        self.assertEqual(budget.max_tokens, 1000)
        self.assertEqual(budget.max_steps, 3)

    def test_a_partial_budget_keeps_the_others_unlimited(self) -> None:
        budget = _cfg(AGENTOS_MAX_STEPS="2").budget()
        self.assertEqual(budget.max_steps, 2)
        self.assertEqual(budget.max_cost, float("inf"))
        self.assertIsNone(budget.max_tokens)

    def test_a_malformed_limit_is_refused_not_silently_ignored(self) -> None:
        """配置错要在**启动前**死 —— 静默忽略会让"我设了上限"变成"其实没有"。"""
        with self.assertRaises(ConfigurationError):
            _cfg(AGENTOS_MAX_TOKENS="lots")
        with self.assertRaises(ConfigurationError):
            _cfg(AGENTOS_MAX_COST="cheap")


if __name__ == "__main__":
    unittest.main()
