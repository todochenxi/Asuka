"""Memory（M17，基线 §21）。

C-7  **Memory 是分层的，且 Qdrant ≠ Truth。**

```text
WORKING     热：当前 Run / 会话内（Redis）   丢了只是变笨，不会变错
EPISODIC    事件：某次 Run 发生过什么（PG）
SEMANTIC    长期事实：用户 / 领域（PG 为事实源，Qdrant 只是索引）
PROCEDURAL  做法：Skill / 经验
```

`Qdrant ≠ Truth`（§41）在 Memory 上的具体含义：
**向量索引是派生数据**。它丢了的后果是"检索不到"，不是"记错了" ——
重建索引就能恢复，而重建的依据是 PG 里的 `MemoryRecord`。
所以 `MemoryStore` 是事实源接口，`Retriever`（retrieval.py）是它之上的索引。

C-8  Memory 写入必须能溯源到 `source_run_id`（事件记忆则必填）：
一条不知道从哪来的记忆既无法审计，也无法在出错时定点删除。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from packages.agent_domain.ids import new_id

from .items import ContextItem, ContextSource


class MemoryLayer(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True)
class MemoryRecord:
    layer: MemoryLayer
    subject: str
    """这条记忆是**关于谁的**：user / agent / tenant / 某个实体 id。

    没有 subject 的记忆无法做隔离 —— 多租户下它会被别的 tenant 检索到。
    """
    content: str
    memory_id: str = field(default_factory=lambda: new_id("mem"))
    source_run_id: str = ""       # C-8
    source_step_id: str = ""
    created_at: datetime | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("MemoryRecord.subject is required (multi-tenant isolation)")
        if not self.content:
            raise ValueError("MemoryRecord.content is required")
        if self.layer is MemoryLayer.EPISODIC and not self.source_run_id:
            raise ValueError("C-8: episodic memory must be traceable to source_run_id")


class MemoryStore(Protocol):
    """Memory 的**事实源**接口。向量索引在它之上（retrieval.py）。"""

    def save(self, record: MemoryRecord) -> None: ...

    def search(
        self,
        *,
        layer: MemoryLayer | None = None,
        subject: str = "",
        query: str = "",
        limit: int = 10,
    ) -> Sequence[MemoryRecord]: ...

    def delete(self, memory_id: str) -> None:
        """遗忘 / 合规删除。没有它，"被要求删除的记忆"只能靠重建库。"""
        ...


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._rows: dict[str, MemoryRecord] = {}

    def save(self, record: MemoryRecord) -> None:
        self._rows[record.memory_id] = record

    def search(
        self,
        *,
        layer: MemoryLayer | None = None,
        subject: str = "",
        query: str = "",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        rows = [
            r
            for r in self._rows.values()
            if (layer is None or r.layer is layer)
            and (not subject or r.subject == subject)
            and (not query or query.lower() in r.content.lower())
        ]
        rows.sort(key=lambda r: (r.created_at or datetime.min, r.memory_id))
        return rows[:limit]

    def delete(self, memory_id: str) -> None:
        self._rows.pop(memory_id, None)


@dataclass
class MemoryManager:
    """Memory → Context 的**唯一转换口**。

    C-1 的落点：`recall()` 返回的是 `ContextItem`，不是 `MemoryRecord`。
    Memory 是"记住的东西"，Context 是"这次要喂给模型的东西" ——
    两者之间必须有一次显式转换，否则 Memory 的字段（layer / subject /
    source_run_id）会一路泄漏进 Context，再泄漏进 Prompt。
    """

    store: MemoryStore
    default_limit: int = 10

    def remember(
        self,
        *,
        layer: MemoryLayer,
        subject: str,
        content: str,
        source_run_id: str = "",
        source_step_id: str = "",
        **attributes: Any,
    ) -> MemoryRecord:
        record = MemoryRecord(
            layer=layer,
            subject=subject,
            content=content,
            source_run_id=source_run_id,
            source_step_id=source_step_id,
            attributes=dict(attributes),
        )
        self.store.save(record)
        return record

    def recall(
        self,
        *,
        subject: str,
        layers: Sequence[MemoryLayer] = (MemoryLayer.SEMANTIC, MemoryLayer.EPISODIC),
        query: str = "",
        limit: int | None = None,
        priority: int = 40,
    ) -> tuple[ContextItem, ...]:
        items: list[ContextItem] = []
        for layer in layers:
            for record in self.store.search(
                layer=layer, subject=subject, query=query,
                limit=limit or self.default_limit,
            ):
                items.append(
                    ContextItem(
                        source=ContextSource.MEMORY,
                        key=record.memory_id or f"{layer.value}:{record.subject}",
                        text=record.content,
                        priority=priority,
                        attributes={
                            "layer": layer.value,
                            "subject": record.subject,
                            "source_run_id": record.source_run_id,
                        },
                    )
                )
        return tuple(items)
