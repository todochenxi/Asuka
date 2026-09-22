"""Run Checkpoint 的存储与写入（基线 §14）。

```text
Kernel Checkpoint（Execution 级，Kernel 拥有）
Run Checkpoint（Run 级，Runtime / Harness 拥有）   ← 本文件
```

> **Kernel 不知道什么是 Step。**
> `current_step` / `completed_tasks` 属于 Run Checkpoint，不属于 Kernel Checkpoint。

写入时机（§14，不定义就会在实现时随机化）：

| 时机 | 写什么 |
|---|---|
| Attempt 成功 | Kernel Checkpoint（Kernel 的事） |
| **Step 完成** | **Run Checkpoint ← 这里** |
| **进入 SUSPENDED 前** | **两者都写，强制 ← 这里** |
| 每次 LLM 调用后 | ❌ 不写 Checkpoint，只写 ContextSnapshot |

"挂起前强制写"这条最容易漏，但漏了后果最严重：
唤醒后要从 Checkpoint 继续，**不重新执行整个 Run**（§10）。
没有 Checkpoint 就只能重跑，而重跑对已经产生过外部副作用的 Task 是灾难。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

from packages.agent_domain.execution import RunCheckpoint


class RunCheckpointStore(Protocol):
    def save(self, checkpoint: RunCheckpoint) -> None: ...

    def latest(self, run_id: str) -> RunCheckpoint | None: ...

    def list_for(self, run_id: str) -> Sequence[RunCheckpoint]: ...


@dataclass
class InMemoryRunCheckpointStore:
    _items: list[RunCheckpoint] = field(default_factory=list)

    def save(self, checkpoint: RunCheckpoint) -> None:
        self._items.append(checkpoint)

    def latest(self, run_id: str) -> RunCheckpoint | None:
        for cp in reversed(self._items):
            if cp.run_id == run_id:
                return cp
        return None

    def list_for(self, run_id: str) -> list[RunCheckpoint]:
        return [cp for cp in self._items if cp.run_id == run_id]


def build_run_checkpoint(
    *,
    run_id: str,
    current_step: str,
    completed_tasks: Sequence[str],
    variables: Mapping[str, object] | None = None,
    context_snapshot_id: str | None = None,
) -> RunCheckpoint:
    """构造一个 Run Checkpoint。

    `context_snapshot_id` 是**引用**不是内嵌（§14）——
    Context 可能几十 KB，内嵌进 Checkpoint 会让恢复点变得又大又难比。
    """
    return RunCheckpoint(
        run_id=run_id,
        current_step=current_step,
        completed_tasks=tuple(completed_tasks),
        variables=dict(variables or {}),
        context_snapshot_id=context_snapshot_id,
    )
