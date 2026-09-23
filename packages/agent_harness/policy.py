"""Policy：回答「这个 Agent / User 是否有权执行这个 Action」（基线 §23）。

只回答三件事：

    ALLOW            放行
    DENY             拒绝（不产生 Task）
    REQUIRE_APPROVAL 需要人来放行

**H-3（= I-9 在 Harness 侧的落地）**：

> `confidence_signal` 永远不能作为放行依据。

所以 `PolicyContext` 里**根本没有 confidence 字段**。
这不是"建议你别用"，而是"你传不进来" —— 把 I-9 从一句约定变成类型系统的约束。
模型自评 0.99 也换不到一次自动放行，因为这条路在类型上就不存在。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel

_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyContext:
    """Policy 做判断时能看到的全部信息。

    注意这里**没有** confidence —— 这是 H-3 的实现方式，不是遗漏。
    """

    run_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    step_no: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True)
class PolicyRule:
    """一条策略规则。字段为 `None` 表示"不限制这个维度"。"""

    name: str
    verdict: Verdict = Verdict.ALLOW
    reason: str = ""
    action_types: frozenset[ActionType] | None = None
    tools: frozenset[str] | None = None
    tenants: frozenset[str] | None = None
    min_risk: RiskLevel | None = None
    #: M101：按 `PolicyContext.attributes` 精确匹配（策略即数据要能表达"环境"）。
    #: 全部键都要相等才算命中；缺一个键就是不匹配。
    attributes: Mapping[str, Any] | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise InvariantViolation("PolicyRule.name is required")
        if self.attributes is not None:
            object.__setattr__(self, "attributes", dict(self.attributes))

    def matches(self, action: Action, ctx: PolicyContext) -> bool:
        if self.action_types is not None and action.action_type not in self.action_types:
            return False
        if self.min_risk is not None and _RISK_ORDER[action.risk_level] < _RISK_ORDER[self.min_risk]:
            return False
        if self.tools is not None:
            tool = action.payload.get("tool")
            if tool not in self.tools:
                return False
        if self.tenants is not None and ctx.tenant_id not in self.tenants:
            return False
        if self.attributes is not None:
            for key, expected in self.attributes.items():
                if ctx.attributes.get(key) != expected:
                    return False
        return True


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    rule: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        # H-1：拒绝与挂起必须带 reason —— 否则审计只能看到"被拦了"，看不到"为什么"
        if self.verdict is not Verdict.ALLOW and not self.reason:
            raise InvariantViolation(
                "H-1: DENY / REQUIRE_APPROVAL must carry a reason (audit trail)"
            )


@dataclass
class PolicyEngine:
    rules: tuple[PolicyRule, ...] = ()
    default_verdict: Verdict = Verdict.ALLOW

    def evaluate(self, action: Action, ctx: PolicyContext | None = None) -> PolicyDecision:
        ctx = ctx or PolicyContext(run_id=action.run_id)
        # priority 高者优先；同优先级按声明顺序
        for rule in sorted(self.rules, key=lambda r: -r.priority):
            if rule.matches(action, ctx):
                return PolicyDecision(verdict=rule.verdict, rule=rule.name, reason=rule.reason)
        # M101 抓到的真 bug：`default_verdict=DENY` 以前会**当场抛 H-1**
        # （`PolicyDecision` 要求非 ALLOW 必须带 reason，而这里给了空串）。
        # 后果不是"默认拒绝不生效"，而是"引擎一走到默认分支就崩" ——
        # 一份 `default: deny` 的策略根本表达不出来。默认拒绝的 reason 就是
        # "没有任何规则命中"，把它写出来既满足 H-1，也让审计读得到原因。
        if self.default_verdict is Verdict.ALLOW:
            return PolicyDecision(verdict=Verdict.ALLOW, rule="default", reason="")
        return PolicyDecision(
            verdict=self.default_verdict,
            rule="default",
            reason="no rule matched; the policy's explicit default verdict applies",
        )

    # ------------------------------------------------------------ 便捷构造
    @classmethod
    def risk_gate(
        cls,
        *,
        min_risk: RiskLevel = RiskLevel.HIGH,
        extra_rules: tuple[PolicyRule, ...] = (),
    ) -> "PolicyEngine":
        """默认策略：高风险动作必须过人；AskUser / HumanApproval 天然要过人。"""
        rules: list[PolicyRule] = [
            PolicyRule(
                name="human-in-the-loop-actions",
                verdict=Verdict.REQUIRE_APPROVAL,
                reason="action requires a human by nature",
                action_types=frozenset({ActionType.HUMAN_APPROVAL, ActionType.ASK_USER}),
                priority=100,
            ),
            PolicyRule(
                name="risk-gate",
                verdict=Verdict.REQUIRE_APPROVAL,
                reason=f"risk_level >= {min_risk.value} requires human approval",
                min_risk=min_risk,
                priority=10,
            ),
        ]
        rules.extend(extra_rules)
        return cls(rules=tuple(rules))
