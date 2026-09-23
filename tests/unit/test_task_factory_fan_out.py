"""M97：Task Factory 的**一对多**（基线 §2.3：`Step : Task = 1 : N`）。

一个 Action 声明 `payload["tasks"]`（`{tool, args}` 列表）⇒ 产生多个 Task，
但它们属于**同一个 Step**（Step 是逻辑节点，Task 是可调度单元）。

扇出是"同一 Action 多个分支"，**不是**一个新的 `ActionType` ——
新造枚举值会多一个"声明了却没人管"的洞（M88/M90 的老毛病）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.task_factory import TaskFactory


def _tool_action(**payload) -> Action:
    return Action(
        run_id="run_1", action_type=ActionType.TOOL_CALL, payload=dict(payload)
    )


class FanOutFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = TaskFactory()

    def test_a_plain_action_yields_exactly_one_task(self) -> None:
        tasks = self.factory.from_actions(
            _tool_action(tool="echo", args={"text": "hi"}), step_id="step_1"
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].payload["tool"], "echo")
        self.assertEqual(tasks[0].step_id, "step_1")

    def test_a_fan_out_action_yields_one_task_per_branch(self) -> None:
        action = _tool_action(
            tasks=[
                {"tool": "echo", "args": {"text": "a"}},
                {"tool": "add", "args": {"numbers": [1, 2]}},
                {"tool": "echo", "args": {"text": "b"}},
            ]
        )
        tasks = self.factory.from_actions(action, step_id="step_1")

        self.assertEqual(len(tasks), 3)
        self.assertEqual([t.payload["tool"] for t in tasks], ["echo", "add", "echo"])
        # 它们属于**同一个 Step** —— 这正是 1:N 的落点。
        self.assertEqual({t.step_id for t in tasks}, {"step_1"})
        # run_id / task_type / risk 全部继承父 Action。
        self.assertEqual({t.run_id for t in tasks}, {"run_1"})

    def test_branches_do_not_carry_the_fan_out_list(self) -> None:
        """`tasks` 描述父子关系，不是"这一片要执行什么" —— 不许写进 Task payload。"""
        action = _tool_action(
            tasks=[{"tool": "echo", "args": {"text": "a"}}, {"tool": "echo", "args": {"text": "b"}}]
        )
        for task in self.factory.from_actions(action, step_id="s"):
            self.assertNotIn("tasks", task.payload)

    def test_from_action_refuses_a_fan_out_action(self) -> None:
        """扇出 Action 走 `from_action()` 会把多出来的分支**静默丢掉** ⇒ 直接拒绝。"""
        action = _tool_action(
            tasks=[{"tool": "echo", "args": {}}, {"tool": "echo", "args": {}}]
        )
        with self.assertRaises(InvariantViolation) as ctx:
            self.factory.from_action(action, step_id="s")
        self.assertIn("fan-out", str(ctx.exception))

    def test_a_malformed_branch_is_named(self) -> None:
        action = _tool_action(tasks=[{"tool": "echo"}, "not-a-mapping"])
        with self.assertRaises(InvariantViolation) as ctx:
            self.factory.from_actions(action, step_id="s")
        self.assertIn("#1", str(ctx.exception))

    def test_the_single_task_path_is_unchanged(self) -> None:
        """控制组：非扇出 Action 的 `from_action` 与 `from_actions` 结果一致。"""
        action = _tool_action(tool="echo", args={"text": "x"})
        single = self.factory.from_action(action, step_id="s")
        only = self.factory.from_actions(action, step_id="s")
        self.assertEqual(len(only), 1)
        self.assertEqual(single.payload, only[0].payload)
        self.assertEqual(single.task_type, only[0].task_type)


if __name__ == "__main__":
    unittest.main()
