"""Run Trace：基线 §44 要求的六要素之一。

```text
Event / Trace / Attempt / Checkpoint / Cost / Token Usage
```

**为什么 State 不够，还要有 Trace：**

    Observation   进 State，是**给 Agent 看的**
                  Replan 可以覆盖它，Reducer 可以折叠它，它是工作记忆

    Trace         不进 State，是**给人和审计看的**
                  只增不改 —— Replan 动不了它

如果 Trace 也能被改写，那"这个 Run 到底干过什么"就没有事实了：
State 是会被 Replan 重写的工作记忆，不是账本。

**为什么 Trace 里必须记"实际服务值"而不是"请求值"：**

这是本轮之前已经踩过两次的坑（Model Gateway 的 `model_id`、
Tool Runtime 的 `version`）：请求 `gpt-4o` 但实际由 `gpt-4o-mini` 服务，
请求 `v2` 但实际跑的是 `v1`。只记请求值的话，事后排障和成本归因
会对着一份"从来没发生过"的账本查问题。

所以 `served` 字段是 Trace 的一等公民，不是可选装饰。

L-6  Trace 是 append-only 的派生审计记录：只有 append，没有 update / pop /
     clear。它是事实，Replan 不能改写事实。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

from packages.agent_domain.errors import InvariantViolation
from packages.execution_kernel.ports import Clock

# ---------------------------------------------------------------- 事件种类
CONTEXT = "context.built"             # M17：一次模型调用的 Context 组装完成
SUBMITTED = "task.submitted"          # Loop 交棒给 Kernel 的那一刻
OBSERVED = "execution.observed"       # 回写结果变成 Observation
CHECKPOINT = "checkpoint.written"     # Run Checkpoint 落盘
APPROVAL = "approval.requested"       # 被 Harness 闸门挡住
APPROVED = "approval.decided"         # 人来答复了
FINISHED = "run.finished"             # Runtime 声明终态（B-7）
CANCELLED = "run.cancelled"           # M33 / B-8：谁叫停的、为什么（审计的第一入口）
RECOVERED = "run.recovered"           # M20：进程重启后从 Snapshot 重新装载
SNAPSHOT = "snapshot.written"         # M20：落了一个可恢复点

# M10：补偿（Saga）。撤销本身是一条事实，不是"没发生过" —— 所以每一步都要留痕。
COMPENSATION_RECORDED = "compensation.recorded"      # 正向执行留下了副作用，登记待撤销
COMPENSATION_STARTED = "compensation.started"        # 撤销动作交给 Kernel
COMPENSATION_DONE = "compensation.done"              # 撤销动作跑完（成/败都记）
COMPENSATION_FINISHED = "compensation.finished"      # 一轮补偿跑完（汇总）
COMPENSATION_UNRESOLVED = "compensation.unresolved"  # S-5：有撤销不掉的，必须被看见
COMPENSATION_DEFERRED = "compensation.deferred"      # S-15：取消时留给人决定，但不静默
COMPENSATION_RELEASED = "compensation.released"      # S-16：Run 成功，副作用按预期保留


@dataclass(frozen=True)
class TraceEntry:
    """一条不可变的事实。

    `step_id / task_id / execution_id / attempt_no` 四个字段是 Trace 的价值所在：
    它们把 §4 的基数链 `Run → Step → Task → Execution → Attempt`
    在一次记录里串起来，于是"这个 token 花在哪一步的哪个 Attempt 上"
    是一个查询，不是一次考古。
    """

    seq: int
    kind: str
    run_id: str
    step_id: str
    task_id: str
    execution_id: str
    attempt_no: int
    at: datetime
    #: 请求值 vs 实际服务值 —— 两者都要留（见模块 docstring）
    served: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


class SystemClock:
    def now(self) -> datetime:
        from datetime import datetime as _dt

        return _dt.now()


@dataclass
class RunTrace:
    """一个 Run 的审计账本。

    对外只有 `append()` 和只读访问器 —— **故意不提供** update / pop / clear /
    remove / __setitem__。这不是偷懒，是 L-6：能删改的账本不是账本。
    """

    run_id: str = ""
    clock: Clock = field(default_factory=SystemClock)
    entries: list[TraceEntry] = field(default_factory=list)

    def append(
        self,
        kind: str,
        *,
        step_id: str = "",
        task_id: str = "",
        execution_id: str = "",
        attempt_no: int = 0,
        served: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> TraceEntry:
        entry = TraceEntry(
            seq=len(self.entries) + 1,
            kind=kind,
            run_id=run_id or self.run_id,
            step_id=step_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt_no=attempt_no,
            at=self.clock.now(),
            served=dict(served or {}),
            payload=dict(payload or {}),
        )
        self.entries.append(entry)
        return entry

    def restore(self, entries: Sequence[TraceEntry]) -> None:
        """**只有**恢复路径可以调用（M20 / R-4）。

        L-6 说账本不可删改，但那是说**运行时**：能删改的账本不是账本。
        从持久层重新装载是另一件事 —— 不是改写事实，是把进程重启时丢掉的事实读回来。

        所以这里加了两条约束，把它和"随便改账本"区分开：
          · 只能往**空账本**里装（非空就是覆盖，那是改写）
          · 序号必须连续（断号说明丢了一段，宁可失败也不能悄悄接上）
        """
        if self.entries:
            raise InvariantViolation(
                "L-6: cannot restore into a non-empty trace; that would be a rewrite"
            )
        expected = 1
        for entry in entries:
            if entry.seq != expected:
                raise InvariantViolation(
                    f"R-4: trace sequence is broken at {entry.seq}, expected {expected}"
                )
            expected += 1
        self.entries.extend(entries)

    # ------------------------------------------------------------ 只读访问
    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[TraceEntry]:
        return iter(tuple(self.entries))

    def kinds(self) -> tuple[str, ...]:
        return tuple(e.kind for e in self.entries)

    def for_step(self, step_id: str) -> tuple[TraceEntry, ...]:
        return tuple(e for e in self.entries if e.step_id == step_id)

    def for_execution(self, execution_id: str) -> tuple[TraceEntry, ...]:
        return tuple(e for e in self.entries if e.execution_id == execution_id)

    def of_kind(self, kind: str) -> tuple[TraceEntry, ...]:
        return tuple(e for e in self.entries if e.kind == kind)


def served_from_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """从 Attempt result 里抽出"实际服务值"。

    LLM 调用给的是 `deployment` / `model`；工具调用给的是 `version`。
    两者形状不同，但语义相同：**实际发生了什么**，不是**请求了什么**。
    """
    result = result or {}
    served: dict[str, Any] = {}
    for key in ("model", "deployment", "version", "tool", "side_effect"):
        value = result.get(key)
        if value:
            served[key] = value
    gateway = result.get("gateway")
    if isinstance(gateway, Mapping):
        served["fallback_count"] = gateway.get("fallback_count", 0)
        served["degraded"] = gateway.get("degraded", False)
    return served
