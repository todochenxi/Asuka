"""M98 / 空洞 250：§32 的 Plan Validator —— 把**计划期真的能判死**的都补上。

§32 列了七项。M89 只建了"计划期一次性就能判死"的两条（I-18 kind / I-19 归属）。
这一轮补齐另外两条**结构上就属于计划期**的：

    Dependency  → 计划必须是一份**可执行的工作单**（至少有一个入口节点）
    Tool Exists → 计划声明的工具，这个运行时**认识**

⚠️ 剩下三项（Permission / Risk / Budget）**刻意不在这里做**：它们是**反复发生**的
约束，执行期逐步拦才是对的时机（Harness `before_action` / Loop 预算计数）。
把它们搬进计划期，等于用一次性检查冒充持续约束。`loop.py` 的模块级归属表
把这件事写清楚 —— 这比造一个看起来像门的空壳诚实。

Resource 仍**无机制**（全仓没有 Resource 概念）—— 登记不治，不假装。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_runtime.loop import PLAN_TOOL_NOT_FOUND, plan_defects


def _plan(nodes) -> Plan:
    return Plan(run_id="run_1", nodes=tuple(nodes))


class DependencyIsAlreadyCoveredTest(unittest.TestCase):
    """§32 的 Dependency **不需要在这里再做一遍** —— 领域层已经拒绝了它。

    一条永远不会响的判据比没有更糟：它看起来像一道门，门后却什么都没有。
    这里把这个**结论**钉住（而不是留一条空判据）。
    """

    def test_a_plan_with_an_entry_point_has_no_defect(self) -> None:
        plan = _plan(
            [
                PlanNode(node_id="a", name="a"),
                PlanNode(node_id="b", name="b", depends_on=("a",)),
            ]
        )
        self.assertEqual(plan_defects(plan, run_id="run_1"), [])

    def test_a_plan_without_an_entry_point_is_refused_by_the_domain_layer(self) -> None:
        """没有入口 = 带环 ⇒ `Plan.__post_init__` 在**构造期**就拒绝（不是计划期）。"""
        with self.assertRaises(InvariantViolation) as ctx:
            Plan(
                run_id="run_1",
                nodes=(
                    PlanNode(node_id="a", name="a", depends_on=("b",)),
                    PlanNode(node_id="b", name="b", depends_on=("a",)),
                ),
            )
        self.assertIn("cycle", str(ctx.exception))

    def test_an_empty_plan_is_not_a_defect(self) -> None:
        """空计划合法（动态决策图允许没有静态节点）。"""
        self.assertEqual(plan_defects(_plan([]), run_id="run_1"), [])


class ToolExistsTest(unittest.TestCase):
    def test_a_tool_node_naming_a_known_tool_passes(self) -> None:
        plan = _plan(
            [PlanNode(node_id="t", name="t", kind=PlanNodeKind.TOOL, tool="calculator")]
        )
        self.assertEqual(
            plan_defects(plan, run_id="run_1", known_tools=frozenset({"calculator"})), []
        )

    def test_a_tool_node_naming_an_unknown_tool_is_refused(self) -> None:
        plan = _plan(
            [PlanNode(node_id="t", name="t", kind=PlanNodeKind.TOOL, tool="ghost")]
        )
        defects = plan_defects(plan, run_id="run_1", known_tools=frozenset({"calculator"}))
        self.assertEqual([d.code for d in defects], [PLAN_TOOL_NOT_FOUND])
        self.assertIn("ghost", defects[0].detail)

    def test_none_known_tools_means_do_not_check(self) -> None:
        """`known_tools=None` = "这条进程没有工具表" ⇒ **不校验**（不是"没有工具"）。"""
        plan = _plan(
            [PlanNode(node_id="t", name="t", kind=PlanNodeKind.TOOL, tool="ghost")]
        )
        self.assertEqual(plan_defects(plan, run_id="run_1", known_tools=None), [])

    def test_a_generic_task_node_is_not_checked_for_tools(self) -> None:
        """`kind='task'` 的通用节点不事先声明工具 —— 不校验它。"""
        plan = _plan([PlanNode(node_id="n", name="n")])
        self.assertEqual(
            plan_defects(plan, run_id="run_1", known_tools=frozenset()), []
        )


class ToolNodeMustNameItsToolTest(unittest.TestCase):
    def test_a_tool_node_without_a_tool_name_is_refused_at_construction(self) -> None:
        """声明必须完整：没有宾语的 tool 节点是一句无法校验的声明。"""
        with self.assertRaises(InvariantViolation) as ctx:
            PlanNode(node_id="t", name="t", kind=PlanNodeKind.TOOL)
        self.assertIn("tool", str(ctx.exception))

    def test_the_control_a_task_node_needs_no_tool(self) -> None:
        PlanNode(node_id="n", name="n")            # 不抛 = 合法
        PlanNode(node_id="h", name="h", kind=PlanNodeKind.HUMAN)


if __name__ == "__main__":
    unittest.main()
