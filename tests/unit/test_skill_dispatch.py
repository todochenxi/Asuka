"""M3：Skill Runtime 接到运行时 —— 调技能之前先问注册表。

    · 注册过的技能 → 照常派生子 Run（走 M25 的 `_suspend_for_child`）
    · 没注册的技能 → **判死 + 点名，且不派子 Run**（不产生副作用）
    · 没配注册表   → 不校验（维持"技能即自由命名"的既有行为）

⚠️ "生成正确" ≠ "真的用上"：这里用一条**假 spawner** 记录它有没有被调用，
才能区分"拒绝了"和"其实照样派了、只是没验"。
"""
from __future__ import annotations

import unittest
import types

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.loop import AgentLoop
from packages.agent_runtime.skill_runtime import SkillRegistry, SkillSpec


class _RecordingSpawner:
    """只记录"有没有被要求派生"，不真的起子 Run。"""

    def __init__(self) -> None:
        self.spawned: list[str] = []

    def spawn(self, request):  # noqa: ANN001, ANN201
        self.spawned.append(str(getattr(request, "target", "")))
        raise RuntimeError("stop here: spawner is a stub")


def _skill_action(run_id: str, name: str) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.SKILL_CALL,
        payload={"skill": name},
        rationale="call a skill",
    )


class _Base(unittest.TestCase):
    def _loop(self, *, skills) -> tuple[AgentLoop, _RecordingSpawner]:
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
            ScriptedPlanner,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        spawner = _RecordingSpawner()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(nodes=1),
            decision_engine=ScriptedDecisionEngine([]),
            spawner=spawner,
            skills=skills,
        )
        return loop, spawner


class UnknownSkillIsRefusedTest(_Base):
    def test_an_unregistered_skill_is_refused_before_spawning(self) -> None:
        registry = SkillRegistry()
        registry.register(SkillSpec(name="summarize", body={"prompt": "p"}))
        loop, spawner = self._loop(skills=registry)

        state = loop.start("do it")
        loop.decision_engine = types.SimpleNamespace(
            decide=lambda s: types.SimpleNamespace(
                run_id=s.run_id,
                selected_action=_skill_action(s.run_id, "ghost"),
                rationale="x",
            )
        )
        outcome = loop.step()

        self.assertEqual(outcome.value, "failed")
        self.assertEqual(loop.agent_run.status.value, "failed")
        # ★ 关键：**根本没派生** —— 拒绝了，不是"派了但没验"
        self.assertEqual(spawner.spawned, [])
        reason = _terminal_reason(loop)
        self.assertIn("SKILL_NOT_FOUND", reason)
        self.assertIn("ghost", reason)
        self.assertIn("summarize", reason)

    def test_a_registered_skill_is_allowed_to_spawn(self) -> None:
        """控制组：注册过的技能会走到派生那一句（假 spawner 会拦在那）。"""
        registry = SkillRegistry()
        registry.register(SkillSpec(name="summarize", body={"prompt": "p"}))
        loop, spawner = self._loop(skills=registry)

        loop.start("do it")
        loop.decision_engine = types.SimpleNamespace(
            decide=lambda s: types.SimpleNamespace(
                run_id=s.run_id,
                selected_action=_skill_action(s.run_id, "summarize"),
                rationale="x",
            )
        )
        with self.assertRaises(RuntimeError):
            loop.step()

        self.assertEqual(spawner.spawned, ["summarize"])

    def test_without_a_registry_nothing_is_checked(self) -> None:
        """没配注册表 ⇒ 不校验（老栈不受影响）。"""
        loop, spawner = self._loop(skills=None)
        loop.start("do it")
        loop.decision_engine = types.SimpleNamespace(
            decide=lambda s: types.SimpleNamespace(
                run_id=s.run_id,
                selected_action=_skill_action(s.run_id, "anything"),
                rationale="x",
            )
        )
        with self.assertRaises(RuntimeError):
            loop.step()
        self.assertEqual(spawner.spawned, ["anything"])


def _terminal_reason(loop: AgentLoop) -> str:
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            return str(entry.payload.get("reason", ""))
    return ""


if __name__ == "__main__":
    unittest.main()
