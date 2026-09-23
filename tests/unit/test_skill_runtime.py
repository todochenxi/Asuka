"""M3：Skill Runtime —— 技能的**定义与注册表**（此前全仓不存在）。

基线 §25 说 Skill = 一段可复用的做事方法，分三种形态（Prompt / Workflow / Agentic），
全部复用同一套 Kernel。此前 `SKILL_CALL` 的**派发路径**（M25 的 `_suspend_for_child`
→ 子 Run）在，但"技能是什么"**连表达都没有** —— 没有 `SkillSpec`、没有注册表。
这一层补的就是那一块：技能的声明与登记。

⚠️ 判据：**声明必须完整**。`kind=workflow` 的技能没有 steps、
`kind=agentic` 的没有 agent_id、`kind=prompt` 的没有 prompt ——
都是一个"没有内容的声明"，构造期就拒绝（不做"兜底成 prompt"）。
"""
from __future__ import annotations

import unittest

from packages.agent_runtime.skill_runtime import (
    SkillKind,
    SkillNotFoundError,
    SkillRegistry,
    SkillSpec,
)


class SkillSpecTest(unittest.TestCase):
    def test_the_three_forms_from_the_baseline_are_representable(self) -> None:
        prompt = SkillSpec(name="summarize", kind="prompt", body={"prompt": "sum it up"})
        workflow = SkillSpec(
            name="onboard", kind="workflow", body={"steps": ["a", "b"]}
        )
        agentic = SkillSpec(
            name="research", kind="agentic", body={"agent_id": "researcher"}
        )
        self.assertIs(prompt.kind, SkillKind.PROMPT)
        self.assertIs(workflow.kind, SkillKind.WORKFLOW)
        self.assertIs(agentic.kind, SkillKind.AGENTIC)

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SkillSpec(name="x", kind="magic", body={"prompt": "p"})
        self.assertIn("magic", str(ctx.exception))

    def test_a_declaration_must_have_content(self) -> None:
        """空 body = 没有内容的声明 —— 三种形态各自点名缺了什么。"""
        with self.assertRaises(ValueError):
            SkillSpec(name="p", kind="prompt", body={})
        with self.assertRaises(ValueError):
            SkillSpec(name="w", kind="workflow", body={"steps": []})
        with self.assertRaises(ValueError):
            SkillSpec(name="a", kind="agentic", body={})

    def test_name_and_version_are_required(self) -> None:
        with self.assertRaises(ValueError):
            SkillSpec(name="", body={"prompt": "p"})
        with self.assertRaises(ValueError):
            SkillSpec(name="x", version="", body={"prompt": "p"})


class SkillRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry()
        self.registry.register(
            SkillSpec(name="summarize", version="1.0.0", body={"prompt": "a"})
        )

    def test_register_and_resolve(self) -> None:
        spec = self.registry.resolve("summarize")
        self.assertEqual(spec.name, "summarize")
        self.assertEqual(spec.qualified_name, "summarize@1.0.0")

    def test_an_unknown_skill_raises(self) -> None:
        with self.assertRaises(SkillNotFoundError):
            self.registry.resolve("ghost")

    def test_a_duplicate_registration_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                SkillSpec(name="summarize", version="1.0.0", body={"prompt": "b"})
            )

    def test_the_default_version_must_be_registered_explicitly(self) -> None:
        """默认版本不能随注册顺序漂移（T-5 的同一条取舍）。"""
        self.registry.register(
            SkillSpec(name="summarize", version="2.0.0", body={"prompt": "c"})
        )
        self.assertEqual(self.registry.resolve("summarize").version, "1.0.0")
        self.registry.set_default("summarize", "2.0.0")
        self.assertEqual(self.registry.resolve("summarize").version, "2.0.0")

    def test_has_and_specs_views(self) -> None:
        self.assertTrue(self.registry.has("summarize"))
        self.assertFalse(self.registry.has("ghost"))
        self.assertEqual(set(self.registry.specs()), {"summarize"})


if __name__ == "__main__":
    unittest.main()
