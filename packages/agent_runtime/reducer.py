"""Runtime 的默认 StateReducer：把 Observation 解释成 State 变更。

X-7  Observation 必须经 Reducer 才能更新 State
I-3  State 只能通过 apply(observation, reducer) 变更

注意 **Plan 也是以 Observation 的形式进入 State** 的（kind=`plan.created`）。
这不是绕路：如果允许 Loop 直接 `state.current_plan = plan`，
I-3 就开了个口子，Replay 时 Plan 的来龙去脉也断了。
"""
from __future__ import annotations

from packages.agent_domain.intelligence.observation import Observation
from packages.agent_domain.intelligence.plan import Plan
from packages.agent_domain.intelligence.state import State

PLAN_CREATED = "plan.created"
PLAN_INVALIDATED = "plan.invalidated"
EXECUTION_RESULT = "execution_result"
EXECUTION_FAILED = "execution_failed"
RUN_FINISHED = "run.finished"
#: M33 / B-8：被叫停也是一件**事实**，同样要进 State。
#:
#: 不留它的后果与"驳回不留 Observation"同构（见 `AgentLoop.reject`）：
#: Agent 不知道自己被叫停过。对一条被恢复的 Run 来说尤其致命 ——
#: 它恢复出来的 State 里没有这一条，于是"为什么我在这里"无从回答。
RUN_CANCELLED = "run.cancelled"

# ── M16：Harness 产生的四类事实 ──
# 它们**不进** completed_tasks：审批不是业务 Task，是 Kernel 的一道闸门。
# 但必须进 State，否则 Agent 不知道自己被拦过 / 被驳回了。
POLICY_DENIED = "policy.denied"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_REJECTED = "approval.rejected"

# ── M25：派生子 Run 产生的两类事实 ──
# 同审批那四类：不进 completed_tasks（它不是"干完的一件活"），
# 但必须进 State —— 否则 Agent 不知道自己派出去过一条子 Run，
# 下一次决策很可能再派一条同样的（D-1 的语义层保险）。
CHILD_RUN_SPAWNED = "child_run.spawned"
CHILD_RUN_FINISHED = "child_run.finished"
#: D-19 / 空洞 229：等到上限也没有结果。
#:
#: 刻意**不复用** `CHILD_RUN_FINISHED`：那条子 Run 没有 finished，
#: 我们只是**不再等了**。用同一个 kind 会让 State 里写着"子 Run 已完成"，
#: 而下一次决策据此认为可以往下走 —— 于是"不知道"被当成"做完了"（PR-19）。
#: 模型必须能分辨这两种情况：一种该换路走，一种该先查那条子 Run 还在不在。
CHILD_RUN_UNKNOWN = "child_run.unknown"


class RuntimeReducer:
    """把"发生了什么"翻译成"我认为世界是什么样"。

    Reducer 里**不做**任何决策、不调 Kernel、不发副作用 ——
    它只把 Observation 的内容写进 State 的字段。
    """

    def reduce(self, state: State, obs: Observation) -> None:
        if obs.kind == PLAN_CREATED:
            plan = obs.content.get("plan")
            if isinstance(plan, Plan):
                state.current_plan = plan
            state.variables["plan_id"] = obs.content.get("plan_id")

        elif obs.kind == PLAN_INVALIDATED:
            state.current_plan = None

        elif obs.kind in (EXECUTION_RESULT, EXECUTION_FAILED):
            execution_id = obs.execution_id or ""
            # 成功和失败都要记账：失败也是事实，Agent 要靠它 Replan
            if execution_id and execution_id not in state.completed_tasks:
                state.active_tasks = [t for t in state.active_tasks if t != execution_id]
                state.completed_tasks.append(execution_id)
            state.variables[f"result:{execution_id}"] = {
                "kind": obs.kind,
                "summary": obs.summary,
                "attempt_no": obs.attempt_no,
            }

        elif obs.kind == RUN_FINISHED:
            state.runtime_status = "FINISHED"

        # ── Harness 的四类事实 ──
        elif obs.kind == POLICY_DENIED:
            denied = list(state.variables.get("denied_actions", []))
            denied.append(
                {
                    "action_id": obs.content.get("action_id"),
                    "action_type": obs.content.get("action_type"),
                    "reasons": obs.content.get("reasons", []),
                }
            )
            state.variables["denied_actions"] = denied

        elif obs.kind == APPROVAL_REQUESTED:
            state.variables["pending_approval_id"] = obs.content.get("approval_id")
            state.variables["pending_approval_execution"] = obs.content.get("execution_id")

        elif obs.kind in (APPROVAL_GRANTED, APPROVAL_REJECTED):
            # 清掉挂起标记；无论批准还是驳回，这一步都**结束了**
            state.variables.pop("pending_approval_id", None)
            state.variables.pop("pending_approval_execution", None)
            state.variables[f"approval:{obs.content.get('approval_id')}"] = obs.kind

        # 兜底：任何 Observation 都留下摘要，便于 Replay / 调试
        state.variables[f"obs:{obs.observation_id}"] = obs.summary
