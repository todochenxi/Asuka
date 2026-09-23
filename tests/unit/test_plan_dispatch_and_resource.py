"""M99：AgentOS 收窄空洞 250 的最后两项。

    PlanNode 的 `agent` / `decision` 分派（此前 I-18 诚实拒绝）
    §32 的 "Resource" 检查（此前"连表达都表达不了"）

判据：**声明必须兑现**。每加一个 kind，就必须有一条真实的执行路径；
每加一个计划期检查，就必须有一次真的会响的拒绝。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_runtime.loop import (
    PLAN_NODE_ACTION_TYPES,
    PLAN_RESOURCE_UNAVAILABLE,
    SUPPORTED_PLAN_NODE_KINDS,
    plan_defects,
)


class AgentAndDecisionAreDispatchedTest(unittest.TestCase):
    def test_all_five_kinds_are_supported(self) -> None:
        self.assertEqual(SUPPORTED_PLAN_NODE_KINDS, frozenset(PlanNodeKind))

    def test_agent_maps_to_delegation(self) -> None:
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.AGENT],
            frozenset({ActionType.AGENT_DELEGATION}),
        )

    def test_decision_maps_to_replan_only(self) -> None:
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.DECISION],
            frozenset({ActionType.REPLAN}),
        )

    def test_every_supported_kind_has_a_non_empty_action_set(self) -> None:
        """一个声明支持、却没有任何 Action 的 kind = 换了个地方说谎。"""
        for kind in SUPPORTED_PLAN_NODE_KINDS:
            with self.subTest(kind=kind.value):
                self.assertTrue(PLAN_NODE_ACTION_TYPES.get(kind))


class ResourceLabelTest(unittest.TestCase):
    def _plan(self, resource_labels) -> Plan:
        return Plan(
            run_id="run_1",
            nodes=(PlanNode(node_id="n", name="n", resource_labels=tuple(resource_labels)),),
        )

    def test_declared_labels_are_representable(self) -> None:
        node = PlanNode(node_id="n", name="n", resource_labels=("gpu", "big-mem"))
        self.assertEqual(node.resource_labels, ("gpu", "big-mem"))

    def test_a_satisfiable_requirement_passes(self) -> None:
        defects = plan_defects(
            self._plan(("gpu",)),
            run_id="run_1",
            available_resources=frozenset({"cpu", "gpu"}),
        )
        self.assertEqual(defects, [])

    def test_an_unsatisfiable_requirement_is_refused(self) -> None:
        defects = plan_defects(
            self._plan(("gpu",)),
            run_id="run_1",
            available_resources=frozenset({"cpu"}),
        )
        self.assertEqual([d.code for d in defects], [PLAN_RESOURCE_UNAVAILABLE])
        self.assertIn("gpu", defects[0].detail)

    def test_none_available_resources_means_do_not_check(self) -> None:
        """`available_resources=None` = "worker 说不清" ⇒ 不校验（不是"没有资源"）。"""
        self.assertEqual(
            plan_defects(self._plan(("gpu",)), run_id="run_1", available_resources=None),
            [],
        )

    def test_a_node_with_no_requirement_is_not_checked(self) -> None:
        self.assertEqual(
            plan_defects(
                self._plan(()), run_id="run_1", available_resources=frozenset()
            ),
            [],
        )


class ResourceRoundTripsThroughSnapshotsTest(unittest.TestCase):
    """新字段必须**读得回来** —— 只序列化不反序列化的话，恢复路径对它是瞎的。"""

    def test_tool_and_resource_labels_survive_a_snapshot(self) -> None:
        from packages.agent_domain.business.snapshot import state_from_dict, state_to_dict
        from packages.agent_domain.intelligence.goal import Budget, Goal
        from packages.agent_domain.intelligence.state import State

        state = State(
            run_id="run_x",
            goal=Goal(
                run_id="run_x",
                objective="o",
                success_criteria=("c",),
                budget=Budget(max_steps=3),
            ),
            current_plan=Plan(
                run_id="run_x",
                nodes=(
                    PlanNode(
                        node_id="t",
                        name="t",
                        kind=PlanNodeKind.TOOL,
                        tool="calculator",
                        resource_labels=("gpu",),
                    ),
                ),
            ),
        )
        restored = state_from_dict(state_to_dict(state))
        node = restored.current_plan.nodes[0]
        self.assertEqual(node.tool, "calculator")
        self.assertEqual(node.resource_labels, ("gpu",))


if __name__ == "__main__":
    unittest.main()
