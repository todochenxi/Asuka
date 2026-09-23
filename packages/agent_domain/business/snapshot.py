"""RunSnapshot：Run 的**恢复信封**（M20，基线 §14 / §17）。

## 为什么它不是 RunCheckpoint

```text
RunCheckpoint   「恢复到哪一步」  current_step / completed_tasks / variables
RunSnapshot     「这一步当时是什么样」  State / 预算计数 / 花掉的钱 / 在等谁的审批
```

§14 要求"进入 SUSPENDED 前**强制**写 Checkpoint"。但**只写 Checkpoint 恢复不了**：

它记的是**指针**，不是**数据**。唤醒之后仍然不知道 Agent 当时认为世界是什么样、
已经走了几步、花了多少钱 —— 于是只能重跑整个 Run。
而重跑对已经产生过外部副作用的 Task 是灾难，这恰恰是 §14 想避免的事。

所以：

> **R-1：进入 SUSPENDED 前必须同时落 Snapshot。**
> Checkpoint 说"从这继续"，Snapshot 提供"继续需要的全部数据"。两者缺一不可。

## 为什么恢复**不能**重来

`steps` / `spent` / `consecutive_denials` 必须一并恢复。少任何一个，
"挂起—恢复"就变成了一条**重置预算的后门**：

```text
Run 走到预算上限 → 恰好被闸门挡住 → 恢复 → 计数归零 → 又能走一轮
```

于是预算不再是一个上限，而是一个可以被 HITL 反复刷新的窗口（**R-2**）。

## 归属

**R-5：恢复由 Runtime 做，不由 API 做。**
API 只问"这个 Run 在不在"；怎么重建是 Runtime 的事 ——
否则"怎么恢复"就有了第二个定义。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from ..errors import InvariantViolation
from ..ids import new_id
from .compensation import CompensationSpec
from ..intelligence.goal import Budget, Goal
from ..intelligence.observation import ArtifactRef, Observation, ObservationSource
from ..intelligence.plan import Plan, PlanNode
from ..intelligence.state import State
from .run import TERMINAL_RUN_STATUSES, AgentRunStatus
from .step import Step, StepStatus

_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_RUN_STATUSES)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 序列化
def _plain(value: Any) -> Any:
    """把领域对象摊成 JSON 安全的纯数据。

    刻意写得**通用**而不是给每个类手写 to_dict：
    手写版本每加一个字段就会漏一次，而漏掉的那个字段在恢复时是**静默**变成默认值的 ——
    这比报错糟得多（"记忆丢了一块"看起来跟"本来就没有"一模一样）。
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _plain(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    return value


def plain(value: Any) -> Any:
    """`_plain` 的公开入口（Runtime 侧的 Trace 序列化也要用）。"""
    return _plain(value)


def parse_dt(raw: Any) -> datetime | None:
    """`_dt` 的公开入口 —— 恢复路径（Runtime 侧）也要用同一套时间解析。

    时间格式不统一会在恢复时变成**静默错误**：解析失败返回 None，
    于是 deadline 变成"没有截止时间"，预算上限凭空消失。
    """
    return _dt(raw)


def _dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


def state_to_dict(state: State) -> dict[str, Any]:
    return _plain(state)


def state_from_dict(data: Mapping[str, Any]) -> State:
    goal_raw = data.get("goal") or {}
    budget_raw = goal_raw.get("budget") or {}
    goal = Goal(
        goal_id=goal_raw.get("goal_id") or "",
        run_id=goal_raw.get("run_id") or data.get("run_id", ""),
        objective=goal_raw.get("objective") or "",
        constraints=tuple(goal_raw.get("constraints") or ()),
        success_criteria=tuple(goal_raw.get("success_criteria") or ()),
        priority=int(goal_raw.get("priority") or 0),
        budget=Budget(
            max_steps=budget_raw.get("max_steps"),
            max_tokens=budget_raw.get("max_tokens"),
            max_cost_usd=budget_raw.get("max_cost_usd"),
            deadline=_dt(budget_raw.get("deadline")),
        ),
        metadata=dict(goal_raw.get("metadata") or {}),
    )

    plan: Plan | None = None
    plan_raw = data.get("current_plan")
    if plan_raw:
        plan = Plan(
            plan_id=plan_raw.get("plan_id") or "",
            run_id=plan_raw.get("run_id") or data.get("run_id", ""),
            nodes=tuple(
                PlanNode(
                    node_id=n.get("node_id", ""),
                    name=n.get("name", ""),
                    kind=n.get("kind", "task"),
                    depends_on=tuple(n.get("depends_on") or ()),
                    # M98/M99：新字段必须**读回来** —— 只序列化不反序列化的话，
                    # 一份从快照恢复的 plan 会丢掉它声明的 tool / 资源，
                    # 于是计划门对"恢复回来的计划"实际是**瞎的**（A-12：丢了变错）。
                    tool=n.get("tool", ""),
                    resource_labels=tuple(n.get("resource_labels") or ()),
                    expected_output=n.get("expected_output"),
                )
                for n in (plan_raw.get("nodes") or ())
            ),
            constraints=tuple(plan_raw.get("constraints") or ()),
            metadata=dict(plan_raw.get("metadata") or {}),
        )

    observations = [
        Observation(
            observation_id=o.get("observation_id") or "",
            run_id=o.get("run_id") or data.get("run_id", ""),
            source=ObservationSource(o.get("source") or ObservationSource.SYSTEM.value),
            kind=o.get("kind") or "",
            summary=o.get("summary") or "",
            content=dict(o.get("content") or {}),
            artifact_refs=tuple(
                ArtifactRef(
                    artifact_id=a.get("artifact_id", ""),
                    uri=a.get("uri", ""),
                    content_type=a.get("content_type", "application/octet-stream"),
                    size_bytes=int(a.get("size_bytes") or 0),
                )
                for a in (o.get("artifact_refs") or ())
            ),
            execution_id=o.get("execution_id"),
            attempt_no=o.get("attempt_no"),
            created_at=_dt(o.get("created_at")) or _utcnow(),
        )
        for o in (data.get("observations") or ())
    ]

    state = State(
        run_id=data["run_id"],
        goal=goal,
        current_plan=plan,
        active_tasks=list(data.get("active_tasks") or ()),
        completed_tasks=list(data.get("completed_tasks") or ()),
        observations=observations,
        variables=dict(data.get("variables") or {}),
        constraints=list(data.get("constraints") or ()),
        runtime_status=data.get("runtime_status") or "RUNNING",
    )
    # version 是并发校验字段（X-10），恢复时必须一并带回 ——
    # 否则恢复出来的 State 版本号归 1，后续 apply 的乐观锁就形同虚设。
    object.__setattr__(state, "version", int(data.get("version") or 1))
    return state


def action_to_dict(action: Any) -> dict[str, Any]:
    """Action 的完整序列化 —— **必须带上 `compensation`**（S-8）。

    这一对函数是 M26 逼出来的。之前只有审批 Store 里一个手写的 `_dump_action`，
    它漏掉了 `compensation` —— 而它不是"漏"，是当时**根本没有通道**：
    `CompensationSpec.to_dict` 因为缩进错误从来不存在（见 compensation.py）。

    漏掉 `compensation` 的后果不是"少一个字段"：
    恢复出来的 Action 没有逆操作声明 → `SagaCoordinator` 看到
    `action.compensation is None` 就**静默跳过** → 补偿账本缺一条。
    而缺的那一条正是子 Run 在外部世界留下的副作用（S-13 / A-12：这是**变错**）。

    所以：序列化只有**这一处**定义。审批与子 Run 共用，
    否则"Action 怎么落库"就会有两套答案，然后漂移。
    """
    spec = getattr(action, "compensation", None)
    return {
        "action_id": action.action_id,
        "run_id": action.run_id,
        "action_type": action.action_type.value,
        "payload": dict(action.payload),
        "timeout_seconds": (
            action.timeout.total_seconds() if action.timeout is not None else None
        ),
        "risk_level": action.risk_level.value,
        "rationale": action.rationale,
        "compensation": spec.to_dict() if spec is not None else None,
    }


def action_from_dict(data: Mapping[str, Any], *, run_id: str = "") -> Any:
    """`action_to_dict` 的逆。

    `run_id` 是兜底：老数据里可能没有这个字段，而 `Action.run_id` 是必填（I-3 附近）。
    但**空数据不是缺省，是损坏** —— 一条读不出"要干什么"的记录不该被放行，
    这里绝不能造一个占位 Action 顶上（审批场景里那等于让人在不知道批什么的情况下签字）。
    """
    from ..intelligence.action import Action, ActionType, RiskLevel  # 避免循环导入

    if not isinstance(data, Mapping):
        # 存的是 JSONB，但不同驱动交回来的可能是字符串 —— 归一化只在这里做一次。
        try:
            data = json.loads(data) if data else {}
        except (TypeError, ValueError):
            data = {}
    if not data:
        raise InvariantViolation(
            "action record has no data stored; refusing to load a placeholder"
        )
    seconds = data.get("timeout_seconds")
    spec_raw = data.get("compensation")
    return Action(
        action_id=data.get("action_id") or "",
        run_id=data.get("run_id") or run_id,
        action_type=ActionType(data["action_type"]),
        payload=data.get("payload") or {},
        timeout=timedelta(seconds=seconds) if seconds is not None else None,
        risk_level=RiskLevel(data.get("risk_level") or "low"),
        rationale=data.get("rationale") or "",
        compensation=(
            CompensationSpec.from_dict(spec_raw) if spec_raw else None
        ),
    )


def step_to_dict(step: Step) -> dict[str, Any]:
    return _plain(step)


def step_from_dict(data: Mapping[str, Any]) -> Step:
    """B-5：Step.status 是**派生**字段，恢复必须走 `deriving()` 窗口。

    直接 `step.status = ...` 会被守卫挡住 —— 那是对的设计，
    所以这里不开后门，而是用 `sync()` 自己也在用的那个窗口。
    """
    step = Step(
        step_id=data["step_id"],
        run_id=data.get("run_id") or "",
        plan_node_id=data.get("plan_node_id") or "",
        name=data.get("name") or "",
        task_ids=tuple(data.get("task_ids") or ()),
    )
    status = data.get("status")
    if status:
        with step.deriving():
            step.status = StepStatus(status)
    return step


# ---------------------------------------------------------------- 快照
@dataclass(frozen=True)
class RunSnapshot:
    """一次可恢复点的**全部**数据。"""

    snapshot_id: str = field(default_factory=lambda: new_id("snap"))
    run_id: str = ""
    agent_id: str = ""
    status: str = ""                                  # AgentRunStatus.value
    state: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[Mapping[str, Any], ...] = ()
    current_step_id: str = ""
    #: 预算计数（已执行的动作数），**不是** Step 的个数
    step_count: int = 0
    #: L-7：连续被拒计数。不恢复的话，一个靠"挂起"续命的 Run 能绕开 DENY_LOOP
    consecutive_denials: int = 0
    pending_approval_id: str | None = None
    #: R-6（M26）：挂起还必须能说清"在等哪条子 Run"。
    #:
    #: 这一列是被一个探针逼出来的：M25 加了 `CHILD_AGENT` / `CHILD_SKILL`
    #: 两种挂起原因，但快照只认审批。于是"为子 Run 而挂起的 Run"
    #: 一旦被快照就撞上 R-1 自己的断言（`is_gated` 却没有 `pending_approval_id`）
    #: 直接 InvariantViolation —— **它连一份快照都落不下来**，遑论恢复。
    #:
    #: R-1 的**意图**从来没错："挂起必须带着你在等谁"。
    #: 错的是它的实现把"等谁"写死成了"等人"。
    pending_child_id: str | None = None
    spent: Mapping[str, Any] = field(default_factory=dict)
    #: R-4：审计账本本身也要带走 —— 否则进程重启就把一个 Run 的账切成两截，
    #: 前半段（谁请求了审批、花了多少 token）在老进程里，后半段在新进程里，
    #: 两边都"看起来完整"，拼起来才知道断了。
    trace: tuple[Mapping[str, Any], ...] = ()
    #: R-7（M86）：注入的 Intelligence 实现**自述的进度**。
    #:
    #: 这一列是被一个探针逼出来的。`RunSnapshot` 的 docstring 写着
    #: "一次可恢复点的**全部**数据"，但那份"全部"只覆盖了 Runtime 自己的
    #: 内存状态（steps / denials / spent / trace …）。注入进来的
    #: Planner / DecisionEngine 若自己记着"走到第几步了"，那份进度不在里面 ——
    #: 于是**恢复之后引擎从第 1 次重新开始**。
    #:
    #: 探针（probe86.py）实测最朴素的后果：一个已经调过模型的 Run 恢复后
    #: **又调了一次模型**（第 1 次的分支），而不是接着 FINISH。
    #:
    #: 形状是两个键的字典，各自可为 None（= 那个 Port 是无状态的，
    #: 或者它没实现 `ProgressBearing`）::
    #:
    #:     {"planner": <planner.progress()>, "decision_engine": <engine.progress()>}
    #:
    #: Runtime **不解释**这两个值，只做一件事：恢复时比一次。
    #: 对不上就点名拒绝恢复（宁可拒绝，不许编造）——
    #: 恢复不了一条 Run 是可接受的；假装恢复而实际重跑一遍不可接受。
    progress: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("R-1: RunSnapshot.run_id is required")
        if not self.state:
            # 没有 State 的快照不是"退化版快照"，是**没法恢复的快照** ——
            # 宁可构造失败，也不要让它在恢复时变成一个空 State 而看不出来。
            raise InvariantViolation("R-1: RunSnapshot.state is required")
        if self.is_gated and not (self.pending_approval_id or self.pending_child_id):
            # 挂着却不知道在等谁 —— 恢复出来谁也叫不醒它。
            #
            # R-6 之前这里写死成 `pending_approval_id`。那在只有一个挂起原因的
            # 时候是对的；M25 引入子 Run 挂起之后，它就变成了一条**会误伤**的断言：
            # 不是在挡住错误，而是在挡住一种合法的挂起。
            raise InvariantViolation(
                "R-1/R-6: a SUSPENDED snapshot must say what it is waiting for "
                "(pending_approval_id or pending_child_id); without it a restored "
                "run is suspended with nobody left to wake it"
            )
        object.__setattr__(self, "state", dict(self.state))
        object.__setattr__(self, "spent", dict(self.spent))
        object.__setattr__(self, "trace", tuple(dict(e) for e in self.trace))
        object.__setattr__(self, "progress", dict(self.progress))

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_VALUES

    @property
    def is_gated(self) -> bool:
        return self.status == AgentRunStatus.SUSPENDED.value

    @property
    def waiting_for(self) -> str | None:
        """挂起在等谁：审批 id 或子 Run id；不在挂起 → None。

        恢复路径要的就是这一个答案 —— "接上谁"。
        刻意不把它做成集合：一次挂起只有一个原因，
        做成集合会让调用方去处理一个**实际不会发生**的组合。
        """
        return self.pending_approval_id or self.pending_child_id
