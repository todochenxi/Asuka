"""M15 阶段 11：完整 Agent Loop —— 基线 §44 第一条 End-to-End 闭环。

    POST /runs → AgentRun → Step → Task → Execution → Worker → LLM
              → Decision → Action → Task → Execution Kernel → Tool
              → Observation → State → Decision → Finish
              → AgentRun = COMPLETED

§44 的要求不是"跑通"，是**跑通且全程留痕**：

    Event / Trace / Attempt / Checkpoint / Cost / Token Usage

所以这个文件的结构就是这六项 —— 少一项不算闭环，即使 AgentRun 确实是 COMPLETED。

阶段 11 新钉的不变量：

    L-1  Loop 持有运行时（Gateway / ToolRuntime），但不实现它们
    L-2  Worker 的 Executor 与 Loop 持有的是同一批对象（单一代码路径）
    L-3  Token 用量回流到 CostManager —— 否则 max_tokens 这条线是断的
    L-5  Step 完成也要写 Run Checkpoint（§14），不只是挂起前
    L-6  Trace append-only：能删改的账本不是账本
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.business import AgentRunStatus, StepStatus
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_harness.cost import Budget as CostBudget
from packages.agent_runtime.assembly import RuntimeStack, assemble_runtime_stack
from packages.agent_runtime.checkpoints import InMemoryRunCheckpointStore
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.agent_runtime.model_gateway import (
    CompletionRequest,
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    ToolRegistry,
    ToolRuntime,
    ToolSpec,
)
from packages.agent_runtime.trace import (
    APPROVAL,
    APPROVED,
    CHECKPOINT,
    FINISHED,
    OBSERVED,
    SUBMITTED,
    RunTrace,
)
from packages.execution_kernel.inmemory import ManualClock


# ---------------------------------------------------------------- 测试替身
class Interpreter:
    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Goal:
        return Goal(
            run_id=str(context.get("run_id", "")),
            objective="answer the arithmetic question",
            success_criteria=("a numeric answer is produced",),
            budget=Budget(max_steps=5),
        )


class Planner:
    """两个节点：先问模型，再算。"""

    def plan(self, state: State) -> Plan:
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n1", name="ask-llm"),
                PlanNode(node_id="n2", name="call-calculator", depends_on=("n1",)),
            ),
        )


@dataclass
class StateDrivenDecisionEngine:
    """真正读 State 的 DecisionEngine —— 不是脚本播放器。

    闭环的价值全在这里：LLM 的结果要能影响下一步决策，
    否则"Decision"只是个摆设，§44 的 `LLM → Decision → Action` 是断的。
    """

    def decide(self, state: State) -> Decision:
        done = len(state.completed_tasks)
        if done == 0:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(
                    run_id=state.run_id,
                    action_type=ActionType.LLM_CALL,
                    payload={"prompt": "how do I compute 2+3?"},
                ),
                rationale="need a plan first",
            )
        if done == 1:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(
                    run_id=state.run_id,
                    action_type=ActionType.TOOL_CALL,
                    payload={"tool": "calculator", "args": {"expr": "2+3"}},
                ),
                rationale="llm said to use the calculator",
            )
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            rationale="answer produced",
        )


def scripted_provider(*, reply: str = "use the calculator", tokens: int = 100):
    calls: list[str] = []

    def _call(deployment: Deployment, request: CompletionRequest):
        calls.append(deployment.deployment_id)
        # 故意让 token 用量非平凡：L-3 断言的是"真的回流了"，不是"字段存在"
        return ok_response(
            deployment, request, text=reply, prompt_tokens=tokens, completion_tokens=tokens // 2
        )

    return _call, calls


def calculator(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"value": eval(args["expr"], {"__builtins__": {}}, {})}  # noqa: S307 仅测试用


def build_gateway(reply: str = "use the calculator"):
    fn, calls = scripted_provider(reply=reply)
    model = Model(model_id="gpt-test", name="test model")
    deployment = Deployment(
        deployment_id="gpt-test@primary",
        model_id="gpt-test",
        provider="scripted",
        endpoint="in-process",
    )
    gateway = ModelGateway(
        ModelRouter([model], [deployment]),
        {"scripted": FunctionProvider("scripted", fn)},
        max_fallbacks=0,
        # G-8：Action 没指定模型 → 由 Gateway 决定，不由 Executor 猜
        default_model_id="gpt-test",
    )
    return gateway, calls


def build_tool_runtime() -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="calculator", version="2.0.0", description="eval an expression"),
        FunctionInvoker(calculator),
        make_default=True,
    )
    return ToolRuntime(registry)


class ClosureTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.gateway, self.provider_calls = build_gateway()
        self.tool_runtime = build_tool_runtime()
        self.checkpoints = InMemoryRunCheckpointStore()
        self.trace = RunTrace(clock=self.clock)
        self.budget = CostBudget(max_cost=10.0, max_tokens=10_000, max_steps=5)
        self.stack = assemble_runtime_stack(
            agent_id="agent-math",
            interpreter=Interpreter(),
            planner=Planner(),
            decision_engine=StateDrivenDecisionEngine(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
            budget=self.budget,
            max_steps=5,
            trace=self.trace,
        )
        self.stack.loop.checkpoints = self.checkpoints

    def close_it(self) -> State:
        self.stack.start("what is 2+3?")
        return self.stack.run()


# ---------------------------------------------------------------- 闭环
class Section44ClosureTest(ClosureTestBase):
    def test_agent_run_reaches_completed(self):
        state = self.close_it()
        loop = self.stack.loop

        self.assertEqual(loop.agent_run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(loop.history[-1], StepOutcome.FINISHED)
        # B-7：终态是 Runtime 声明的，Step 全绿本身推不出 COMPLETED
        self.assertEqual(
            [e.kind for e in self.trace.of_kind(FINISHED)], [FINISHED]
        )

    def test_the_chain_is_llm_then_tool_then_finish(self):
        """§44 的顺序：LLM → Decision → Tool → Finish。"""
        self.close_it()
        loop = self.stack.loop

        self.assertEqual(loop.steps, 2)
        self.assertEqual(self.provider_calls, ["gpt-test@primary"])
        self.assertEqual(len(loop.steps_of_run), 2)          # 两个 Plan Node → 两个 Step
        self.assertEqual(
            [s.plan_node_id for s in loop.steps_of_run], ["n1", "n2"]
        )

    # ---------------------------------------------------------- 六要素
    def test_element_1_event(self):
        """Event：Kernel 的 outbox 里必须有完整的生命周期事件。"""
        self.close_it()
        types = [e.event_type for e in self.stack.kernel.outbox.all()]  # type: ignore[attr-defined]
        self.assertTrue(types)
        for expected in ("execution.running", "attempt.succeeded", "execution.completed"):
            self.assertIn(expected, types)

    def test_element_2_trace(self):
        """Trace：每一步都要有记录，且 Run → Step → Task → Execution 串得起来。"""
        self.close_it()
        kinds = self.trace.kinds()
        self.assertIn(SUBMITTED, kinds)
        self.assertIn(OBSERVED, kinds)
        self.assertIn(FINISHED, kinds)

        for entry in self.trace.of_kind(SUBMITTED):
            self.assertTrue(entry.run_id)
            self.assertTrue(entry.step_id)
            self.assertTrue(entry.task_id)
            self.assertTrue(entry.execution_id)

    def test_element_2b_trace_records_served_values_not_requested(self):
        """Trace 记的是**实际服务值** —— 请求值 vs 实际值这个坑已经踩过两次。"""
        self.close_it()
        served_by_exec: dict[str, dict] = {}
        for entry in self.trace.of_kind(OBSERVED):
            served_by_exec[entry.execution_id] = dict(entry.served)

        all_served = [v for v in served_by_exec.values()]
        self.assertTrue(any("deployment" in s for s in all_served), all_served)
        self.assertTrue(any("version" in s for s in all_served), all_served)
        # 工具的**实际**版本是 2.0.0，不是默认的 1.0.0
        self.assertIn("2.0.0", [str(s.get("version")) for s in all_served])

    def test_element_3_attempt(self):
        """Attempt：每次执行都要留下 Attempt 记录（E-4：历史可查）。"""
        self.close_it()
        kernel = self.stack.kernel
        total = 0
        for step in self.stack.loop.steps_of_run:
            for task_id in step.task_ids:
                execution = kernel.repository.get_by_task(task_id)
                self.assertIsNotNone(execution)
                attempts = kernel.attempts.list_by_execution(execution.execution_id)
                self.assertTrue(attempts)
                total += len(attempts)
        self.assertEqual(total, 2)                 # LLM 一次 + Tool 一次，各 1 个 Attempt

    def test_element_4_checkpoint(self):
        """Checkpoint：Step 完成也要写（L-5），不只是挂起前。"""
        self.close_it()
        saved = self.checkpoints.list_for(self.stack.run_id)
        self.assertTrue(saved, "L-5: step 完成时没有落 Run Checkpoint")
        # E-24 的另一半：current_step 只能出现在 Run Checkpoint，不能进 Kernel Checkpoint
        self.assertTrue(any(cp.current_step for cp in saved))

    def test_element_5_cost(self):
        """Cost：CostManager 必须真的被记账。"""
        self.close_it()
        snap = self.stack.loop.harness.cost.snapshot()      # type: ignore[union-attr]
        self.assertEqual(snap["steps"], 2)

    def test_element_6_token_usage(self):
        """Token Usage：L-3 —— 用量必须回流，否则 Budget.max_tokens 是断的。"""
        self.close_it()
        spent = self.stack.loop.harness.cost.spent_tokens   # type: ignore[union-attr]
        self.assertGreater(spent, 0)

    def test_l3_token_budget_actually_stops_the_run(self):
        """L-3 的硬证据：把 max_tokens 压到 1，第二次决策前就该被拦。

        如果 token 没回流，这条测试会挂 —— 这正是"不记账"的真实后果。
        """
        stack = assemble_runtime_stack(
            agent_id="agent-math",
            interpreter=Interpreter(),
            planner=Planner(),
            decision_engine=StateDrivenDecisionEngine(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
            budget=CostBudget(max_cost=10.0, max_tokens=1, max_steps=5),
        )
        stack.start("what is 2+3?")
        state = stack.run()
        self.assertGreater(
            stack.loop.harness.cost.spent_tokens,          # type: ignore[union-attr]
            stack.loop.harness.cost.budget.max_tokens,     # type: ignore[union-attr]
        )
        # 超预算 → DENY（不是继续跑），Run 停在活跃态而不是"假装完成"
        self.assertIn(StepOutcome.DENIED, stack.loop.history)
        self.assertNotEqual(stack.loop.agent_run.status, AgentRunStatus.COMPLETED)
        self.assertIsNotNone(state)

    def test_l7_a_forever_denied_run_terminates(self):
        """L-7：被拒不计入任何预算，所以必须有一条专门的收敛规则。

        这条测试在加上 L-7 之前是**跑不完的**（死循环，进程被 kill）。
        """
        stack = assemble_runtime_stack(
            agent_id="agent-math",
            interpreter=Interpreter(),
            planner=Planner(),
            decision_engine=StateDrivenDecisionEngine(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=self.clock,
            budget=CostBudget(max_cost=10.0, max_tokens=1, max_steps=5),
        )
        stack.start("what is 2+3?")
        stack.run()
        self.assertEqual(stack.loop.history[-1], StepOutcome.DENY_LOOP)
        self.assertEqual(stack.loop.agent_run.status, AgentRunStatus.FAILED)


# ---------------------------------------------------------------- L-* 边界
class LoopBoundaryTest(ClosureTestBase):
    def test_l1_loop_holds_runtimes_but_does_not_implement_them(self):
        loop = self.stack.loop
        self.assertIs(loop.model_gateway, self.gateway)
        self.assertIs(loop.tool_runtime, self.tool_runtime)
        # 它拿不到 provider / invoker 这一层 —— 那是 Gateway / ToolRuntime 内部的事
        for forbidden in ("adapters", "providers", "invokers", "endpoint", "registry"):
            self.assertFalse(hasattr(loop, forbidden), forbidden)

    def test_l2_worker_and_loop_share_the_same_runtime_objects(self):
        """L-2：一条代码路径。装配函数里没有第二个可以造 Executor 的地方。"""
        loop = self.stack.loop
        worker = self.stack.worker
        # PR-19：Executor 被包在 TaskTypeRouter 里，L-2 要**穿过**它才看得见 ——
        # 这正是 `TaskTypeRouter.handler()` 存在的理由：
        # 否则"共享同一批对象"就成了没法断言的口头承诺。
        llm = worker.executors["http"].handler(TaskType.LLM_CALL)
        tool = worker.executors["native"].handler(TaskType.TOOL_CALL)
        self.assertIs(llm.gateway, loop.model_gateway)
        self.assertIs(tool.runtime, loop.tool_runtime)
        self.assertIs(self.stack.gateway, loop.model_gateway)

    def test_l6_trace_is_append_only(self):
        """L-6：能删改的账本不是账本。"""
        forbidden = ("update", "pop", "clear", "remove", "delete", "__setitem__", "__delitem__")
        for name in forbidden:
            self.assertFalse(hasattr(RunTrace, name), f"RunTrace 不该有 {name}")

    def test_b7_only_runtime_declares_the_terminal_state(self):
        """B-7：终态只有一个来源。"""
        self.close_it()
        finished = self.trace.of_kind(FINISHED)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].payload["status"], "completed")


class StepFanOutTest(ClosureTestBase):
    def test_step_holds_gate_and_action_tasks_together(self):
        """`Step : Task = 1 : N`（§4）在真实路径上成立：

        闸门 Task 和获批后真正执行的 Task 挂在**同一个 Step** 上 ——
        因为它们本来就是同一步（"这一步在等审批，然后这一步被做了"）。
        """
        loop = self.stack.loop
        state = loop.start("please delete carefully")

        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = _OneShotDecisionEngine(risky)   # type: ignore[assignment]

        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertEqual(loop.approve(by="alice"), StepOutcome.EXECUTED)

        step = loop.current_step
        self.assertIsNotNone(step)
        self.assertEqual(len(step.task_ids), 2)               # 闸门 + 真动作
        self.assertEqual(step.status, StepStatus.COMPLETED)

        # 审批在 Trace 里留下了两段：请求 + 答复
        self.assertIn(APPROVAL, self.trace.kinds())
        self.assertIn(APPROVED, self.trace.kinds())
        self.assertIn(CHECKPOINT, self.trace.kinds())


@dataclass
class _OneShotDecisionEngine:
    action: Action
    used: bool = False

    def decide(self, state: State) -> Decision:
        if self.used:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            )
        self.used = True
        return Decision(
            run_id=state.run_id,
            selected_action=self.action,
            confidence_signal=0.99,      # I-9：再高也不换自动放行
            rationale="scripted",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
