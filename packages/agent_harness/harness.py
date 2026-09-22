"""Harness —— AgentLoop 唯一允许调用的 Harness 入口（基线 §2 的 hook point）。

```text
Agent Loop
    ↓
Action
    ↓
Harness.Policy / Guardrail      ← 唯一拦截点，由 Runtime 主动调用
    ↓
ALLOW             → Runtime.TaskFactory → Task → Kernel
REQUIRE_APPROVAL  → 请求挂起 → Kernel 写 SUSPENDED
DENY              → 不产生 Task，直接回 Observation
```

**H-4（可执行的边界）**：

> `Harness` 的字段里**没有** kernel。

这不是一句约定 —— `test_harness_has_no_kernel_reference` 会断言
`"kernel" not in Harness.__dataclass_fields__`。
Harness 想挂起一个 Action，只能交出 `ApprovalRequest` 让 Loop 去办，
它在代码上就没有能力反向调用 Kernel。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel

from .approval import (
    ApprovalRequest,
    ApprovalStore,
    HumanLoop,
    InMemoryApprovalStore,
)
from .cost import Budget, CostManager
from .guardrail import GuardrailEngine, GuardrailFinding, GuardrailSeverity, GuardrailStage, GuardrailVerdict
from .policy import _RISK_ORDER, PolicyContext, PolicyEngine, Verdict
from .ports import Clock, SystemClock


@dataclass(frozen=True)
class HarnessVerdict:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    approval: ApprovalRequest | None = None
    findings: tuple[GuardrailFinding, ...] = ()

    def __post_init__(self) -> None:
        # H-2：REQUIRE_APPROVAL 必须带着那条审批请求
        # —— 否则 Loop 拿到了"要审批"却不知道审批谁，只能自己临时造一个，
        #    审批记录就和 Kernel 里的 SUSPENDED 对不上了。
        if self.verdict is Verdict.REQUIRE_APPROVAL and self.approval is None:
            raise InvariantViolation(
                "H-2: REQUIRE_APPROVAL must carry an ApprovalRequest"
            )
        if self.verdict is Verdict.DENY and not self.reasons:
            raise InvariantViolation("H-1: DENY must carry at least one reason")

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass
class Harness:
    policy: PolicyEngine = field(default_factory=PolicyEngine.risk_gate)
    guardrails: GuardrailEngine = field(default_factory=GuardrailEngine)
    cost: CostManager = field(default_factory=CostManager)
    approvals: HumanLoop = field(default_factory=HumanLoop)
    clock: Clock = field(default_factory=SystemClock)

    # ------------------------------------------------------------ Action 拦截点
    def before_action(
        self,
        action: Action,
        *,
        context: PolicyContext | None = None,
    ) -> HarnessVerdict:
        """Runtime 在把 Action 变成 Task 之前**必须**先过这一关。"""
        context = context or PolicyContext(run_id=action.run_id)
        reasons: list[str] = []
        findings: list[GuardrailFinding] = []

        # 1) 预算 —— 超了就终止，不给人"续杯"的口子（见 cost.py 的理由）
        if self.cost.exceeded:
            return HarnessVerdict(
                verdict=Verdict.DENY,
                reasons=(f"cost: {self.cost.reason()}",),
            )

        # 2) Policy —— 权限问题
        decision = self.policy.evaluate(action, context)
        if decision.verdict is Verdict.DENY:
            return HarnessVerdict(
                verdict=Verdict.DENY,
                reasons=(f"policy[{decision.rule}]: {decision.reason}",),
            )
        needs_human = decision.verdict is Verdict.REQUIRE_APPROVAL
        if needs_human:
            reasons.append(f"policy[{decision.rule}]: {decision.reason}")

        # 2.5) S-9：逆操作**跟着正向动作一起审**。
        #
        # "批准一个动作 = 同时批准它的撤销"这句话，如果只写在注释里，
        # 就是一条断言而不是事实（同 A-10 的教训）：
        # 补偿阶段是**绕过** `before_action` 的（见 loop._execute_compensation），
        # 因为撤销时再等人批准会让副作用永久留着。
        # 既然撤销时不再审一次，那么**这里**就必须把它审掉 ——
        # 否则"撤销"是一条完全没经过策略的动作。
        #
        # 所以：声明了补偿、但补偿工具本身被策略拒绝 → 整个动作都不许做。
        # 一个不能撤销的高风险动作，不该被放出去。
        if action.compensation is not None:
            undo = self._compensation_action(action)
            undo_decision = self.policy.evaluate(undo, context)
            if undo_decision.verdict is Verdict.DENY:
                return HarnessVerdict(
                    verdict=Verdict.DENY,
                    reasons=(
                        "S-9: the declared compensation is not permitted "
                        f"(policy[{undo_decision.rule}]: {undo_decision.reason}); "
                        "an action whose inverse is forbidden must not be executed",
                    ),
                )
            if undo_decision.verdict is Verdict.REQUIRE_APPROVAL:
                # 逆操作需要人批 —— 那就连正向一起等批（人的批准同时覆盖两者）
                needs_human = True
                reasons.append(f"S-9 compensation[{undo_decision.rule}]: {undo_decision.reason}")

        # 3) Guardrail —— 安全约束（ACTION + TOOL 两个阶段）
        for stage in (GuardrailStage.ACTION, GuardrailStage.TOOL):
            verdict = self.guardrails.check(stage, action, context=context.attributes)
            findings.extend(verdict.findings)
            if verdict.blocked:
                # H-7：BLOCK 不给人审批绕过的机会
                block = next(f for f in verdict.findings if f.severity is GuardrailSeverity.BLOCK)
                return HarnessVerdict(
                    verdict=Verdict.DENY,
                    reasons=(f"guardrail[{block.guardrail}]: {block.message}",),
                    findings=tuple(findings),
                )
            if verdict.needs_review:
                needs_human = True
                review = next(f for f in verdict.findings if f.severity is GuardrailSeverity.REVIEW)
                reasons.append(f"guardrail[{review.guardrail}]: {review.message}")

        if needs_human:
            request = self.approvals.request(action, reason="; ".join(reasons))
            return HarnessVerdict(
                verdict=Verdict.REQUIRE_APPROVAL,
                reasons=tuple(reasons),
                approval=request,
                findings=tuple(findings),
            )

        return HarnessVerdict(verdict=Verdict.ALLOW, findings=tuple(findings))

    # ------------------------------------------------------------ 补偿（S-9）
    @staticmethod
    def _compensation_action(action: Action) -> Action:
        """把 `action.compensation` 变成一个可被 Policy 评估的 Action。

        只用于**问一句"这个撤销工具允许用吗"**，不产生 Task、不进入执行。
        `risk_level` 继承正向动作：撤销一个高风险动作，本身就是高风险动作。
        """
        spec = action.compensation
        assert spec is not None
        return Action(
            run_id=action.run_id,
            action_type=ActionType.TOOL_CALL,
            payload={"tool": spec.tool, "args": dict(spec.args)},
            risk_level=action.risk_level,
            rationale=f"compensation of {action.action_type.value}: {spec.description}",
        )

    # ------------------------------------------------------------ 输入 / 输出
    def check_input(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> GuardrailVerdict:
        return self.guardrails.check(
            GuardrailStage.INPUT, text, context=dict(context or {})
        )

    def check_output(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> GuardrailVerdict:
        return self.guardrails.check(
            GuardrailStage.OUTPUT, text, context=dict(context or {})
        )

    # ------------------------------------------------------------ 计价
    def charge(self, *, cost: float = 0.0, tokens: int = 0, steps: int = 0) -> None:
        self.cost.charge(cost=cost, tokens=tokens, steps=steps)

    # ------------------------------------------------------------ 便捷构造
    @classmethod
    def default(
        cls,
        *,
        require_approval_for: frozenset[RiskLevel] = frozenset({RiskLevel.HIGH}),
        budget: Budget | None = None,
        guardrails: GuardrailEngine | None = None,
        approval_ttl: timedelta = timedelta(minutes=30),
        clock: Clock | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> "Harness":
        """一个"开箱即用但不含糊"的 Harness。

        默认只包含三条规则，且每条都能追溯到基线：
          · HUMAN_APPROVAL / ASK_USER 天然要过人（ActionType 语义）
          · risk_level >= HIGH 要过人（I-9：confidence 换不来自动放行）
          · 预算耗尽 → DENY
        """
        clock = clock or SystemClock()
        if require_approval_for:
            # 取集合里**最低**的那一档作门槛：{HIGH, MEDIUM} 应当连 MEDIUM 一起拦
            min_risk = min(require_approval_for, key=lambda r: _RISK_ORDER[r])
        else:
            min_risk = RiskLevel.HIGH          # 没配置就至少拦 HIGH
        return cls(
            policy=PolicyEngine.risk_gate(min_risk=min_risk),
            guardrails=guardrails or GuardrailEngine(),
            cost=CostManager(budget=budget or Budget()),
            approvals=HumanLoop(
                # A-10：不传就退化为进程内存储；生产必须传 PG 版，
                # 否则服务一重启，待批列表就空了（而 Run 还挂着等人批）。
                store=approval_store or InMemoryApprovalStore(),
                clock=clock,
                default_ttl=approval_ttl,
            ),
            clock=clock,
        )


def action_is_human_gated(action: Action) -> bool:
    """这个 Action 是否天生需要人（不依赖任何策略配置）。"""
    return action.action_type in (ActionType.HUMAN_APPROVAL, ActionType.ASK_USER)
