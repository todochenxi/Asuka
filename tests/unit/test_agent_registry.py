"""M61 / 空洞 231：agent 注册表。

两条最要紧的：

1. **配了注册表就真的校验** —— 空洞 231 说的"不校验"到此为止。
2. **没配就维持现状** —— 否则 7 种即席 agent id 的现有用例会全红，
   而它们红的理由不是有 bug，只是没写注册表。
   把"没登记"和"写错了"混成一种错，是最容易制造假警报的方式。
"""
from __future__ import annotations

import unittest

from packages.agent_registry import (
    Agent,
    Registry,
    RegistryError,
    parse,
)


def _text() -> str:
    return """
[agents.agent-it]
description = "集成测试栈"
stack = "examples.demo_stack:build_approval_demo_stack_factory"

[agents.agent-math]
description = "只做算术"
"""


class TestParsing(unittest.TestCase):
    def test_agents_are_loaded(self):
        r = parse(_text())
        self.assertEqual(r.ids(), ("agent-it", "agent-math"))
        self.assertEqual(len(r), 2)

    def test_optional_fields_default_to_empty(self):
        """"`stack` 不写就跟随全局配置 —— 大多数部署只有一个栈。"""
        a = parse(_text()).get("agent-math")
        assert a is not None
        self.assertEqual(a.stack, "")
        self.assertEqual(a.description, "只做算术")

    def test_knows_distinguishes_declared_from_undeclared(self):
        """这是空洞 231 的答案："这个 id 存不存在"第一次有了判定。"""
        r = parse(_text())
        self.assertTrue(r.knows("agent-it"))
        self.assertFalse(r.knows("agent-nope"))


class TestRefusal(unittest.TestCase):
    """与 manifest 同理：一律点名，不静默。"""

    def test_missing_agents_section(self):
        with self.assertRaises(RegistryError) as ctx:
            parse("[other]\nx = 1\n")
        self.assertIn("REGISTRY_NO_AGENTS", str(ctx.exception))

    def test_unknown_field_in_an_agent(self):
        """多半是拼错 —— 静默忽略的话那条配置永远不生效。"""
        with self.assertRaises(RegistryError) as ctx:
            parse('[agents.a]\ndescription = "x"\nstak = "y"\n')
        self.assertIn("REGISTRY_UNKNOWN_KEY", str(ctx.exception))

    def test_non_string_field(self):
        with self.assertRaises(RegistryError) as ctx:
            parse("[agents.a]\ndescription = 1\n")
        self.assertIn("REGISTRY_BAD_TYPE", str(ctx.exception))

    def test_empty_registry(self):
        with self.assertRaises(RegistryError) as ctx:
            parse("[agents]\n")
        self.assertIn("REGISTRY_EMPTY", str(ctx.exception))

    def test_broken_toml(self):
        with self.assertRaises(RegistryError) as ctx:
            parse("[agents\n")
        self.assertIn("REGISTRY_NOT_TOML", str(ctx.exception))


class TestServiceIntegration(unittest.TestCase):
    """注册表接到 `start_run` 上的行为。"""

    def _plane(self, registry):
        from packages.agent_api import InProcessControlPlane, StartRunRequest

        def factory(agent_id, approvals):          # noqa: ARG001 - 不会被真的调用
            raise AssertionError("未登记的 agent 不该走到装配")

        return (
            InProcessControlPlane(factory=factory, registry=registry),
            StartRunRequest,
        )

    def test_an_unregistered_agent_is_refused(self):
        """空洞 231 的主断言：配了注册表，没登记的 id 必须 404。"""
        from packages.agent_api.errors import ApiError

        plane, Req = self._plane(parse(_text()))
        with self.assertRaises(ApiError) as ctx:
            plane.start_run(Req(agent_id="agent-nope", user_request="x"))
        err = ctx.exception
        # 错误码在 `.code` 上（不在消息里），HTTP 语义在 `.http_status` 上
        self.assertEqual(err.code, "AGENT_NOT_FOUND")
        self.assertEqual(err.http_status, 404)

    def _assert_not_refused_for_unregistered(self, plane, Req, agent_id, why):
        """断言"没有因为未登记而被拒"。

        两个坑，都踩过：
        1. 必须比 `.code`，不能比 `str(e)` —— 错误码不在消息里（`.code` 上）。
        2. 必须接**所有**异常再取 `.code`：工厂抛的是普通 `AssertionError`，
           只 `except ApiError` 会让它直接冒泡成 ERROR，而不是被这条断言判定。
        """
        try:
            plane.start_run(Req(agent_id=agent_id, user_request="x"))
        except Exception as e:                        # noqa: BLE001
            self.assertNotEqual(
                getattr(e, "code", None), "AGENT_NOT_FOUND", why
            )

    def test_no_registry_means_passthrough(self):
        """**这条是向后兼容的命脉**：没配注册表时不能因为"未登记"而拒绝。

        各处的即席 agent id（`agent-it` / `agent-api` / …）都不是登记过的名字。
        若这里也拒绝，那些用例会全红 —— 而它们并没有 bug。
        """
        plane, Req = self._plane(None)
        self._assert_not_refused_for_unregistered(
            plane, Req, "whatever",
            "没配注册表时不该因为'未登记'而拒绝 —— 那会打红一堆即席 agent id",
        )

    def test_a_registered_agent_passes_the_check(self):
        """登记过的 id 不会在**校验**这步被挡（后面装配失败是另一回事）。"""
        plane, Req = self._plane(parse(_text()))
        self._assert_not_refused_for_unregistered(
            plane, Req, "agent-it", "登记过的 id 不该被未登记校验挡下"
        )
