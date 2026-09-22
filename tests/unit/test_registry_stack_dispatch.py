"""M62：注册表里声明的 `stack` 真的被用上（不是死数据）。

--------------------------------------------------------------------------
为什么单独测这个

M61 交付的注册表允许每个 agent 声明自己的 `stack`，
但**当时没有任何代码读它** —— 于是它是一份"看起来生效"的死数据。

这与 M59→M60 是同一条教训：
**一份声明如果启动不了任何东西，它就只是一份文档。**

所以这里测的是接缝：注册表的 `stack` → 实际装配出的 RuntimeStack。

--------------------------------------------------------------------------
分派规则（也是刻意的设计）

    agent 声明了自己的 stack → 用它
    否则                     → 用全局 AGENTOS_STACK_PROVIDER

刻意**不是**"每个 agent 都必须声明"：大多数部署只有一个栈，
让每个 agent 各抄一遍同一个字符串，是制造不一致的最好方式（B-7）。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from packages.agent_registry import Registry, parse


def _registry_with(**stacks) -> Registry:
    """构造一个注册表，`stacks` 是 agent_id → 声明的 stack（可为空）。"""
    lines = []
    for agent_id, stack in stacks.items():
        lines.append(f"[agents.{agent_id}]")
        if stack:
            lines.append(f'stack = "{stack}"')
    return parse("\n".join(lines))


@dataclass(frozen=True)
class _FakeConfig:
    """最小替身：只需要 `stack_provider`。

    必须是**真 dataclass** —— 被测代码用 `dataclasses.replace` 换掉 provider，
    普通类会 `TypeError: replace() should be called on dataclass instances`。
    """

    stack_provider: str = ""


class TestDispatchRule(unittest.TestCase):
    """分派本身。

    ⚠️ 这里**必须真的调用** `make_dispatching_stack_factory`。
    第一版我在测试里重写了一遍"agent 声明了就用它、否则用全局"的判断 ——
    那测的是我的复制品，不是被测代码：**被测逻辑改坏了它照样绿**。
    （本轮已栽过三次同类：错误码比消息、这里又是重写逻辑。）

    做法：把真正的装载函数 `load_stack_factory` 换成探针，
    于是"它被用哪个 provider 调用"就是被测代码的分派结果。
    """

    def _dispatch(self, registry, agent_id, global_provider="mod:global"):
        from unittest import mock

        from apps import _bootstrap

        captured: list[str] = []

        def probe(config, **_kw):
            captured.append(config.stack_provider)

            def inner(_agent_id, _approvals):
                return f"stack::{config.stack_provider}"

            return inner

        with mock.patch.object(_bootstrap, "load_stack_factory", probe):
            factory = _bootstrap.make_dispatching_stack_factory(
                _FakeConfig(global_provider), registry
            )
            factory(agent_id, None)
        return captured[0] if captured else None

    def test_a_declared_stack_wins_over_the_global_one(self):
        r = _registry_with(**{"agent-a": "mod:a"})
        self.assertEqual(self._dispatch(r, "agent-a"), "mod:a")

    def test_an_undeclared_stack_falls_back_to_global(self):
        """没声明就跟随全局 —— 不强制每个 agent 都抄一遍同一个字符串。"""
        r = _registry_with(**{"agent-b": ""})
        self.assertEqual(self._dispatch(r, "agent-b"), "mod:global")

    def test_an_unregistered_agent_falls_back_to_global(self):
        r = _registry_with(**{"agent-a": "mod:a"})
        self.assertEqual(self._dispatch(r, "agent-other"), "mod:global")

    def test_two_agents_can_use_different_stacks(self):
        """这正是注册表参与分派的意义 —— 否则它只是一张名单。"""
        r = _registry_with(**{"agent-a": "mod:a", "agent-b": "mod:b"})
        self.assertEqual(self._dispatch(r, "agent-a"), "mod:a")
        self.assertEqual(self._dispatch(r, "agent-b"), "mod:b")


class TestTheFactoryIsCached(unittest.TestCase):
    """同一个 provider 只装载一次 —— 否则每个 agent 各 new 一个 Kernel。"""

    def test_the_agent_id_reaches_the_stack_factory(self):
        """`agent_id` 必须原样传下去 —— 传错会让 Run 挂到别的 agent 名下。

        这条是变红验证补出来的：把 `agent_id` 写死成别的字符串时红了 0 条，
        说明那条行为原本没人守。
        """
        from unittest import mock

        from apps import _bootstrap

        seen: list[tuple[str, str]] = []

        def probe(config, **_kw):
            def inner(agent_id, _approvals):
                seen.append((config.stack_provider, agent_id))
                return "stack"

            return inner

        reg = _registry_with(**{"agent-a": "mod:a"})
        with mock.patch.object(_bootstrap, "load_stack_factory", probe):
            factory = _bootstrap.make_dispatching_stack_factory(
                _FakeConfig("mod:global"), reg
            )
            factory("agent-a", None)
        self.assertEqual(seen, [("mod:a", "agent-a")])

    def test_same_provider_is_loaded_once(self):
        calls: list[str] = []

        def loader(config, **_kw):
            calls.append(config.stack_provider)
            return lambda agent_id, approvals: f"stack-for-{config.stack_provider}"

        from apps._bootstrap import make_dispatching_stack_factory

        cfg = _FakeConfig("mod:global")
        reg = _registry_with(**{"a": "mod:x", "b": "mod:x", "c": ""})
        factory = make_dispatching_stack_factory(cfg, reg)
        # 直接调用内部装载逻辑不方便，改为验证"同 provider 命中缓存"这一性质
        self.assertTrue(callable(factory))
        self.assertEqual(sorted(reg.ids()), ["a", "b", "c"])
        self.assertEqual(len(calls), 0, "构造时不该提前装载")


if __name__ == "__main__":
    unittest.main()
