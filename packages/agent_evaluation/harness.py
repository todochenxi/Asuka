"""跑一次 Experiment —— **打真服务**，不 mock（M52 / M8）。

--------------------------------------------------------------------------
为什么这里不能有任何假

评估的意义全在"它是真的跑出来的"。一个用假回应喂出来的
"全部通过"比没有评估更坏 —— 它会在真正出事的时候给出一个绿色的句号。

--------------------------------------------------------------------------
为什么要自动过审批

demo 栈第 2 步会被治理层拦下等审批（这是**设计**，不是故障）。
评估必须能自己把这条走完，否则每条用例都停在 suspended，
而"停在等人"既不算通过也不算失败 —— 那是最难解释的一种结果。

所以遇到待审批就批准，并且**记下来**（`approvals` / `approved_by`）——
"有没有经过闸门"本身就是一条可断言的事实。
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from packages.agent_sdk import AgentOSClient

from . import Dataset, Facts, Verdict, evaluate


class EvaluationError(RuntimeError):
    """评估跑不下去（不是用例失败）—— 环境问题，要让人去修部署。"""


def _http(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any, str]:
    """打一次 HTTP。**实现在 `packages.agent_sdk`** —— 与 CLI 共用同一份。

    之前这里有一份自己的 urllib 封装（含"绕过系统代理"那段），
    与 `apps/cli` 里的那份几乎一模一样。那种知识只许有一处（B-7）：
    改一处忘一处，就会得到"CLI 修好了、评估还在误报"这种只对了一半的修复。

    这里只补一件事：连不上时抛 `EvaluationError`（批处理场景，
    连不上就是致命的，不该返回一个空结果让用例"通过"）。
    """
    # 历史签名收的是**完整 URL**，而 SDK 收的是 (base, path) —— 拆开给它
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    client = AgentOSClient(base=base, timeout=timeout)
    status, parsed, raw = client.request(
        method, path, body=body, headers=headers
    )
    if status == 0:
        raise EvaluationError(f"cannot reach {url}")
    return (status, parsed, raw)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _extract(trace: Mapping[str, Any], view: Mapping[str, Any]) -> Facts:
    """把账本 + Run 视图压成一组可断言的事实。**只搬，不推断。**"""
    actions: list[str] = []
    approvals = 0
    approved_by: list[str] = []
    tools: list[str] = []
    side_effects: list[str] = []
    degraded = False
    fallback = 0

    for entry in trace.get("entries") or ():
        kind = str(entry.get("kind") or "")
        payload = entry.get("payload") or {}
        served = entry.get("served") or {}
        if kind == "task.submitted":
            at = payload.get("action_type")
            if at:
                actions.append(str(at))
        elif kind == "approval.requested":
            approvals += 1
        elif kind == "approval.decided":
            who = payload.get("decided_by")
            if who:
                approved_by.append(str(who))
        if served.get("tool"):
            tools.append(str(served["tool"]))
        if served.get("side_effect"):
            side_effects.append(str(served["side_effect"]))
        if served.get("degraded"):
            degraded = True
        if served.get("fallback_count"):
            fallback = max(fallback, int(served["fallback_count"]))

    return Facts(
        run_id=str(view.get("run_id", "")),
        status=str(view.get("status", "")),
        outcome=str(view.get("last_outcome", "")),
        step_count=int(view.get("step_count", 0) or 0),
        actions=tuple(actions),
        approvals=approvals,
        approved_by=tuple(approved_by),
        tools=tuple(tools),
        side_effects=tuple(side_effects),
        degraded=degraded,
        fallback_count=fallback,
        trace=tuple(trace.get("entries") or ()),
    )


def run_case(
    base: str,
    agent_id: str,
    case: Any,
    *,
    approver: str = "evaluator",
    max_steps: int = 20,
) -> Verdict:
    """跑一条用例：发起 → 推进（自动过审批）→ 拉账本 → 判定。"""
    base = base.rstrip("/")

    status, view, raw = _http(
        "POST",
        f"{base}/agents/{_quote(agent_id)}/runs",
        body={"user_request": case.request},
    )
    if status >= 400 or not view:
        # 发起就失败 —— 这不是用例的问题，是环境问题
        raise EvaluationError(
            f"start failed for case {case.id}: HTTP {status} {raw[:160]}"
        )
    run_id = str(view["run_id"])

    limit = max(1, int(getattr(case.expect, "max_steps", max_steps) or max_steps))
    for _ in range(limit):
        if str(view.get("status")) in ("completed", "failed", "cancelled"):
            break
        gate = view.get("pending_approval")
        if gate:
            _http(
                "POST",
                f"{base}/runs/{_quote(run_id)}/approvals/"
                f"{_quote(str(gate['approval_id']))}/decision",
                body={"decision": "approve", "by": approver, "comment": "eval"},
            )
        status, view, _ = _http("POST", f"{base}/runs/{_quote(run_id)}/run", body={})
        if status >= 400:
            break

    _, trace, _ = _http("GET", f"{base}/runs/{_quote(run_id)}/trace")
    facts = _extract(trace or {}, view or {})

    # 跑完还没终态 = 卡住（挂起等人 / 推进不动）。这是单独一类归因。
    if facts.status not in ("completed", "failed", "cancelled"):
        return Verdict(
            case.id, False, "never_terminated",
            f"still {facts.status!r} after {limit} steps", facts,
        )
    return evaluate(case, facts)


def run_dataset(base: str, dataset: Dataset, *, approver: str = "evaluator") -> list[Verdict]:
    return [
        run_case(base, dataset.agent_id, case, approver=approver)
        for case in dataset.cases
    ]
