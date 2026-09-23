"""M95：把"有模块、没接线"的三样接进运行时，并证明它们会响。

    ContextAssembler  → loop._context_payload 发 `context.built` + 快照
    Guardrail OUTPUT   → 出声之前拦下密钥泄漏（H-7）
    Guardrail INPUT    → 进入决策之前拦下敏感输入

⚠️ 没有规则 / 没有 assembler 时**行为不变** —— 这是接线的底线：
接了但不改变默认行为，配了才生效。
"""
from __future__ import annotations

import unittest

from examples.demo_stack import (
    DemoDecisionEngine,
    DemoInterpreter,
    DemoPlanner,
    build_model_gateway,
    build_tool_runtime,
)
from packages.agent_context.assembler import ContextAssembler
from packages.agent_context.memory import InMemoryMemoryStore, MemoryManager
from packages.agent_harness.approval import InMemoryApprovalStore
from packages.agent_harness.guardrail import (
    GuardrailEngine,
    SecretPatternGuardrail,
    SensitiveTopicGuardrail,
)
from packages.agent_harness.harness import Harness
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.model_gateway import (
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)


def _echo_gateway(text: str, *, cost: float = 0.0) -> ModelGateway:
    model = Model(model_id="fake")
    deployment = Deployment(
        deployment_id="fake@in-process", model_id="fake", provider="fake"
    )

    def complete(dep, request):
        return ok_response(
            dep,
            request,
            text=text,
            prompt_tokens=1,
            completion_tokens=1,
            metadata={"cost_usd": cost},
        )

    return ModelGateway(
        ModelRouter([model], [deployment]),
        {"fake": FunctionProvider("fake", complete)},
        max_fallbacks=0,
        default_model_id="fake",
    )


def _stack(gateway, *, harness=None, assembler=None, memory=None):
    return assemble_runtime_stack(
        agent_id="t",
        interpreter=DemoInterpreter(),
        planner=DemoPlanner(),
        decision_engine=DemoDecisionEngine(),
        gateway=gateway,
        tool_runtime=build_tool_runtime(),
        harness=harness,
        context_assembler=assembler,
        memory=memory,
        approval_store=InMemoryApprovalStore(),
    )


def _kinds(loop) -> list[str]:
    return [e.kind for e in loop.trace.entries]


class ContextAssemblyIsWiredTest(unittest.TestCase):
    def test_a_context_snapshot_is_built_and_traced(self) -> None:
        stack = _stack(
            build_model_gateway(),
            harness=Harness.default(),
            assembler=ContextAssembler(),
        )
        stack.start("compute 6*7")
        stack.run()

        self.assertIn("context.built", _kinds(stack.loop))
        built = next(e for e in stack.loop.trace.entries if e.kind == "context.built")
        self.assertIn("snapshot_id", built.payload)
        self.assertGreaterEqual(built.payload["total_tokens"], 0)

    def test_without_an_assembler_nothing_changes(self) -> None:
        """控制组：不接 assembler 就**没有** `context.built` —— 证明上一条是接线带来的。"""
        stack = _stack(build_model_gateway())
        stack.start("compute 6*7")
        stack.run()
        self.assertNotIn("context.built", _kinds(stack.loop))


class OutputGuardrailTest(unittest.TestCase):
    def test_a_leaked_secret_fails_the_run(self) -> None:
        harness = Harness.default(
            guardrails=GuardrailEngine(guardrails=(SecretPatternGuardrail(),))
        )
        stack = _stack(_echo_gateway("here is the key sk-deadbeef"), harness=harness)
        stack.start("give me the key")
        stack.run()

        self.assertEqual(stack.loop.agent_run.status.value, "failed")
        events = [e for e in stack.loop.trace.entries if e.kind == "guardrail.rejected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["stage"], "output")

    def test_a_clean_answer_finishes(self) -> None:
        harness = Harness.default(
            guardrails=GuardrailEngine(guardrails=(SecretPatternGuardrail(),))
        )
        stack = _stack(_echo_gateway("EXPIRE key seconds sets a timeout"), harness=harness)
        stack.start("how do I set a timeout")
        stack.run()

        self.assertEqual(stack.loop.agent_run.status.value, "completed")
        self.assertNotIn("guardrail.rejected", _kinds(stack.loop))


class InputGuardrailTest(unittest.TestCase):
    def test_a_sensitive_request_never_reaches_the_decision(self) -> None:
        harness = Harness.default(
            guardrails=GuardrailEngine(
                guardrails=(SensitiveTopicGuardrail(keywords=("classified",)),)
            )
        )
        stack = _stack(_echo_gateway("ok"), harness=harness)
        stack.start("this document is classified")
        stack.run()

        self.assertEqual(stack.loop.agent_run.status.value, "failed")
        events = [e for e in stack.loop.trace.entries if e.kind == "guardrail.rejected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["stage"], "input")
        # 输入被拦 ⇒ **没有**任何 Task 提交（决策链根本没开始）。
        self.assertNotIn("task.submitted", _kinds(stack.loop))


class CostBudgetIsEnforcedTest(unittest.TestCase):
    def test_cost_reaches_the_costmanager(self) -> None:
        """M96：`cost_usd` 必须回到 CostManager —— 否则 `max_cost` 死活不生效。"""
        from packages.agent_harness.cost import Budget as CostBudget

        harness = Harness.default(budget=CostBudget(max_cost=0.0))
        stack = _stack(_echo_gateway("answer", cost=0.5), harness=harness)
        stack.start("q")
        stack.run()

        self.assertEqual(stack.loop.harness.cost.spent_cost, 0.5)
        self.assertTrue(stack.loop.harness.cost.exceeded)

    def test_it_denies_the_next_action_once_over_budget(self) -> None:
        """超预算之后 `before_action` 会 DENY —— 难点在"下一步"要真的发生。"""
        from packages.agent_domain.intelligence.action import Action, ActionType
        from packages.agent_harness.cost import Budget as CostBudget

        harness = Harness.default(budget=CostBudget(max_cost=0.0))
        stack = _stack(_echo_gateway("answer", cost=0.5), harness=harness)
        state = stack.start("q")
        # 先花掉一次（模拟已经跑过一步 LLM）。
        stack.loop.harness.charge(cost=0.5)
        action = Action(
            run_id=state.run_id,
            action_type=ActionType.LLM_CALL,
            payload={"prompt": "again"},
        )
        verdict = stack.loop.harness.before_action(action)
        self.assertEqual(verdict.verdict.value, "deny")
        self.assertIn("cost budget exhausted", "; ".join(verdict.reasons))

    def test_the_control_without_a_budget_it_is_allowed(self) -> None:
        """控制组：不限预算时同一个动作被放行 —— 上一条不是"一律拒绝"。"""
        from packages.agent_domain.intelligence.action import Action, ActionType

        stack = _stack(_echo_gateway("answer"), harness=Harness.default())
        state = stack.start("q")
        action = Action(
            run_id=state.run_id,
            action_type=ActionType.LLM_CALL,
            payload={"prompt": "again"},
        )
        self.assertEqual(
            stack.loop.harness.before_action(action).verdict.value, "allow"
        )


class MemoryWriteTest(unittest.TestCase):
    def test_a_completed_run_remembers_its_outcome(self) -> None:
        memory = MemoryManager(InMemoryMemoryStore())
        stack = _stack(
            _echo_gateway("EXPIRE key seconds sets a timeout"),
            assembler=ContextAssembler(memory_provider=lambda req: memory.recall(subject="t")),
            memory=memory,
        )
        stack.start("how do I set a timeout")
        stack.run()

        recalled = memory.recall(subject="t")
        self.assertTrue(recalled, "完成之后必须留下一条 episodic 记忆")
        self.assertIn("EXPIRE key seconds", "\n".join(i.text for i in recalled))

    def test_memory_is_keyed_by_agent_not_run(self) -> None:
        """控制组：按 run_id 查**查不到** —— 证明记忆不是"每 Run 各一份"。"""
        memory = MemoryManager(InMemoryMemoryStore())
        stack = _stack(_echo_gateway("answer"), memory=memory)
        stack.start("q")
        stack.run()

        # 记忆挂在 agent_id 上，不在 run_id 上。
        self.assertTrue(memory.recall(subject="t"))
        self.assertEqual(memory.recall(subject=stack.run_id), ())
        self.assertEqual(memory.recall(subject="t2"), ())


class _ReviewOutput:
    """OUTPUT 阶段、REVIEW 严重度的一条测试护栏（命中关键词 → 人确认）。"""

    name = "review-output"
    stage = __import__(
        "packages.agent_harness.guardrail", fromlist=["GuardrailStage"]
    ).GuardrailStage.OUTPUT

    def __init__(self, keyword: str) -> None:
        from packages.agent_harness.guardrail import (
            GuardrailFinding,
            GuardrailSeverity,
        )
        from collections.abc import Mapping

        self._finding = None
        self._guardrail_finding = GuardrailFinding
        self._severity = GuardrailSeverity
        self._keyword = keyword

    def check(self, target, *, context):
        text = target if isinstance(target, str) else str(target)
        if self._keyword in text:
            return self._guardrail_finding(
                guardrail=self.name,
                stage=self.stage,
                severity=self._severity.REVIEW,
                message=f"output contains {self._keyword!r}; a human should look",
            )
        return None


class OutputGuardrailReviewTest(unittest.TestCase):
    """H-7：REVIEW 不是 FAILED —— 它把人叫到闸门前。"""

    def _review_harness(self) -> Harness:
        return Harness.default(
            guardrails=GuardrailEngine(guardrails=(_ReviewOutput("proprietary"),))
        )

    def test_a_review_output_suspends_for_approval(self) -> None:
        stack = _stack(
            _echo_gateway("this is proprietary material"), harness=self._review_harness()
        )
        stack.start("summarize the doc")
        stack.run()

        # 挂起等人，而不是判死。
        self.assertEqual(stack.loop.agent_run.status.value, "suspended")
        self.assertIsNotNone(stack.loop.pending_approval)
        self.assertIn(
            "guardrail[output]", stack.loop.pending_approval.reason
        )

    def test_approving_lets_it_finish(self) -> None:
        stack = _stack(
            _echo_gateway("this is proprietary material"), harness=self._review_harness()
        )
        stack.start("summarize the doc")
        stack.run()
        stack.loop.approve(by="alice")
        stack.run()

        self.assertEqual(stack.loop.agent_run.status.value, "completed")


if __name__ == "__main__":
    unittest.main()
