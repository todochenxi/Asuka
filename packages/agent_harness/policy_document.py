"""Policy-as-Code：策略是一份**数据**，不是一个 Python 构造函数（基线 §23 / M9）。

### 空洞

此前策略只能由 Python 代码构造：

    PolicyEngine(rules=(PolicyRule(name=..., verdict=..., tools=...),))

改一条策略 = 改代码 + 重新部署。OPA 的做法是把策略变成**数据**，
由一个独立的决策点（PDP）加载并评估；执行点（PEP，这里是 `Harness.before_action`）
只问结果。这份模块补的就是"数据 → 决策点"那一段。

### 三条不变量

| # | 内容 |
|---|---|
| P-1 | **未知的 effect / 字段 / action_type 在加载期拒绝** —— 绝不静默忽略。一个拼错的 `effect: "audit"` 退化成默认放行，是策略里最危险的一类 bug |
| P-2 | 文档**必须显式声明 `default`** —— "什么都没匹配时怎么办"是策略的性质，不是可以省的默认值（fail-closed 不是默认值，是必须写下来的决定） |
| P-3 | 决策点与执行点分离：本模块只产出 `PolicyEngine`，不 import `Harness`、不产生副作用 |

### 为什么是严格加载而不是"尽量理解"

"尽量理解"看起来更友好，代价是**把配置错误翻译成了安全决策**：
`when.tool` 少写一个 `s`（正确是 `tools`），宽容的加载器会忽略这条约束，
于是"只允许这个工具"变成"所有工具都行"。宁可加载失败、让人看见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import ActionType, RiskLevel

from .policy import PolicyEngine, PolicyRule, Verdict

_TOP_KEYS = frozenset({"rules", "default"})
_RULE_KEYS = frozenset({"name", "effect", "reason", "priority", "when"})
_WHEN_KEYS = frozenset({"action_types", "tools", "tenants", "min_risk", "attributes"})


class PolicyDocumentError(InvariantViolation):
    """策略文档不合法。加载期抛出 —— 一份读不懂的策略不许被"尽量理解"。"""


def _verdict(value: Any, where: str) -> Verdict:
    try:
        return Verdict(value)
    except ValueError as exc:
        known = [v.value for v in Verdict]
        raise PolicyDocumentError(
            f"{where}: unknown verdict {value!r}; expected one of {known}"
        ) from exc


def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise PolicyDocumentError(f"{where}: expected a non-empty list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise PolicyDocumentError(f"{where}: every entry must be a non-empty string")
        out.append(item)
    return tuple(out)


def _unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PolicyDocumentError(
            f"{where}: unknown key(s) {unknown}; allowed keys are {sorted(allowed)}"
        )


@dataclass(frozen=True)
class PolicyDocument:
    """一份加载完成、已校验的策略文档。它**是**数据，不是可执行代码。"""

    rules: tuple[PolicyRule, ...]
    default_verdict: Verdict
    source: str = ""

    # ------------------------------------------------------------ 加载
    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], *, source: str = ""
    ) -> "PolicyDocument":
        if not isinstance(data, Mapping):
            raise PolicyDocumentError("policy document must be a mapping")
        _unknown_keys(data, _TOP_KEYS, "policy")

        # P-2：default 必须显式写下来
        if "default" not in data:
            raise PolicyDocumentError(
                "policy: 'default' is required; a policy must say what happens when "
                "no rule matches (an omitted default is how fail-closed silently "
                "becomes fail-open)"
            )
        default = _verdict(data["default"], "policy.default")

        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, (list, tuple)):
            raise PolicyDocumentError("policy.rules: expected a list")

        rules: list[PolicyRule] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_rules):
            rule = cls._rule(raw, where=f"policy.rules[{index}]")
            if rule.name in seen:
                raise PolicyDocumentError(
                    f"policy.rules[{index}]: duplicate rule name {rule.name!r}; "
                    f"duplicate names make the audit trail ambiguous"
                )
            seen.add(rule.name)
            rules.append(rule)

        return cls(rules=tuple(rules), default_verdict=default, source=source)

    @staticmethod
    def _rule(raw: Any, *, where: str) -> PolicyRule:
        if not isinstance(raw, Mapping):
            raise PolicyDocumentError(f"{where}: each rule must be a mapping")
        _unknown_keys(raw, _RULE_KEYS, where)

        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise PolicyDocumentError(f"{where}: 'name' is required")
        if "effect" not in raw:
            raise PolicyDocumentError(f"{where}: 'effect' is required")
        verdict = _verdict(raw["effect"], f"{where}.effect")

        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise PolicyDocumentError(f"{where}.reason: expected a string")
        # H-1 在加载期就兑现：非 ALLOW 的规则没有 reason 是一条读不懂的审计
        if verdict is not Verdict.ALLOW and not reason:
            raise PolicyDocumentError(
                f"{where}: effect {verdict.value!r} requires a non-empty 'reason' (H-1)"
            )

        priority = raw.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise PolicyDocumentError(f"{where}.priority: expected an integer")

        when = raw.get("when", {})
        if not isinstance(when, Mapping):
            raise PolicyDocumentError(f"{where}.when: expected a mapping")
        _unknown_keys(when, _WHEN_KEYS, f"{where}.when")

        return PolicyRule(
            name=name,
            verdict=verdict,
            reason=reason,
            action_types=PolicyDocument._action_types(when.get("action_types"), where),
            tools=PolicyDocument._tools(when.get("tools"), where),
            tenants=PolicyDocument._tenants(when.get("tenants"), where),
            min_risk=PolicyDocument._min_risk(when.get("min_risk"), where),
            attributes=PolicyDocument._attributes(when.get("attributes"), where),
            priority=priority,
        )

    @staticmethod
    def _action_types(value: Any, where: str) -> frozenset[ActionType] | None:
        if value is None:
            return None
        items = _string_list(value, f"{where}.when.action_types")
        out: list[ActionType] = []
        for item in items:
            try:
                out.append(ActionType(item))
            except ValueError as exc:
                known = [t.value for t in ActionType]
                raise PolicyDocumentError(
                    f"{where}.when.action_types: unknown action type {item!r}; "
                    f"expected one of {known}"
                ) from exc
        return frozenset(out)

    @staticmethod
    def _tools(value: Any, where: str) -> frozenset[str] | None:
        if value is None:
            return None
        return frozenset(_string_list(value, f"{where}.when.tools"))

    @staticmethod
    def _tenants(value: Any, where: str) -> frozenset[str] | None:
        if value is None:
            return None
        return frozenset(_string_list(value, f"{where}.when.tenants"))

    @staticmethod
    def _min_risk(value: Any, where: str) -> RiskLevel | None:
        if value is None:
            return None
        try:
            return RiskLevel(value)
        except ValueError as exc:
            known = [r.value for r in RiskLevel]
            raise PolicyDocumentError(
                f"{where}.when.min_risk: unknown risk level {value!r}; "
                f"expected one of {known}"
            ) from exc

    @staticmethod
    def _attributes(value: Any, where: str) -> Mapping[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or not value:
            raise PolicyDocumentError(
                f"{where}.when.attributes: expected a non-empty mapping"
            )
        return dict(value)

    # ------------------------------------------------------------ 决策点
    def compile(self) -> PolicyEngine:
        """产出决策点（PDP）。**只**产出 PolicyEngine —— 不碰 Harness（P-3）。"""
        return PolicyEngine(rules=self.rules, default_verdict=self.default_verdict)


__all__ = ["PolicyDocument", "PolicyDocumentError"]
