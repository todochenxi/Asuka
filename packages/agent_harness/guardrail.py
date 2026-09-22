"""Guardrail：回答「这个输入 / Action / 输出是否满足安全约束」（基线 §23）。

四个阶段：

    INPUT    用户输入进 Loop 之前
    ACTION   Action 变成 Task 之前
    TOOL     具体某个工具被调之前
    OUTPUT   结果写回 / 发给用户之前

三档严重程度（这是本文件最关键的设计）：

    BLOCK    硬性违反 → DENY
    REVIEW   拿不准，需要人确认 → REQUIRE_APPROVAL
    WARN     记录但不阻断 → ALLOW

> **为什么必须有 REVIEW 而不是只有 BLOCK / 放行？**
> 真实护栏大量是"灰度"的（疑似敏感词、超长输出、越权但可能合理）。
> 只有 BLOCK 会让护栏要么形同虚设、要么卡死业务；
> 有了 REVIEW，"不确定"就有了一个**有据可查的去处**，而不是被迫二选一。

> **BLOCK 不能靠人工审批绕过**（H-7）：
> 安全约束的底线不是权限问题，审批放行的是"人"，不是"规则"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class GuardrailStage(str, Enum):
    INPUT = "input"
    ACTION = "action"
    TOOL = "tool"
    OUTPUT = "output"


class GuardrailSeverity(str, Enum):
    WARN = "warn"
    REVIEW = "review"
    BLOCK = "block"


@dataclass(frozen=True)
class GuardrailFinding:
    guardrail: str
    stage: GuardrailStage
    severity: GuardrailSeverity
    message: str


@dataclass(frozen=True)
class GuardrailVerdict:
    findings: tuple[GuardrailFinding, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(f.severity is GuardrailSeverity.BLOCK for f in self.findings)

    @property
    def needs_review(self) -> bool:
        # H-7：已经 BLOCK 了就不再谈 review，直接判死
        return (not self.blocked) and any(
            f.severity is GuardrailSeverity.REVIEW for f in self.findings
        )

    @property
    def passed(self) -> bool:
        return not self.blocked and not self.needs_review

    @property
    def warnings(self) -> tuple[GuardrailFinding, ...]:
        return tuple(f for f in self.findings if f.severity is GuardrailSeverity.WARN)


class Guardrail(Protocol):
    """一条护栏。返回 `None` 表示"我没意见"。"""

    name: str
    stage: GuardrailStage

    def check(self, target: Any, *, context: Mapping[str, Any]) -> GuardrailFinding | None: ...


@dataclass
class GuardrailEngine:
    guardrails: tuple[Guardrail, ...] = ()

    def check(
        self,
        stage: GuardrailStage,
        target: Any,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> GuardrailVerdict:
        context = dict(context or {})
        findings: list[GuardrailFinding] = []
        for g in self.guardrails:
            if g.stage is not stage:
                continue
            finding = g.check(target, context=context)
            if finding is not None:
                findings.append(finding)
        return GuardrailVerdict(findings=tuple(findings))

    def for_stage(self, stage: GuardrailStage) -> tuple[Guardrail, ...]:
        return tuple(g for g in self.guardrails if g.stage is stage)


# ---------------------------------------------------------------- 内置护栏


@dataclass(frozen=True)
class ToolAllowlistGuardrail:
    """TOOL 阶段：工具白名单。不在名单上一律 BLOCK。"""

    allowed: frozenset[str]
    name: str = "tool-allowlist"
    stage: GuardrailStage = GuardrailStage.TOOL

    def check(self, target: Any, *, context: Mapping[str, Any]) -> GuardrailFinding | None:
        tool = None
        if isinstance(target, Mapping):
            tool = target.get("tool")
        if tool is None:
            tool = getattr(target, "payload", {}).get("tool") if hasattr(target, "payload") else None
        if tool is None:
            return None                                  # 不是工具调用，不归我管
        if tool in self.allowed:
            return None
        return GuardrailFinding(
            guardrail=self.name,
            stage=self.stage,
            severity=GuardrailSeverity.BLOCK,
            message=f"tool '{tool}' is not in the allowlist",
        )


@dataclass(frozen=True)
class SecretPatternGuardrail:
    """OUTPUT 阶段：疑似密钥泄漏。

    BLOCK 而不是 REVIEW —— 密钥一旦发出去，人工审批也收不回来。
    """

    patterns: tuple[str, ...] = ("sk-", "BEGIN RSA PRIVATE KEY", "ghp_", "AKIA")
    name: str = "secret-pattern"
    stage: GuardrailStage = GuardrailStage.OUTPUT

    def check(self, target: Any, *, context: Mapping[str, Any]) -> GuardrailFinding | None:
        text = target if isinstance(target, str) else str(target)
        for p in self.patterns:
            if p in text:
                return GuardrailFinding(
                    guardrail=self.name,
                    stage=self.stage,
                    severity=GuardrailSeverity.BLOCK,
                    message=f"output looks like it contains a secret (matched '{p}')",
                )
        return None


@dataclass(frozen=True)
class SensitiveTopicGuardrail:
    """INPUT 阶段：命中敏感词 → 需要人确认（REVIEW，不是 BLOCK）。"""

    keywords: tuple[str, ...] = ()
    name: str = "sensitive-topic"
    stage: GuardrailStage = GuardrailStage.INPUT

    def check(self, target: Any, *, context: Mapping[str, Any]) -> GuardrailFinding | None:
        text = target if isinstance(target, str) else str(target)
        hit = [k for k in self.keywords if k in text]
        if not hit:
            return None
        return GuardrailFinding(
            guardrail=self.name,
            stage=self.stage,
            severity=GuardrailSeverity.REVIEW,
            message=f"sensitive keywords present: {', '.join(hit)}",
        )
