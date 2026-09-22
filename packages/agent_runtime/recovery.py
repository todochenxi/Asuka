"""Run Recovery：把挂起的 Run 从存储里重新装载出来（M20）。

## 为什么需要这一层

M18 补上了 HITL 审批回调，M19 让审批记录活过重启。但还有一个洞：

```text
服务重启 → 内存里的 RuntimeStack 全没了
         → 审批还在 PG 里（M19 保证了）
         → 但 Run 没了 → decide() 返回 404 RUN_NOT_FOUND
         → 人看得见待批事项，却点不动它
```

`ApprovalStore` 持久化只解决了"看得见"，没解决"点得动"。

## 恢复什么，不恢复什么

**恢复**：State / 已走的步数 / 花掉的钱 / 连续被拒计数 / Step 列表 / 在等哪条审批 / 审计账本。

**不恢复**：Kernel 的 Execution / Attempt —— 它们本来就在 PG 里，
由 Kernel 自己读。**恢复不是重跑**，已经发出去的 Task 不会重发（幂等键 = execution_id）。

## 不变量

```text
R-1  进入 SUSPENDED 前必须落 Snapshot（只落 RunCheckpoint 恢复不了：它是指针不是数据）
R-2  恢复必须带上预算计数与已花费 —— 否则"挂起—恢复"是一条重置预算的后门
R-3  终态 Run 不可恢复（终态不可变，§10）
R-4  审计账本必须延续：run_id 连续 + 序号连续 + 留一条 RECOVERED
R-5  恢复由 Runtime 做，不由 API 做（API 只问"在不在"，不管"怎么重建"）
```
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from packages.agent_domain.business.snapshot import RunSnapshot, parse_dt, plain
from packages.agent_domain.errors import IllegalTransition
from packages.agent_harness.approval import ApprovalStore

from .trace import TraceEntry

__all__ = [
    "InMemoryRunSnapshotStore",
    "RunRecovery",
    "RunSnapshotStore",
    "trace_entries_to_dicts",
    "trace_entry_from_dict",
]


class RunSnapshotStore(Protocol):
    """RunSnapshot 的存储端口。

    **必须持久**（R-1）：快照存在的唯一理由就是活过进程重启，
    放内存里等于没写。
    """

    def save(self, snapshot: RunSnapshot) -> None: ...

    def latest(self, run_id: str) -> RunSnapshot | None: ...

    def list_for(self, run_id: str) -> Sequence[RunSnapshot]: ...


@dataclass
class InMemoryRunSnapshotStore:
    """测试用。生产必须换成 PG 版 —— 见类上方的理由。"""

    _items: list[RunSnapshot] = field(default_factory=list)

    def save(self, snapshot: RunSnapshot) -> None:
        self._items.append(snapshot)

    def latest(self, run_id: str) -> RunSnapshot | None:
        for snap in reversed(self._items):
            if snap.run_id == run_id:
                return snap
        return None

    def list_for(self, run_id: str) -> list[RunSnapshot]:
        return [s for s in self._items if s.run_id == run_id]


# ---------------------------------------------------------------- Trace
def trace_entries_to_dicts(trace: Any) -> tuple[Mapping[str, Any], ...]:
    return tuple(plain(e) for e in trace.entries)


def trace_entry_from_dict(data: Mapping[str, Any]) -> TraceEntry:
    return TraceEntry(
        seq=int(data["seq"]),
        kind=data["kind"],
        run_id=data.get("run_id") or "",
        step_id=data.get("step_id") or "",
        task_id=data.get("task_id") or "",
        execution_id=data.get("execution_id") or "",
        attempt_no=int(data.get("attempt_no") or 0),
        at=parse_dt(data.get("at")) or datetime.now(),
        served=dict(data.get("served") or {}),
        payload=dict(data.get("payload") or {}),
    )


# ---------------------------------------------------------------- 服务
@dataclass
class RunRecovery:
    """R-5：恢复是 Runtime 的事。

    API 层只调用 `rebuild()`，不知道快照长什么样、也不知道怎么把 State 反序列化回来 ——
    "怎么恢复"只有这一个定义。
    """

    snapshots: RunSnapshotStore
    #: 与 ControlPlane 的 factory 同签名：`(agent_id, approval_store) -> RuntimeStack`。
    #: 装配时**必须**把同一个 `snapshots` 也传给 `assemble_runtime_stack()` ——
    #: 写的人是 Loop，读的人是这里，两边得是同一份。
    factory: Callable[[str, ApprovalStore], RuntimeStack]
    approvals: ApprovalStore

    def has(self, run_id: str) -> bool:
        return self.snapshots.latest(run_id) is not None

    def rebuild(self, run_id: str) -> "RuntimeStack":
        """把一个 Run 从最新快照重新装载出来。"""
        snapshot = self.snapshots.latest(run_id)
        if snapshot is None:
            # 没有快照 = 没有可恢复点。这不是"退化成新建"，是**恢复不了**。
            raise LookupError(f"R-1: no snapshot for run {run_id!r}; nothing to recover")
        if snapshot.is_terminal:
            # R-3：终态不可变（§10）。能"恢复"一个已经结束的 Run，
            # 就等于给终态开了一个后门 —— COMPLETED 也能被重新推着走。
            raise IllegalTransition(
                f"R-3: run {run_id!r} is already {snapshot.status}; "
                "a terminal run cannot be restored"
            )
        stack = self.factory(snapshot.agent_id, self.approvals)
        stack.loop.restore(snapshot)
        return stack
