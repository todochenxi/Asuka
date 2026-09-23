"""阶段 8：最小 Agent Loop 闭环。

    Goal → Plan → Decision → Action → Task → Kernel/Worker → Observation → State → …

重点验证的是**边界有没有守住**，不是"能不能跑通"：

    I-1   Goal 必须由 Interpreter 解释
    I-3   State 只能经 Observation + Reducer 变更（Plan 也不例外）
    I-4   Decision 不可执行 —— 只有 Action → TaskFactory → Task 才产生副作用
    I-6   来自执行的 Observation 只能由 from_execution_result 构造
    I-9   confidence 再高也不能自动执行高风险动作
    X-1   Runtime 造出 Task 即交棒，不碰执行生命周期
    X-10  同一 run 的 State 写入串行化（expected_version 校验）
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.errors import ConcurrentStateError, InvariantViolation
from packages.agent_domain.execution import ExecutionStatus, FailureClass
from packages.agent_domain.execution.retry import FailureClass as FC
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.executors import (
    LLMCallExecutor,
    ToolCallExecutor,
    ToolRegistry,
)
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.task_factory import TaskFactory
from packages.execution_kernel.inmemory import (
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import ExecutorError, Worker, WorkerConfig


# ---------------------------------------------------------------- 测试替身
class ScriptedInterpreter:
    def __init__(self, objective: str = "answer the question", max_steps: int = 6) -> None:
        self.objective = objective
        self.max_steps = max_steps

    def interpret(self, user_request: str, context: Mapping[str, object]) -> Goal:
        # I-2：success_criteria 必填，否则 Goal 不合法
        return Goal(
            run_id=str(context.get("run_id", "")),
            objective=self.objective,
            success_criteria=("answer is produced",),
            budget=Budget(max_steps=self.max_steps),
        )


class ScriptedPlanner:
    def __init__(self, nodes: int = 2) -> None:
        self.nodes = nodes
        self.calls = 0

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(
            run_id=state.run_id,
            nodes=tuple(
                PlanNode(node_id=f"n{i}", name=f"step-{i}") for i in range(self.nodes)
            ),
        )


@dataclass
class ScriptedDecisionEngine:
    """按脚本吐 Decision，用完最后一个就返回 selected_action=None（= 目标达成）。"""

    script: list[Action]
    index: int = 0
    risk_level: RiskLevel = RiskLevel.LOW

    def decide(self, state: State) -> Decision:
        if self.index >= len(self.script):
            # Decision 必须带 Action（构造时校验），所以"没有下一步"= FINISH
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
                rationale="nothing left to do",
            )
        action = self.script[self.index]
        self.index += 1
        return Decision(
            run_id=state.run_id,
            selected_action=action,
            confidence_signal=0.95,          # I-9：高 signal 也不能换自动放行
            rationale="scripted",
        )


class FakeLLM:
    def __init__(self, reply: str = "42") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        self.prompts.append(prompt)
        return {"text": self.reply}


class BrokenLLM:
    def complete(self, prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        raise RuntimeError("upstream 503")


def calculator(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"value": eval(args["expr"], {"__builtins__": {}}, {})}  # noqa: S307 仅测试用


# ---------------------------------------------------------------- 装配
class LoopTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=self.clock,
        )
        self.registry = ToolRegistry()
        self.registry.register("calculator", calculator)
        self.llm = FakeLLM("the answer is 5")
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors={
                "native": ToolCallExecutor(self.registry),
                "http": LLMCallExecutor(self.llm),
            },
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )
        self.planner = ScriptedPlanner()

    def _loop(
        self, script: list[Action], *, max_steps: int = 6, **config
    ) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine(script),
            config=AgentLoopConfig(max_steps=max_steps, **config),
        )

    def _action(self, action_type: ActionType, **payload) -> Action:
        return Action(
            run_id="",                       # 先占位，start() 之后再重建真正的 run_id
            action_type=action_type,
            payload=payload,
        )

    def _with_run(self, loop: AgentLoop, action_type: ActionType, **payload) -> Action:
        state = loop.start("2+3=?")
        return Action(
            run_id=state.run_id,
            action_type=action_type,
            payload=payload,
        )


class FanOutTest(LoopTestBase):
    """M97：一个 Action 扇出多个 Task，它们属于**同一个 Step**（§2.3）。"""

    def _fan_out_loop(self, branches: list[dict]) -> AgentLoop:
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("batch add")
        action = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tasks": branches},
        )
        loop.decision_engine = ScriptedDecisionEngine([action])
        return loop

    def test_one_action_becomes_many_tasks_on_one_step(self) -> None:
        loop = self._fan_out_loop(
            [
                {"tool": "calculator", "args": {"expr": "1+1"}},
                {"tool": "calculator", "args": {"expr": "2+2"}},
                {"tool": "calculator", "args": {"expr": "3+3"}},
            ]
        )
        outcome = loop.step()

        self.assertEqual(outcome.value, "executed")
        # 一次 `_execute` 把三个分支全部跑完 —— Step 只有一个，Task 有三个。
        step = loop.current_step
        assert step is not None
        self.assertEqual(len(step.task_ids), 3)
        self.assertEqual(len({t.step_id for t in loop.steps_of_run}), 1)

    def test_the_fan_out_step_status_is_derived_from_all_branches(self) -> None:
        """Step.status 是**全部** Task 的聚合 —— 三片全 COMPLETED 才 COMPLETED。"""
        loop = self._fan_out_loop(
            [
                {"tool": "calculator", "args": {"expr": "1+1"}},
                {"tool": "calculator", "args": {"expr": "2+2"}},
            ]
        )
        loop.step()
        step = loop.current_step
        assert step is not None
        self.assertEqual(step.status.value, "completed")

    def test_the_control_a_plain_action_is_still_one_task(self) -> None:
        """控制组：非扇出 Action 仍然是一个 Step 一个 Task —— 1:N 不改 1:1。"""
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("one")
        loop.decision_engine = ScriptedDecisionEngine(
            [
                Action(
                    run_id=state.run_id,
                    action_type=ActionType.TOOL_CALL,
                    payload={"tool": "calculator", "args": {"expr": "1+1"}},
                )
            ]
        )
        loop.step()
        step = loop.current_step
        assert step is not None
        self.assertEqual(len(step.task_ids), 1)


class MinimalLoopTest(LoopTestBase):
    def test_full_loop_reaches_goal(self) -> None:
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("what is 2+3?")

        # I-1：Goal 是解释出来的，不是原始请求
        self.assertEqual(state.goal.objective, "answer the question")
        self.assertTrue(state.goal.success_criteria)

        outcome = loop.step()
        self.assertEqual(outcome, StepOutcome.FINISHED)     # 没有下一步 → 目标达成
        self.assertEqual(state.runtime_status, "FINISHED")

    def test_llm_then_tool_then_finish(self) -> None:
        """最小闭环：一个 LLM + 一个 Tool。"""
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("what is 2+3?")

        llm_action = Action(run_id=state.run_id, action_type=ActionType.LLM_CALL,
                            payload={"prompt": "compute 2+3"})
        tool_action = Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                             payload={"tool": "calculator", "args": {"expr": "2+3"}})
        loop.decision_engine = ScriptedDecisionEngine([llm_action, tool_action])

        self.assertEqual(loop.step(), StepOutcome.EXECUTED)   # LLM
        self.assertEqual(loop.step(), StepOutcome.EXECUTED)   # Tool
        self.assertEqual(loop.step(), StepOutcome.FINISHED)

        self.assertEqual(loop.steps, 2)
        self.assertEqual(self.llm.prompts, ["compute 2+3"])
        # Tool 的结果经 Observation → Reducer 进了 State
        self.assertEqual(len(state.completed_tasks), 2)
        self.assertEqual(state.version, 5)   # 起始 1 + plan/2×execution/finished 共 4 次 apply

    def test_plan_enters_state_only_via_observation(self) -> None:
        """I-3：Plan 也不能被 Loop 直接塞进 State。"""
        loop = self._loop([])
        state = loop.start("hi")

        with self.assertRaises(InvariantViolation) as ctx:
            state.current_plan = Plan(run_id=state.run_id)
        self.assertIn("I-3", str(ctx.exception))

        loop.step()                                        # 正常路径：规划
        self.assertIsNotNone(state.current_plan)
        self.assertEqual(self.planner.calls, 1)

    def test_failure_is_also_an_observation(self) -> None:
        """失败不是终点：它也是事实，Agent 要靠它决定下一步。"""
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("boom")
        bad = Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                     payload={"tool": "does_not_exist"})
        loop.decision_engine = ScriptedDecisionEngine([bad])

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        # 失败同样进 State，且 runtime_status 没有变成 FINISHED
        self.assertEqual(len(state.completed_tasks), 1)
        self.assertEqual(state.runtime_status, "RUNNING")
        self.assertIn("execution_failed",
                      str(state.variables[f"result:{state.completed_tasks[0]}"]))


class ApprovalTest(LoopTestBase):
    def test_high_risk_is_suspended_not_executed(self) -> None:
        """I-9：confidence 0.95 也换不来自动执行 —— 高风险必须挂起。"""
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("delete everything")
        risky = Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                       payload={"tool": "calculator", "args": {"expr": "1"}},
                       risk_level=RiskLevel.HIGH)
        loop.decision_engine = ScriptedDecisionEngine([risky])

        outcome = loop.step()
        self.assertEqual(outcome, StepOutcome.WAITING_APPROVAL)
        self.assertEqual(loop.steps, 0)                    # 一步都没执行
        self.assertEqual(len(state.completed_tasks), 0)
        self.assertIsNotNone(loop.pending_action)

        # 没有放行之前，再 step() 也是等待
        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)

    def test_approve_then_execute(self) -> None:
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("careful")
        risky = Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                       payload={"tool": "calculator", "args": {"expr": "7*6"}},
                       risk_level=RiskLevel.HIGH)
        loop.decision_engine = ScriptedDecisionEngine([risky])

        loop.step()
        self.assertEqual(loop.approve(), StepOutcome.EXECUTED)
        self.assertEqual(len(state.completed_tasks), 1)

    def test_human_approval_action_always_waits(self) -> None:
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("ask human")
        approval = Action(run_id=state.run_id, action_type=ActionType.HUMAN_APPROVAL,
                          payload={"question": "ok?"}, timeout=timedelta(seconds=30))
        loop.decision_engine = ScriptedDecisionEngine([approval])

        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)


class BudgetTest(LoopTestBase):
    def test_budget_exhausted_stops_the_loop(self) -> None:
        # Goal 的 budget 优先于 Loop 默认配置（预算是 Goal 的一部分，不是调度参数）
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=2),
            planner=self.planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("loop forever")
        actions = [
            Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                   payload={"tool": "calculator", "args": {"expr": "1+1"}})
            for _ in range(4)
        ]
        loop.decision_engine = ScriptedDecisionEngine(actions)

        self.assertEqual(loop.step(), StepOutcome.EXECUTED)
        self.assertEqual(loop.step(), StepOutcome.EXECUTED)
        self.assertEqual(loop.step(), StepOutcome.BUDGET_EXHAUSTED)

    def test_run_stops_at_terminal_outcome(self) -> None:
        loop = self._loop([])
        state = loop.start("done")
        loop.run()
        self.assertEqual(loop.history[-1], StepOutcome.FINISHED)


class TaskFactoryTest(unittest.TestCase):
    def test_finish_produces_no_task(self) -> None:
        factory = TaskFactory()
        action = Action(run_id="run_1", action_type=ActionType.FINISH)
        with self.assertRaises(InvariantViolation):
            factory.from_action(action)

    def test_llm_call_becomes_llm_task_over_http(self) -> None:
        factory = TaskFactory()
        action = Action(run_id="run_1", action_type=ActionType.LLM_CALL,
                        payload={"prompt": "hi"})
        task = factory.from_action(action)
        self.assertEqual(task.task_type.value, "llm_call")
        self.assertEqual(task.executor_type.value, "http")
        self.assertTrue(task.step_id)                      # E-11：可溯源到 Step

    def test_high_risk_lowers_priority_and_carries_risk(self) -> None:
        factory = TaskFactory()
        action = Action(run_id="run_1", action_type=ActionType.TOOL_CALL,
                        payload={"tool": "x"}, risk_level=RiskLevel.HIGH)
        task = factory.from_action(action)
        self.assertLess(task.priority, 0)
        self.assertEqual(task.payload["risk_level"], "high")


class StateConcurrencyTest(LoopTestBase):
    def test_apply_detects_version_conflict(self) -> None:
        """X-10：冲突必须抛错，不允许静默覆盖。"""
        loop = self._loop([])
        state = loop.start("x")
        from packages.agent_domain.intelligence.observation import Observation

        obs = Observation(run_id=state.run_id, kind="test", summary="s")
        state.apply(obs, loop.reducer, expected_version=state.version)
        with self.assertRaises(ConcurrentStateError):
            state.apply(obs, loop.reducer, expected_version=state.version - 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
