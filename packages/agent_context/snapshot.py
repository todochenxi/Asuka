"""ContextSnapshot（M17，基线 §14）。

```text
Checkpoint        = 从哪里恢复
ContextSnapshot   = 模型当时看到了什么
```

C-5  两者**不是一回事**，而且必须能被断言区分：

    · `ContextSnapshot` 里没有 `current_step` / `completed_tasks`（那是 Run Checkpoint 的）
    · `RunCheckpoint` 里没有 `items` / `total_tokens`（那是 Snapshot 的）

§14 的写入时机表格里有一行很容易读漏：

> 每次 LLM 调用后 ❌ 不写 Checkpoint，只写 ContextSnapshot

原因是两者的**读者不同**：Checkpoint 是给 Recovery 读的（要能接着跑），
Snapshot 是给人和审计读的（要能回答"它当时凭什么这么答"）。
把 Context 塞进 Checkpoint 会让恢复路径背上几百 KB 的Prompt，
而它根本用不上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from packages.agent_domain.ids import new_id
from packages.execution_kernel.ports import Clock

from .budget import ContextPlan, DroppedItem
from .items import ContextItem


@dataclass(frozen=True)
class ContextSnapshot:
    """某一次模型调用**实际**看到的 Context。

    ⚠️ 记的是实际值，不是请求值 —— 这是阶段 9/10 已经踩过两次的坑
    （Gateway 的 `model_id`、Tool 的 `version`）第三次出现：
    只记"我们想给它看什么"的话，Snapshot 就退化成配置单，
    而真正的排障问题永远是"它实际看到了什么"。
    """

    snapshot_id: str
    run_id: str
    created_at: datetime
    items: tuple[ContextItem, ...] = ()
    dropped: tuple[DroppedItem, ...] = ()
    total_tokens: int = 0
    #: 调的是哪个模型 / 哪个 Deployment（实际值）
    model_id: str = ""
    deployment_id: str = ""
    execution_id: str = ""
    attributes: Mapping[str, object] = field(default_factory=dict)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i.source.value for i in self.items))

    def items_of(self, source: str) -> tuple[ContextItem, ...]:
        return tuple(i for i in self.items if i.source.value == source)


class ContextSnapshotStore(Protocol):
    def save(self, snapshot: ContextSnapshot) -> None: ...

    def get(self, snapshot_id: str) -> ContextSnapshot | None: ...

    def list_for(self, run_id: str) -> Sequence[ContextSnapshot]: ...


@dataclass
class InMemoryContextSnapshotStore:
    _items: list[ContextSnapshot] = field(default_factory=list)

    def save(self, snapshot: ContextSnapshot) -> None:
        self._items.append(snapshot)

    def get(self, snapshot_id: str) -> ContextSnapshot | None:
        for snap in reversed(self._items):
            if snap.snapshot_id == snapshot_id:
                return snap

    def list_for(self, run_id: str) -> list[ContextSnapshot]:
        return [s for s in self._items if s.run_id == run_id]


def build_snapshot(
    *,
    run_id: str,
    plan: ContextPlan,
    clock: Clock,
    model_id: str = "",
    deployment_id: str = "",
    execution_id: str = "",
    attributes: Mapping[str, object] | None = None,
) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=new_id("ctx"),
        run_id=run_id,
        created_at=clock.now(),
        items=tuple(plan.kept),
        dropped=tuple(plan.dropped),
        total_tokens=plan.total_tokens,
        model_id=model_id,
        deployment_id=deployment_id,
        execution_id=execution_id,
        attributes=dict(attributes or {}),
    )
