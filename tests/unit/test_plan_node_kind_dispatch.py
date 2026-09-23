"""M12 / I-21：已支持的 PlanNode.kind 必须兑现为对应 Action。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_runtime.loop import (
    PLAN_NODE_ACTION_TYPES,
    PLAN_NODE_ACTION_MISMATCH,
    SUPPORTED_PLAN_NODE_KINDS,
    AgentLoop,
    StepOutcome,
)
from packages.agent_runtime.reducer import RUN_FINISHED


class _KindPlanner:
    def __init__(self, kind: str, *, tool: str = "") -> None:
        self.kind = kind
        self.tool = tool

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(
                    node_id="n0",
                    name=f"{self.kind}-node",
                    kind=self.kind,
                    # M98：`kind='tool'` 必须点名调哪个工具（否则构造期拒绝）。
                    tool=self.tool,
                ),
            ),
        )


class _Base(unittest.TestCase):
    def _new(self, kind: str, action_type: ActionType, *, tool: str = "", **kwargs):
        # 函数内导入，避免 unittest 把被复用的 LoopTestBase 重新收集一遍。
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=_KindPlanner(kind, tool=tool),
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("2+3=?")
        action = Action(
            run_id=state.run_id,
            action_type=action_type,
            payload=kwargs.pop("payload", {}),
            **kwargs,
        )
        loop.decision_engine = ScriptedDecisionEngine([action])
        return loop, state

    @staticmethod
    def _finished_reason(loop: AgentLoop) -> str:
        for entry in reversed(loop.trace.entries):
            if entry.kind == RUN_FINISHED:
                return str(entry.payload.get("reason", ""))
        return ""


class SupportedKindsAreExplicitTest(unittest.TestCase):
    def test_m12_expands_only_to_real_dispatch_paths(self) -> None:
        """M99：五个 kind 全部有真实分派路径 —— 声明与能力对齐。"""
        self.assertEqual(SUPPORTED_PLAN_NODE_KINDS, frozenset(PlanNodeKind))
        self.assertEqual(set(PLAN_NODE_ACTION_TYPES), set(PlanNodeKind))
        # `agent` → 委派（M25 的派生子 Run）；`decision` → 纯决策（REPLAN）。
        self.assertIn(PlanNodeKind.AGENT, SUPPORTED_PLAN_NODE_KINDS)
        self.assertIn(PlanNodeKind.DECISION, SUPPORTED_PLAN_NODE_KINDS)

    def test_task_is_generic_but_tool_and_human_are_narrow(self) -> None:
        self.assertIn(ActionType.LLM_CALL, PLAN_NODE_ACTION_TYPES[PlanNodeKind.TASK])
        self.assertIn(ActionType.TOOL_CALL, PLAN_NODE_ACTION_TYPES[PlanNodeKind.TASK])
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.TOOL],
            frozenset({ActionType.TOOL_CALL}),
        )
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.HUMAN],
            frozenset({ActionType.HUMAN_APPROVAL, ActionType.ASK_USER}),
        )


class ToolKindDispatchTest(_Base):
    def test_tool_kind_with_tool_action_reaches_kernel(self) -> None:
        loop, state = self._new(
            "tool",
            ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "2+3"}},
            tool="calculator",
        )

        self.assertEqual(loop.step(), StepOutcome.EXECUTED)
        self.assertEqual(loop.steps, 1)
        self.assertEqual([s.plan_node_id for s in loop.steps_of_run], ["n0"])
        self.assertEqual(len(state.completed_tasks), 1)

    def test_tool_kind_cannot_be_satisfied_by_an_llm_action(self) -> None:
        loop, _ = self._new(
            "tool", ActionType.LLM_CALL, payload={"prompt": "wrong"}, tool="calculator"
        )

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)
        self.assertEqual(loop.steps, 0)
        self.assertEqual(loop.steps_of_run, [])
        reason = self._finished_reason(loop)
        self.assertIn(PLAN_NODE_ACTION_MISMATCH, reason)
        self.assertIn("n0", reason)
        self.assertIn("tool_call", reason)
        self.assertIn("llm_call", reason)


class HumanKindDispatchTest(_Base):
    def test_human_kind_enters_the_existing_approval_path(self) -> None:
        loop, _ = self._new(
            "human",
            ActionType.HUMAN_APPROVAL,
            timeout=timedelta(seconds=30),
            rationale="please approve this step",
        )

        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertIsNotNone(loop.pending_approval)
        self.assertEqual(loop.pending_action.action_type, ActionType.HUMAN_APPROVAL)
        self.assertEqual(loop.steps, 0)

    def test_human_kind_cannot_be_satisfied_by_a_tool_action(self) -> None:
        loop, _ = self._new(
            "human",
            ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "2+3"}},
        )

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        self.assertEqual(loop.steps, 0)
        self.assertEqual(loop.steps_of_run, [])
        reason = self._finished_reason(loop)
        self.assertIn(PLAN_NODE_ACTION_MISMATCH, reason)
        self.assertIn("human", reason)
        self.assertIn("tool_call", reason)
        self.assertIn("human_approval", reason)


if __name__ == "__main__":
    unittest.main()
