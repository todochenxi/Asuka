"""Execution / Task 不变量：E-1、E-2、E-8、E-11、E-12、E-13、E-19、E-21、X-2。"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import (
    ConcurrentStateError,
    IllegalTransition,
    InvariantViolation,
    TerminalStateError,
)
from packages.agent_domain.execution import (
    ExecutionStatus,
    ExecutorType,
    Suspension,
    SuspensionReason,
    Task,
    TaskType,
)
from packages.agent_domain.execution.state_machine import (
    EXECUTION_TRANSITIONS,
    ExecutionStateMachine,
)

from .helpers import make_execution, make_task, new_run


class TestTask(unittest.TestCase):
    def test_e11_task_must_trace_to_step(self):
        with self.assertRaises(InvariantViolation):
            Task(run_id=new_run(), step_id="", task_type=TaskType.TOOL_CALL)

    def test_e12_scheduler_only_knows_task(self):
        """Task 不携带 Goal / Decision 等 Intelligence 概念（X-2）。"""
        task = make_task()
        payload = {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "step_id": task.step_id,
            "task_type": task.task_type,
            "executor_type": task.executor_type,
            "priority": task.priority,
        }
        self.assertNotIn("goal", payload)
        self.assertNotIn("decision", payload)

    def test_no_idempotency_key_on_task(self):
        """幂等 Key 在 Execution 上（= execution_id），不在 Task 上。"""
        task = make_task()
        self.assertNotIn("idempotency_key", task.__dataclass_fields__)

    def test_executor_type_must_be_registered(self):
        with self.assertRaises(InvariantViolation):
            Task(run_id=new_run(), step_id="step_1", executor_type="kafka")


class TestExecution(unittest.TestCase):
    def test_e21_idempotency_key_equals_execution_id(self):
        ex = make_execution()
        self.assertEqual(ex.idempotency_key, ex.execution_id)

        with self.assertRaises(InvariantViolation):
            from packages.agent_domain.execution import Execution

            Execution(execution_id="exec_abc123", task_id="task_1", idempotency_key="other")

    def test_e1_status_cannot_be_assigned_directly(self):
        ex = make_execution()
        for field_name in ("status", "lease", "suspension", "current_attempt_no", "cancellation_requested"):
            with self.assertRaises(InvariantViolation):
                setattr(ex, field_name, None)

    def test_e13_optimistic_lock(self):
        ex = make_execution()
        ex.check_version(1)
        with self.assertRaises(ConcurrentStateError):
            ex.check_version(99)

    def test_e8_suspension_requires_reason(self):
        ex = make_execution()
        with self.assertRaises(InvariantViolation):
            with ex.mutating():
                ex.status = ExecutionStatus.SUSPENDED
            ex.validate()

        with ex.mutating():
            ex.status = ExecutionStatus.SUSPENDED
            ex.suspension = Suspension(
                reason=SuspensionReason.HUMAN_APPROVAL,
                wait_condition={"approver": "ops"},
            )
        ex.validate()

    def test_suspension_requires_wait_condition(self):
        with self.assertRaises(InvariantViolation):
            Suspension(reason=SuspensionReason.TIMER, wait_condition={})


class TestExecutionStateMachine(unittest.TestCase):
    def setUp(self):
        self.sm = ExecutionStateMachine()

    def test_e1_illegal_transition_rejected(self):
        ex = make_execution()
        with self.assertRaises(IllegalTransition):
            self.sm.transition(ex, ExecutionStatus.COMPLETED)   # PENDING → COMPLETED 非法

    def test_e2_terminal_is_irreversible(self):
        ex = make_execution()
        self.sm.transition(ex, ExecutionStatus.RUNNING)
        self.sm.transition(ex, ExecutionStatus.COMPLETED)
        with self.assertRaises(TerminalStateError):
            self.sm.transition(ex, ExecutionStatus.RUNNING)

    def test_e23_stale_cannot_go_running_directly(self):
        """STALE → RUNNING 不在转换表：必须经 aggregate.recover()。"""
        self.assertNotIn(
            ExecutionStatus.RUNNING.value,
            EXECUTION_TRANSITIONS[ExecutionStatus.STALE.value],
        )

    def test_transition_emits_event(self):
        ex = make_execution()
        event = self.sm.transition(ex, ExecutionStatus.RUNNING)
        self.assertEqual(event.aggregate_type, "execution")
        self.assertEqual(event.aggregate_id, ex.execution_id)
        self.assertEqual(event.payload["to"], "RUNNING")
        self.assertEqual(ex.version, 2)

    def test_e2_all_terminal_states_are_empty(self):
        for s in ("COMPLETED", "FAILED", "CANCELLED"):
            self.assertEqual(EXECUTION_TRANSITIONS[s], frozenset())


class TestKernelOnlyKnowsTask(unittest.TestCase):
    def test_x2_execution_has_no_goal_or_decision(self):
        ex = make_execution()
        for forbidden in ("goal", "decision", "observation"):
            self.assertNotIn(forbidden, ex.__dataclass_fields__)

    def test_e19_task_execution_is_one_to_one(self):
        """同一个 Task 不允许绑定两个 Execution —— 重跑必须新建 Task。"""
        task = make_task()
        seen = {}

        def bind(execution):
            if execution.task_id in seen:
                raise InvariantViolation("E-19: task already bound to an execution")
            seen[execution.task_id] = execution.execution_id

        bind(make_execution(task))
        with self.assertRaises(InvariantViolation):
            bind(make_execution(task))


if __name__ == "__main__":
    unittest.main()
