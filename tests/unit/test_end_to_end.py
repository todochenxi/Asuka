"""最小闭环（M15 §7 验收）+ 跨层不变量：E-14、X-1、X-3、X-4、X-6、X-9、X-12。"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import unittest
from datetime import timedelta

import packages.agent_domain as domain
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import (
    ErrorInfo,
    ExecutionStatus,
    ExecutorType,
    FailureClass,
    RetryPolicy,
    SuspensionReason,
    TaskType,
)
from packages.agent_domain.intelligence import (
    Action,
    ActionResolver,
    ActionType,
    Decision,
    Goal,
    Observation,
    PassthroughReducer,
    State,
    interpret_goal,
)

from .helpers import make_task, new_run


class _Interpreter:
    def interpret(self, user_request: str, context) -> Goal:
        return Goal(
            run_id=context["run_id"],
            objective=user_request,
            success_criteria=("final answer cites retrieved data",),
        )


class TestMinimalLoop(unittest.TestCase):
    def test_one_agent_step_end_to_end(self):
        run_id = new_run()

        # 1) Goal（I-1）
        goal = interpret_goal("查一下账上余额", _Interpreter(), run_id=run_id)

        # 2) State
        state = State(run_id=run_id, goal=goal)

        # 3) Decision → Action（I-4）
        action = Action(
            run_id=run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "get_balance", "args": {"account": "A-1"}},
        )
        decision = Decision(run_id=run_id, selected_action=action, confidence_signal=0.7)
        resolved = ActionResolver().resolve(decision)

        # 4) Action → Task（Tool Call 产生 Task，不产生 ToolExecution）
        task = make_task(
            run_id=run_id,
            task_type=TaskType.TOOL_CALL,
            executor_type=ExecutorType.NATIVE,
            payload=dict(resolved.payload),
        )

        # 5) Kernel 受理：Execution + Attempt + Lease
        from packages.agent_domain.execution import ExecutionAggregate, Execution

        execution = Execution(task_id=task.task_id)
        agg = ExecutionAggregate(execution)
        attempt, lease = agg.claim(worker_id="worker-1", ttl=timedelta(seconds=30))
        self.assertEqual(execution.status, ExecutionStatus.RUNNING)
        self.assertEqual(execution.idempotency_key, execution.execution_id)

        # 6) Worker 执行成功 → Attempt SUCCEEDED → Execution COMPLETED
        agg.succeed(token=lease.fencing_token, result={"balance": 100})
        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)

        # 7) Execution Result → Observation → State（X-7）
        obs = Observation.from_execution_result(
            run_id=run_id,
            execution_id=execution.execution_id,
            attempt_no=attempt.attempt_no,
            kind="tool_result",
            summary="balance=100",
            content={"balance": 100},
        )
        state.apply(obs, PassthroughReducer())
        self.assertEqual(len(state.observations), 1)

        # 8) 事件流完整（X-3）
        event_types = [e.event_type for e in agg.events]
        self.assertIn("execution.running", event_types)
        self.assertIn("attempt.succeeded", event_types)
        self.assertIn("execution.completed", event_types)
        self.assertIn("lease.acquired", event_types)

        # X-4：Observation 与 Event 是两个通道
        self.assertNotIn(obs.observation_id, [e.event_id for e in agg.events])

    def test_failed_then_retry_then_success(self):
        from packages.agent_domain.execution import Execution, ExecutionAggregate

        run_id = new_run()
        task = make_task(run_id=run_id, task_type=TaskType.TOOL_CALL)
        agg = ExecutionAggregate(Execution(task_id=task.task_id))

        _, lease1 = agg.claim(worker_id="w1")
        _, will_retry = agg.fail(
            token=lease1.fencing_token,
            error=ErrorInfo(code="timeout", message="timeout", failure_class=FailureClass.TRANSIENT),
            retry_policy=RetryPolicy(max_attempts=3),
        )
        self.assertTrue(will_retry)

        attempt2, lease2 = agg.claim(worker_id="w1")
        self.assertEqual(attempt2.attempt_no, 2)
        self.assertGreater(lease2.fencing_token, lease1.fencing_token)

        agg.succeed(token=lease2.fencing_token, result={"ok": True})
        self.assertEqual(agg.execution.status, ExecutionStatus.COMPLETED)

    def test_hitl_suspend_then_resume(self):
        from packages.agent_domain.execution import Execution, ExecutionAggregate

        run_id = new_run()
        agg = ExecutionAggregate(Execution(task_id=make_task(run_id=run_id).task_id))
        agg.claim(worker_id="w1")
        agg.suspend(
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approver": "risk"},
        )
        self.assertIsNone(agg.execution.lease)
        attempt, _ = agg.resume(worker_id="w1")
        self.assertEqual(attempt.attempt_no, 2)


class TestNoNestedExecutionObjects(unittest.TestCase):
    def test_e14_no_task_execution_or_tool_execution(self):
        """Kernel 只有 Task / Execution / Attempt，不允许出现套娃对象。"""
        forbidden = ("TaskExecution", "ToolExecution", "LLMExecution")
        found: list[str] = []
        for mod_name in ("execution", "intelligence", "events", "ids", "errors"):
            module = importlib.import_module(f"packages.agent_domain.{mod_name}")
            for name, _ in inspect.getmembers(module, inspect.isclass):
                if name in forbidden:
                    found.append(f"{module.__name__}.{name}")
        for pkg in ("execution", "intelligence"):
            package = importlib.import_module(f"packages.agent_domain.{pkg}")
            for _, mod_name, _ in pkgutil.iter_modules(package.__path__):
                module = importlib.import_module(f"packages.agent_domain.{pkg}.{mod_name}")
                for name, _ in inspect.getmembers(module, inspect.isclass):
                    if name in forbidden:
                        found.append(f"{module.__name__}.{name}")
        self.assertEqual(found, [])

    def test_x6_agentrun_is_not_kernel_execution(self):
        """AgentRun 是业务 Run，不是 Kernel Execution：Execution 上没有 run 级业务字段。"""
        from packages.agent_domain.execution import Execution

        for forbidden in ("agent_run_id", "goal", "user_request"):
            self.assertNotIn(forbidden, Execution.__dataclass_fields__)

    def test_x9_step_belongs_to_business_domain(self):
        """Step 只以 step_id 形式出现在 Task 上，Kernel 不定义 Step 对象。"""
        from packages.agent_domain.execution import Task

        self.assertIn("step_id", Task.__dataclass_fields__)
        with self.assertRaises(ImportError):
            importlib.import_module("packages.agent_domain.execution.step")


if __name__ == "__main__":
    unittest.main()
