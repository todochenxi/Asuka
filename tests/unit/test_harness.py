"""M16：Harness —— Policy / Guardrail / Cost / HITL。

验证的重点是**边界有没有守住**，不是"能不能跑通"：

    H-1  DENY / REQUIRE_APPROVAL 必须带 reason（审计要知道为什么）
    H-2  REQUIRE_APPROVAL 必须带那条 ApprovalRequest
    H-3  confidence_signal 传不进 Policy（I-9 的类型级落地）
    H-4  Harness 字段里没有 kernel（它无法反向调用 Kernel）
    H-5  审批必须有截止时间
    H-6  终态审批不可再改
    H-7  Guardrail BLOCK 不给人审批绕过的机会
    H-8  过期审批不能被批准

    X-11 Harness 只能**请求**挂起，写 SUSPENDED 的是 Kernel
"""
from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import timedelta

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import ExecutionStatus
from packages.agent_domain.execution.execution import SuspensionReason
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_harness import (
    ApprovalRequest,
    ApprovalStatus,
    Budget,
    CostManager,
    GuardrailEngine,
    GuardrailSeverity,
    GuardrailStage,
    HumanLoop,
    InMemoryApprovalStore,
    PolicyContext,
    PolicyEngine,
    PolicyRule,
    SecretPatternGuardrail,
    SensitiveTopicGuardrail,
    ToolAllowlistGuardrail,
    Verdict,
)
from packages.agent_harness.harness import Harness, HarnessVerdict
from packages.agent_harness.ports import ManualClock as HarnessClock
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.execution_kernel.inmemory import (
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import Worker, WorkerConfig

from .test_agent_loop import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    ToolCallExecutor,
    ToolRegistry,
    calculator,
)


RUN = "run_h"


def act(
    action_type: ActionType = ActionType.TOOL_CALL,
    *,
    risk: RiskLevel = RiskLevel.LOW,
    timeout: timedelta | None = None,
    **payload,
) -> Action:
    payload.setdefault("tool", "calculator")
    payload.setdefault("args", {"expr": "1+1"})
    # I-8：HUMAN_APPROVAL 必须带 timeout，所以这里给个默认，不是可选装饰
    if action_type is ActionType.HUMAN_APPROVAL and timeout is None:
        timeout = timedelta(minutes=5)
    return Action(
        run_id=RUN,
        action_type=action_type,
        payload=payload,
        risk_level=risk,
        timeout=timeout,
    )


# ============================================================ Policy


class PolicyTest(unittest.TestCase):
    def test_high_risk_requires_approval(self) -> None:
        engine = PolicyEngine.risk_gate()
        d = engine.evaluate(act(risk=RiskLevel.HIGH))
        self.assertIs(d.verdict, Verdict.REQUIRE_APPROVAL)
        self.assertIn("risk-gate", d.rule)

    def test_low_risk_is_allowed(self) -> None:
        self.assertIs(
            PolicyEngine.risk_gate().evaluate(act(risk=RiskLevel.LOW)).verdict,
            Verdict.ALLOW,
        )

    def test_human_approval_action_always_gated(self) -> None:
        """HUMAN_APPROVAL / ASK_USER 天然要过人 —— 哪怕 risk_level 是 LOW。"""
        engine = PolicyEngine.risk_gate()
        for t in (ActionType.HUMAN_APPROVAL, ActionType.ASK_USER):
            a = act(t, timeout=timedelta(minutes=5))
            self.assertIs(engine.evaluate(a).verdict, Verdict.REQUIRE_APPROVAL)

    def test_deny_rule_wins_over_risk_gate(self) -> None:
        engine = PolicyEngine.risk_gate(
            extra_rules=(
                PolicyRule(
                    name="no-delete",
                    verdict=Verdict.DENY,
                    reason="delete is forbidden",
                    tools=frozenset({"rm_rf"}),
                    priority=200,
                ),
            )
        )
        a = act(tool="rm_rf", risk=RiskLevel.HIGH)
        d = engine.evaluate(a)
        self.assertIs(d.verdict, Verdict.DENY)
        self.assertEqual(d.rule, "no-delete")

    def test_tenant_scoping(self) -> None:
        engine = PolicyEngine(
            rules=(
                PolicyRule(
                    name="tenant-a-strict",
                    verdict=Verdict.DENY,
                    reason="tenant a may not call tools",
                    tenants=frozenset({"tenant-a"}),
                ),
            )
        )
        self.assertIs(
            engine.evaluate(act(), PolicyContext(run_id=RUN, tenant_id="tenant-a")).verdict,
            Verdict.DENY,
        )
        self.assertIs(
            engine.evaluate(act(), PolicyContext(run_id=RUN, tenant_id="tenant-b")).verdict,
            Verdict.ALLOW,
        )

    def test_h1_deny_without_reason_is_rejected(self) -> None:
        """规则没写 reason 时，**产出决策**这一步就该炸 —— 不能默默拒绝。

        放在 evaluate() 而不是构造期校验，是因为规则是配置数据（可能来自远端），
        但"一次拒绝必须能说出为什么"是审计底线，不能等落到日志里才发现是空的。
        """
        engine = PolicyEngine(rules=(PolicyRule(name="x", verdict=Verdict.DENY),))
        with self.assertRaises(InvariantViolation):
            engine.evaluate(act())
        engine2 = PolicyEngine(
            rules=(PolicyRule(name="y", verdict=Verdict.REQUIRE_APPROVAL),)
        )
        with self.assertRaises(InvariantViolation):
            engine2.evaluate(act())

    def test_h3_confidence_is_not_an_input(self) -> None:
        """I-9 的类型级落地：PolicyContext 里根本没有 confidence 这个字段。

        不是"建议你别用"，是"你传不进来"。模型自评 0.99 也换不到自动放行。
        """
        names = {f.name for f in fields(PolicyContext)}
        self.assertNotIn("confidence", names)
        self.assertNotIn("confidence_signal", names)
        with self.assertRaises(TypeError):
            PolicyContext(run_id=RUN, confidence_signal=0.99)  # type: ignore[call-arg]


# ============================================================ Guardrail


class GuardrailTest(unittest.TestCase):
    def test_tool_not_in_allowlist_is_blocked(self) -> None:
        engine = GuardrailEngine(
            guardrails=(ToolAllowlistGuardrail(allowed=frozenset({"calculator"})),)
        )
        v = engine.check(GuardrailStage.TOOL, act(tool="rm_rf"))
        self.assertTrue(v.blocked)
        self.assertFalse(v.needs_review)

    def test_tool_in_allowlist_passes(self) -> None:
        engine = GuardrailEngine(
            guardrails=(ToolAllowlistGuardrail(allowed=frozenset({"calculator"})),)
        )
        self.assertTrue(engine.check(GuardrailStage.TOOL, act(tool="calculator")).passed)

    def test_secret_in_output_is_blocked(self) -> None:
        engine = GuardrailEngine(guardrails=(SecretPatternGuardrail(),))
        v = engine.check(GuardrailStage.OUTPUT, "here you go: sk-abcdef123")
        self.assertTrue(v.blocked)

    def test_sensitive_input_needs_review_not_block(self) -> None:
        """灰度护栏：不是"违规"，是"拿不准"，所以是 REVIEW 不是 BLOCK。"""
        engine = GuardrailEngine(
            guardrails=(SensitiveTopicGuardrail(keywords=("layoff",)),)
        )
        v = engine.check(GuardrailStage.INPUT, "draft a layoff notice")
        self.assertFalse(v.blocked)
        self.assertTrue(v.needs_review)

    def test_stage_isolation(self) -> None:
        """同一个护栏不该在别的阶段被触发 —— 输入护栏不能去审输出。"""
        engine = GuardrailEngine(
            guardrails=(SensitiveTopicGuardrail(keywords=("layoff",)),)
        )
        self.assertTrue(engine.check(GuardrailStage.INPUT, "layoff").needs_review)
        self.assertTrue(engine.check(GuardrailStage.OUTPUT, "layoff").passed)

    def test_block_beats_review(self) -> None:
        """H-7：同一阶段里既有 REVIEW 又有 BLOCK 时，BLOCK 说了算。

        安全底线不是权限问题 —— 不能让人"审批通过"一条已经违规的输出。
        """
        engine = GuardrailEngine(
            guardrails=(
                SensitiveTopicGuardrail(keywords=("layoff",), stage=GuardrailStage.OUTPUT),
                SecretPatternGuardrail(),
            )
        )
        v = engine.check(GuardrailStage.OUTPUT, "layoff list, key: sk-1")
        severities = {f.severity for f in v.findings}
        self.assertIn(GuardrailSeverity.REVIEW, severities)
        self.assertIn(GuardrailSeverity.BLOCK, severities)
        self.assertTrue(v.blocked)
        self.assertFalse(v.needs_review)      # BLOCK 在场，就不再谈 review


# ============================================================ Cost


class CostTest(unittest.TestCase):
    def test_budget_exhausted_denies(self) -> None:
        cost = CostManager(budget=Budget(max_cost=1.0))
        cost.charge(cost=2.0)
        self.assertTrue(cost.exceeded)
        self.assertIn("budget exhausted", cost.reason())

    def test_tokens_and_steps_also_counted(self) -> None:
        cost = CostManager(budget=Budget(max_tokens=10))
        cost.charge(tokens=11)
        self.assertTrue(cost.exceeded)
        cost2 = CostManager(budget=Budget(max_steps=2))
        cost2.charge(steps=3)
        self.assertTrue(cost2.exceeded)

    def test_negative_charge_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CostManager().charge(cost=-1)


# ============================================================ Approval


class ApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = HarnessClock()
        self.loop = HumanLoop(
            store=InMemoryApprovalStore(),
            clock=self.clock,
            default_ttl=timedelta(minutes=10),
        )

    def _request(self, **kw) -> ApprovalRequest:
        return self.loop.request(act(risk=RiskLevel.HIGH), reason="high risk", **kw)

    def test_h5_approval_must_expire(self) -> None:
        """没有截止时间的审批 = 允许一个人把 Run 永久挂住。"""
        with self.assertRaises(InvariantViolation):
            ApprovalRequest(
                run_id=RUN,
                action=act(),
                reason="x",
                requested_at=self.clock.now(),
                expires_at=None,
            )

    def test_approve_and_reject(self) -> None:
        req = self._request()
        self.assertTrue(req.is_pending)
        self.loop.approve(req.approval_id, by="alice")
        self.assertIs(req.status, ApprovalStatus.APPROVED)
        self.assertEqual(req.decided_by, "alice")

        req2 = self._request()
        self.loop.reject(req2.approval_id, by="bob", comment="too risky")
        self.assertIs(req2.status, ApprovalStatus.REJECTED)
        self.assertEqual(req2.comment, "too risky")

    def test_h6_terminal_approval_is_immutable(self) -> None:
        req = self._request()
        self.loop.approve(req.approval_id, by="alice")
        with self.assertRaises(InvariantViolation):
            self.loop.reject(req.approval_id, by="bob")

    def test_h8_expired_approval_cannot_be_decided(self) -> None:
        req = self._request(ttl=timedelta(minutes=1))
        self.clock.advance(timedelta(minutes=5))
        with self.assertRaises(InvariantViolation):
            self.loop.approve(req.approval_id, by="alice")

    def test_expire_due_moves_pending_to_expired(self) -> None:
        """**意图不会自己变成终态** —— 没人扫，过期审批会永远停在 PENDING。"""
        req = self._request(ttl=timedelta(minutes=1))
        self.assertEqual(self.loop.expire_due(), [])
        self.clock.advance(timedelta(minutes=5))
        self.assertEqual(self.loop.expire_due(), [req.approval_id])
        self.assertIs(req.status, ApprovalStatus.EXPIRED)
        # 扫过了就不会重复扫
        self.assertEqual(self.loop.expire_due(), [])

    def test_bind_execution_is_one_shot(self) -> None:
        req = self._request()
        self.loop.bind(req.approval_id, "exec_1")
        self.assertEqual(req.execution_id, "exec_1")
        with self.assertRaises(InvariantViolation):
            self.loop.bind(req.approval_id, "exec_2")


# ============================================================ Harness


class HarnessTest(unittest.TestCase):
    def test_h4_harness_has_no_kernel_reference(self) -> None:
        """**可执行的边界**：Harness 的字段里没有 kernel，它就没有能力反向调用。"""
        names = {f.name for f in fields(Harness)}
        self.assertNotIn("kernel", names)
        self.assertNotIn("repository", names)
        self.assertNotIn("worker", names)

    def test_h2_require_approval_carries_the_request(self) -> None:
        with self.assertRaises(InvariantViolation):
            HarnessVerdict(verdict=Verdict.REQUIRE_APPROVAL, approval=None)

    def test_h1_deny_carries_reasons(self) -> None:
        with self.assertRaises(InvariantViolation):
            HarnessVerdict(verdict=Verdict.DENY, reasons=())

    def test_allow_for_low_risk(self) -> None:
        h = Harness.default()
        self.assertTrue(h.before_action(act(risk=RiskLevel.LOW)).allowed)

    def test_guardrail_block_denies_even_low_risk(self) -> None:
        h = Harness.default(
            guardrails=GuardrailEngine(
                guardrails=(ToolAllowlistGuardrail(allowed=frozenset({"calculator"})),)
            )
        )
        v = h.before_action(act(tool="rm_rf", risk=RiskLevel.LOW))
        self.assertIs(v.verdict, Verdict.DENY)

    def test_guardrail_review_escalates_to_approval(self) -> None:
        """REVIEW 档的护栏把 ALLOW 顶成 REQUIRE_APPROVAL —— "拿不准"有处可去。

        这是三档设计存在的理由：只有 BLOCK / 放行两档的话，
        灰度护栏要么形同虚设、要么把业务卡死。
        """
        h = Harness.default(
            guardrails=GuardrailEngine(
                guardrails=(
                    ToolAllowlistGuardrail(allowed=frozenset({"calculator", "rm_rf"})),
                    SensitiveTopicGuardrail(keywords=("rm_rf",), stage=GuardrailStage.ACTION),
                )
            )
        )
        # 干净的动作：白名单放行、无敏感词 → ALLOW
        self.assertIs(h.before_action(act(tool="calculator")).verdict, Verdict.ALLOW)
        # 命中敏感词但没违规 → 不是 DENY，是"找个人看看"
        v = h.before_action(act(tool="rm_rf", risk=RiskLevel.LOW))
        self.assertIs(v.verdict, Verdict.REQUIRE_APPROVAL)
        self.assertIsNotNone(v.approval)

    def test_cost_exhausted_denies_before_policy(self) -> None:
        h = Harness.default(budget=Budget(max_cost=1.0))
        h.charge(cost=5.0)
        v = h.before_action(act(risk=RiskLevel.LOW))
        self.assertIs(v.verdict, Verdict.DENY)
        self.assertIn("cost", v.reasons[0])

    def test_check_input_and_output_are_reachable(self) -> None:
        h = Harness.default(
            guardrails=GuardrailEngine(guardrails=(SecretPatternGuardrail(),))
        )
        self.assertTrue(h.check_output("plain text").passed)
        self.assertTrue(h.check_output("sk-123").blocked)


# ============================================================ Loop × Harness


class LoopHarnessTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=self.clock,
        )
        registry = ToolRegistry()
        registry.register("calculator", calculator)
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors={"native": ToolCallExecutor(registry)},
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )

    def _loop(self, script: list[Action], harness: Harness, **cfg) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine(script),
            harness=harness,
            config=AgentLoopConfig(max_steps=6, **cfg),
        )

    def _action(self, *a, **kw) -> Action:
        return act(*a, **kw)


class DenyTest(LoopHarnessTestBase):
    def test_denied_action_produces_no_task(self) -> None:
        """基线 §2：DENY → 不产生 Task，直接回 Observation。"""
        harness = Harness.default(
            guardrails=GuardrailEngine(
                guardrails=(ToolAllowlistGuardrail(allowed=frozenset({"calculator"})),)
            )
        )
        loop = self._loop([], harness)
        state = loop.start("delete stuff")
        blocked = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "rm_rf", "args": {}},
            risk_level=RiskLevel.LOW,
        )
        loop.decision_engine = ScriptedDecisionEngine([blocked])

        self.assertIs(loop.step(), StepOutcome.DENIED)
        # 没有 Task → Kernel 里什么都没发生
        self.assertEqual(len(state.completed_tasks), 0)
        # 但 State 里必须留下"我被拒了"
        self.assertEqual(len(state.variables["denied_actions"]), 1)
        self.assertEqual(loop.steps, 0)

    def test_denial_is_recorded_with_reasons(self) -> None:
        harness = Harness.default(
            guardrails=GuardrailEngine(
                guardrails=(ToolAllowlistGuardrail(allowed=frozenset({"calculator"})),)
            )
        )
        loop = self._loop([], harness)
        state = loop.start("x")
        bad = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "rm_rf", "args": {}},
        )
        loop.decision_engine = ScriptedDecisionEngine([bad])
        loop.step()
        denied = state.variables["denied_actions"][0]
        self.assertEqual(denied["action_type"], "tool_call")
        self.assertTrue(denied["reasons"])


class ApprovalFlowTest(LoopHarnessTestBase):
    def _risky_loop(self, **cfg) -> AgentLoop:
        harness = Harness.default()
        loop = self._loop([], harness, **cfg)
        state = loop.start("do something risky")
        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = ScriptedDecisionEngine([risky])
        loop.state = state
        return loop

    def test_i9_high_confidence_does_not_buy_auto_execution(self) -> None:
        """ScriptedDecisionEngine 给的 confidence 是 0.95 —— 照样要过人。"""
        loop = self._risky_loop()
        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)

    def test_suspension_is_written_by_kernel_not_harness(self) -> None:
        """X-11：Harness 只请求，SUSPENDED 是 Kernel 写的。"""
        loop = self._risky_loop()
        loop.step()
        approval = loop.pending_approval
        assert approval is not None
        self.assertIsNotNone(approval.execution_id)

        execution = self.kernel.repository.get(approval.execution_id)
        assert execution is not None
        self.assertIs(execution.status, ExecutionStatus.SUSPENDED)
        self.assertIs(execution.suspension.reason, SuspensionReason.HUMAN_APPROVAL)
        self.assertEqual(
            execution.suspension.wait_condition["approval_id"], approval.approval_id
        )

    def test_gate_execution_released_its_lease(self) -> None:
        """E-9：进入 SUSPENDED 必须释放 Lease —— 否则 Lease 会白白过期再走 Recovery。"""
        loop = self._risky_loop()
        loop.step()
        assert loop.pending_approval is not None
        execution = self.kernel.repository.get(loop.pending_approval.execution_id)
        assert execution is not None
        self.assertIsNone(execution.lease)

    def test_approve_closes_gate_then_executes(self) -> None:
        loop = self._risky_loop()
        loop.step()
        gate_id = loop.pending_approval.execution_id

        self.assertIs(loop.approve(by="alice"), StepOutcome.EXECUTED)
        # 闸门走完了
        self.assertIs(self.kernel.status_of(gate_id), ExecutionStatus.COMPLETED)
        # 原始 Action 真的执行了
        self.assertEqual(len(loop.state.completed_tasks), 1)
        self.assertIsNone(loop.pending_approval)
        # 挂起标记已经从 State 里清掉
        self.assertNotIn("pending_approval_id", loop.state.variables)

    def test_reject_does_not_execute_but_is_recorded(self) -> None:
        """驳回是事实：不留 Observation，Agent 下次还会提同一个动作。"""
        loop = self._risky_loop()
        loop.step()
        self.assertIs(loop.reject(by="bob", comment="nope"), StepOutcome.DENIED)
        self.assertEqual(len(loop.state.completed_tasks), 0)
        self.assertIsNone(loop.pending_approval)
        # 状态里留下了驳回记录
        self.assertTrue(
            any(k.startswith("approval:") for k in loop.state.variables),
            "rejection must leave a trace in State",
        )

    def test_expired_approval_closes_the_run(self) -> None:
        hclock = HarnessClock()
        # 显式传入 harness 时，截止时间由 harness 自己说了算（见 AgentLoopConfig 的说明）
        harness = Harness.default(clock=hclock, approval_ttl=timedelta(minutes=1))
        loop = self._loop([], harness)
        state = loop.start("risky")
        risky = Action(
            run_id=state.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": "calculator", "args": {"expr": "6*7"}},
            risk_level=RiskLevel.HIGH,
        )
        loop.decision_engine = ScriptedDecisionEngine([risky])

        loop.step()
        approval = loop.pending_approval
        assert approval is not None
        # 时间推过截止时间
        hclock.advance(timedelta(minutes=5))
        expired = loop.expire_approvals()
        self.assertEqual(expired, [approval.approval_id])
        self.assertIsNone(loop.pending_approval)
        self.assertIn(StepOutcome.APPROVAL_EXPIRED, loop.history)

    def test_step_is_idempotent_while_waiting(self) -> None:
        loop = self._risky_loop()
        loop.step()
        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertIs(loop.step(), StepOutcome.WAITING_APPROVAL)

    def test_approve_without_pending_raises(self) -> None:
        loop = self._risky_loop()
        with self.assertRaises(InvariantViolation):
            loop.approve()


if __name__ == "__main__":
    unittest.main()
