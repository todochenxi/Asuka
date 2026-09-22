"""Intelligence 层不变量：I-1 ~ I-9、X-7、X-10。"""
from __future__ import annotations

import dataclasses
import unittest
from datetime import timedelta

from packages.agent_domain.errors import ConcurrentStateError, InvariantViolation
from packages.agent_domain.intelligence import (
    Action,
    ActionResolver,
    ActionType,
    ArtifactRef,
    Budget,
    Decision,
    Goal,
    Observation,
    ObservationSource,
    PassthroughReducer,
    Plan,
    PlanNode,
    RiskLevel,
    State,
    interpret_goal,
)
from packages.agent_domain.ids import new_run_id

from .helpers import new_run


class _EchoInterpreter:
    def interpret(self, user_request: str, context) -> Goal:
        return Goal(
            run_id=context["run_id"],
            objective=user_request,
            success_criteria=("answer is grounded in retrieved data",),
        )


class TestGoal(unittest.TestCase):
    def test_i1_goal_must_come_from_interpreter(self):
        run_id = new_run()
        goal = interpret_goal("分析这家公司的现金流", _EchoInterpreter(), run_id=run_id)
        self.assertEqual(goal.run_id, run_id)

        with self.assertRaises(InvariantViolation):
            interpret_goal("", _EchoInterpreter(), run_id=run_id)
        with self.assertRaises(InvariantViolation):
            interpret_goal("x", _EchoInterpreter(), run_id="")

    def test_i1_interpreter_cannot_return_foreign_run(self):
        class _Bad:
            def interpret(self, user_request, context):
                return Goal(
                    run_id=new_run_id(),
                    objective=user_request,
                    success_criteria=("ok",),
                )

        with self.assertRaises(InvariantViolation):
            interpret_goal("x", _Bad(), run_id=new_run())

    def test_i2_success_criteria_required(self):
        with self.assertRaises(InvariantViolation):
            Goal(run_id=new_run(), objective="do something", success_criteria=())
        Goal(run_id=new_run(), objective="do something", success_criteria=("ok",))

    def test_budget_and_constraints(self):
        goal = Goal(
            run_id=new_run(),
            objective="o",
            constraints=("no external network",),
            success_criteria=("done",),
            budget=Budget(max_steps=10, max_cost_usd=1.5),
        )
        self.assertEqual(goal.budget.max_steps, 10)


class TestState(unittest.TestCase):
    def _state(self, run_id: str) -> State:
        return State(
            run_id=run_id,
            goal=Goal(run_id=run_id, objective="o", success_criteria=("done",)),
        )

    def _obs(self, run_id: str, execution_id: str = "exec_1", attempt_no: int = 1) -> Observation:
        return Observation.from_execution_result(
            run_id=run_id,
            execution_id=execution_id,
            attempt_no=attempt_no,
            kind="tool_result",
            summary="balance=100",
        )

    def test_i3_state_only_changed_via_apply(self):
        run_id = new_run()
        st = self._state(run_id)
        with self.assertRaises(InvariantViolation):
            st.variables = {"x": 1}
        with self.assertRaises(InvariantViolation):
            st.runtime_status = "COMPLETED"

    def test_x7_observation_must_go_through_reducer(self):
        run_id = new_run()
        st = self._state(run_id)
        before = st.version
        st.apply(self._obs(run_id), PassthroughReducer())
        self.assertEqual(st.version, before + 1)
        self.assertEqual(len(st.observations), 1)

    def test_x10_serialized_writes_and_version_check(self):
        run_id = new_run()
        st = self._state(run_id)
        st.apply(self._obs(run_id), PassthroughReducer())
        v = st.version
        # 落后版本的写入必须被拒绝（并行 Task 同时 reduce 的保护）
        with self.assertRaises(ConcurrentStateError):
            st.apply(self._obs(run_id), PassthroughReducer(), expected_version=v - 1)
        # 重放：用正确版本写入成功
        st.apply(self._obs(run_id), PassthroughReducer(), expected_version=v)
        self.assertEqual(st.version, v + 1)

    def test_state_rejects_foreign_run_observation(self):
        run_id = new_run()
        st = self._state(run_id)
        with self.assertRaises(InvariantViolation):
            st.apply(self._obs(new_run()), PassthroughReducer())

    def test_i5_observation_is_immutable(self):
        obs = self._obs(new_run())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            obs.kind = "other"


class TestObservation(unittest.TestCase):
    def test_i6_observation_requires_real_source(self):
        run_id = new_run()
        obs = Observation.from_execution_result(
            run_id=run_id,
            execution_id="exec_abc",
            attempt_no=1,
            kind="tool_result",
            summary="ok",
        )
        self.assertEqual(obs.source, ObservationSource.EXECUTION_RESULT)

        with self.assertRaises(InvariantViolation):
            Observation.from_execution_result(
                run_id=run_id, execution_id="", attempt_no=1, kind="k", summary="s"
            )
        with self.assertRaises(InvariantViolation):
            Observation.from_execution_result(
                run_id=run_id, execution_id="exec_abc", attempt_no=0, kind="k", summary="s"
            )

    def test_i7_large_payload_must_use_artifact(self):
        run_id = new_run()
        big = {"blob": "x" * (70 * 1024)}
        with self.assertRaises(InvariantViolation):
            Observation.from_execution_result(
                run_id=run_id,
                execution_id="exec_abc",
                attempt_no=1,
                kind="tool_result",
                summary="big",
                content=big,
            )
        ok = Observation.from_execution_result(
            run_id=run_id,
            execution_id="exec_abc",
            attempt_no=1,
            kind="tool_result",
            summary="big",
            content={"note": "stored in s3"},
            artifact_refs=(
                ArtifactRef(artifact_id="art_1", uri="s3://bucket/key", size_bytes=70 * 1024),
            ),
        )
        self.assertTrue(ok.is_large())


class TestActionAndDecision(unittest.TestCase):
    def test_i8_human_approval_requires_timeout(self):
        run_id = new_run()
        with self.assertRaises(InvariantViolation):
            Action(run_id=run_id, action_type=ActionType.HUMAN_APPROVAL)
        Action(
            run_id=run_id,
            action_type=ActionType.HUMAN_APPROVAL,
            timeout=timedelta(minutes=30),
            risk_level=RiskLevel.HIGH,
        )

    def test_i4_decision_cannot_execute(self):
        run_id = new_run()
        action = Action(run_id=run_id, action_type=ActionType.TOOL_CALL)
        decision = Decision(run_id=run_id, selected_action=action, confidence_signal=0.9)
        self.assertFalse(hasattr(decision, "execute"))
        self.assertFalse(hasattr(decision, "run"))
        resolved = ActionResolver().resolve(decision)
        self.assertIs(resolved, action)

    def test_i5_decision_is_immutable(self):
        run_id = new_run()
        d = Decision(
            run_id=run_id,
            selected_action=Action(run_id=run_id, action_type=ActionType.FINISH),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.confidence_signal = 1.0

    def test_i9_confidence_is_signal_not_probability(self):
        run_id = new_run()
        action = Action(run_id=run_id, action_type=ActionType.TOOL_CALL)
        with self.assertRaises(InvariantViolation):
            Decision(run_id=run_id, selected_action=action, confidence_signal=1.4)
        d = Decision(run_id=run_id, selected_action=action, confidence_signal=0.95)
        # 只提供描述，不提供自动放行能力
        self.assertEqual(d.describe_confidence(), "high-signal")
        for forbidden in ("should_auto_execute", "auto_execute", "is_confident"):
            self.assertFalse(hasattr(d, forbidden))


class TestPlan(unittest.TestCase):
    def test_plan_acyclic_and_dependencies(self):
        run_id = new_run()
        plan = Plan(
            run_id=run_id,
            nodes=(
                PlanNode(node_id="a", name="collect"),
                PlanNode(node_id="b", name="analyze", depends_on=("a",)),
            ),
        )
        self.assertEqual(len(plan.root_nodes()), 1)

        with self.assertRaises(InvariantViolation):
            Plan(
                run_id=run_id,
                nodes=(
                    PlanNode(node_id="a", name="a", depends_on=("b",)),
                    PlanNode(node_id="b", name="b", depends_on=("a",)),
                ),
            )

    def test_step_is_runtime_instance_of_plan_node(self):
        """Step（Business Domain）由 PlanNode 实例化而来，Kernel 只看到它派生的 Task（X-9）。"""
        plan = Plan(run_id=new_run(), nodes=(PlanNode(node_id="a", name="collect"),))
        node = plan.node("a")
        self.assertEqual(node.node_id, "a")


if __name__ == "__main__":
    unittest.main()
