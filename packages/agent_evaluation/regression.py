"""回归对比（M52 / M8）。

--------------------------------------------------------------------------
为什么"和上一次比"比"绝对通过率"更有用

一条用例这次通过了，说明不了什么 —— 它可能一直都通过。
真正要报警的是**它上次通过、这次不通过了**。

所以这里比的是**同一条用例的前后两次**：

    regressed   上次通过 → 这次不通过     ← 唯一必须报警的一类
    improved    上次不通过 → 这次通过
    unchanged   都一样（含"两次都不通过"，那是**已知问题**，不是新退步）

刻意把"两次都不通过"归为 unchanged 而不是 failed：
一条一直失败的用例是** backlog**，不是回归；
把它报成回归会让回归信号淹没在噪声里 ——
那正是"每轮 3 条红、其实 0 条新问题"这种疲惫感的来源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import Verdict


@dataclass(frozen=True)
class Delta:
    case_id: str
    kind: str                 # regressed / improved / unchanged / new
    before: bool | None = None
    after: bool = False
    attribution: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "attribution": self.attribution,
        }


def compare(before: Mapping[str, bool], after: Sequence[Verdict]) -> list[Delta]:
    """把上一轮的结果（case_id → passed）与这一轮比一比。"""
    out: list[Delta] = []
    for verdict in after:
        prev = before.get(verdict.case_id)
        if prev is None:
            out.append(Delta(verdict.case_id, "new", None, verdict.passed,
                             verdict.attribution))
        elif prev and not verdict.passed:
            out.append(Delta(verdict.case_id, "regressed", True, False,
                             verdict.attribution))
        elif not prev and verdict.passed:
            out.append(Delta(verdict.case_id, "improved", False, True))
        else:
            out.append(Delta(verdict.case_id, "unchanged", prev, verdict.passed,
                             verdict.attribution))
    return out


def regressions(deltas: Sequence[Delta]) -> list[Delta]:
    return [d for d in deltas if d.kind == "regressed"]


def summarize(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    passed = [v for v in verdicts if v.passed]
    failed = [v for v in verdicts if not v.passed]
    by_attr: dict[str, int] = {}
    for v in failed:
        by_attr[v.attribution] = by_attr.get(v.attribution, 0) + 1
    return {
        "total": len(verdicts),
        "passed": len(passed),
        "failed": len(failed),
        "by_attribution": by_attr,
    }
