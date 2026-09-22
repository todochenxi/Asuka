"""评估平台（M52 / M8）。

--------------------------------------------------------------------------
它评什么，不评什么

它评**行为轨迹**，不评"答案对不对"。

一次 Run 在账本里留下的可断言事实是：

    终态 / outcome / step_count        run.finished
    走过的动作                          task.submitted.action_type
    有没有经过审批、谁批的               approval.requested / decided
    调了什么工具、什么副作用             served.tool / side_effect
    模型有没有降级                       served.degraded / fallback_count

账本里**没有**答案文本（demo 的 `llm_call` 结果不进 trace）。
所以这里断言的是"它怎么走到终态的"，不是"它说对了没有"。

对**回归**而言这够了，而且更稳：答案文本会随模型波动，
而"两步完成、经过审批、调了 note.write"这条路径不该随便变。

--------------------------------------------------------------------------
为什么必须打真服务

评估结果只有在**真起 Run、真推进、真查账本**时才成立。
一个用假回应喂出来的"全部通过"比没有评估更坏 ——
它会在真正出事的时候给出一个绿色的句号。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------- 事实


@dataclass(frozen=True)
class Facts:
    """一次 Run 跑完之后的**事实**（全部来自服务，不推断）。"""

    run_id: str
    status: str
    outcome: str
    step_count: int
    actions: tuple[str, ...] = ()
    approvals: int = 0
    approved_by: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    degraded: bool = False
    fallback_count: int = 0
    trace: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "outcome": self.outcome,
            "step_count": self.step_count,
            "actions": list(self.actions),
            "approvals": self.approvals,
            "approved_by": list(self.approved_by),
            "tools": list(self.tools),
            "side_effects": list(self.side_effects),
            "degraded": self.degraded,
            "fallback_count": self.fallback_count,
        }


# ---------------------------------------------------------------- 断言


@dataclass(frozen=True)
class Expect:
    """一条用例期望什么。字段全空 = 什么都不要求（只要求能跑到终态）。"""

    status: str = ""                      # 期望终态：completed / cancelled / ...
    outcome: str = ""
    approval_required: bool | None = None  # True=必须经过审批；False=必须不经过
    actions: tuple[str, ...] = ()          # 期望走过的动作（按出现顺序的子序列）
    tools: tuple[str, ...] = ()            # 期望调过的工具
    max_steps: int = 20                    # 超过就算"没跑完"
    not_degraded: bool = False             # True=模型不许降级

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Expect":
        return cls(
            status=str(d.get("status", "")),
            outcome=str(d.get("outcome", "")),
            approval_required=d.get("approval_required"),
            actions=tuple(d.get("actions", ()) or ()),
            tools=tuple(d.get("tools", ()) or ()),
            max_steps=int(d.get("max_steps", 20)),
            not_degraded=bool(d.get("not_degraded", False)),
        )


@dataclass(frozen=True)
class Case:
    id: str
    request: str
    expect: Expect = field(default_factory=Expect)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Case":
        return cls(
            id=str(d.get("id", "")),
            request=str(d.get("request", "")),
            expect=Expect.from_dict(d.get("expect", {}) or {}),
        )


@dataclass(frozen=True)
class Dataset:
    name: str
    agent_id: str
    cases: tuple[Case, ...]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Dataset":
        cases = tuple(Case.from_dict(c) for c in (d.get("cases") or ()))
        if not cases:
            raise ValueError("EVAL_DATASET_EMPTY: a dataset needs at least one case")
        return cls(
            name=str(d.get("name", "unnamed")),
            agent_id=str(d.get("agent_id", "")),
            cases=cases,
        )

    @classmethod
    def load(cls, path: str) -> "Dataset":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------- 归因

#: 失败归因。刻意用**机制**命名而不是用"错误"命名 ——
#: 它们回答的是"卡在哪一步"，好让人直接知道该去看哪里。
ATTR_START_FAILED = "start_failed"            # 发起就失败
ATTR_NEVER_TERMINATED = "never_terminated"     # 推到上限还没终态（卡住/挂起等人）
ATTR_STATUS = "unexpected_status"              # 终态不是期望的
ATTR_OUTCOME = "unexpected_outcome"
ATTR_APPROVAL = "approval_mismatch"            # 该经过审批的没经过 / 反之
ATTR_ACTION = "missing_action"                 # 期望的动作没走
ATTR_TOOL = "missing_tool"                     # 期望的工具没调
ATTR_DEGRADED = "model_degraded"               # 模型降级了（最难发现的一类）


@dataclass(frozen=True)
class Verdict:
    case_id: str
    passed: bool
    #: 失败归因（通过时为空字符串）
    attribution: str = ""
    detail: str = ""
    facts: Facts | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "attribution": self.attribution,
            "detail": self.detail,
            "facts": self.facts.as_dict() if self.facts else None,
        }


def evaluate(case: Case, facts: Facts) -> Verdict:
    """按期望判定，失败时给出**归因**（不只是"不通过"）。"""
    ex = case.expect

    if ex.status and facts.status != ex.status:
        return Verdict(case.id, False, ATTR_STATUS,
                       f"status: want {ex.status}, got {facts.status}", facts)
    if ex.outcome and facts.outcome != ex.outcome:
        return Verdict(case.id, False, ATTR_OUTCOME,
                       f"outcome: want {ex.outcome}, got {facts.outcome}", facts)
    if ex.approval_required is True and facts.approvals == 0:
        return Verdict(case.id, False, ATTR_APPROVAL,
                       "expected the run to be gated, but no approval happened", facts)
    if ex.approval_required is False and facts.approvals > 0:
        return Verdict(case.id, False, ATTR_APPROVAL,
                       f"expected no gate, but {facts.approvals} approval(s) happened", facts)

    for action in ex.actions:
        if action not in facts.actions:
            return Verdict(case.id, False, ATTR_ACTION,
                           f"missing action {action}; got {list(facts.actions)}", facts)
    for tool in ex.tools:
        if tool not in facts.tools:
            return Verdict(case.id, False, ATTR_TOOL,
                           f"missing tool {tool}; got {list(facts.tools)}", facts)
    if ex.not_degraded and (facts.degraded or facts.fallback_count > 0):
        return Verdict(case.id, False, ATTR_DEGRADED,
                       f"model degraded (degraded={facts.degraded}, "
                       f"fallback={facts.fallback_count})", facts)

    return Verdict(case.id, True, facts=facts)
