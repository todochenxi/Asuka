"""Memory 的 PostgreSQL 实现（M96）。

`MemoryStore` 的**事实源**（C-7）：向量索引是派生数据，重建的依据是这里。
此前只有 `InMemoryMemoryStore` —— 进程一重启"它记得什么"就没了，
而记忆的全部意义恰恰是**跨 Run**。

表结构见 `infrastructure/postgres/020_memories.sql`。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..memory import MemoryLayer, MemoryRecord

#: `MemoryRecord.created_at` 默认是 `None`（领域层不强制），但 DB 列是 NOT NULL。
#: 缺值补"现在" —— 与领域语义一致，而不是让 INSERT 炸。
def _created(record: MemoryRecord) -> datetime:
    return record.created_at or datetime.now(timezone.utc)

_INSERT = (
    "INSERT INTO memories ("
    " memory_id, layer, subject, content, source_run_id, source_step_id,"
    " attributes, created_at"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)

_SELECT = (
    "SELECT memory_id, layer, subject, content, source_run_id, source_step_id,"
    " attributes, created_at FROM memories"
)


def _load(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _row_to_record(row: Mapping[str, Any]) -> MemoryRecord:
    created = row["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    return MemoryRecord(
        layer=MemoryLayer(str(row["layer"])),
        subject=str(row["subject"]),
        content=str(row["content"]),
        memory_id=str(row["memory_id"]),
        source_run_id=str(row["source_run_id"] or ""),
        source_step_id=str(row["source_step_id"] or ""),
        created_at=created,
        attributes=dict(_load(row["attributes"]) or {}),
    )


class PostgresMemoryStore:
    """`MemoryStore` 的 PostgreSQL 实现（020_memories.sql）。

    ⚠️ `delete()` 在这里是**真的删**（合规删除的语义）。生产里若要保留审计，
    该换成失效标记而不是 DELETE —— 但那是 Memory 的版本化策略，
    不在"把 Memory 落到 PG"这一层擅自决定（B-7：一个事实一处定义）。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, record: MemoryRecord) -> None:
        cur = self.conn.cursor()
        cur.execute(
            _INSERT,
            (
                record.memory_id,
                record.layer.value,
                record.subject,
                record.content,
                record.source_run_id,
                record.source_step_id,
                json.dumps(dict(record.attributes), ensure_ascii=False, default=str),
                _created(record),
            ),
        )

    def search(
        self,
        *,
        layer: MemoryLayer | None = None,
        subject: str = "",
        query: str = "",
        limit: int = 10,
    ) -> Sequence[MemoryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if layer is not None:
            clauses.append("layer = %s")
            params.append(layer.value)
        if subject:
            clauses.append("subject = %s")
            params.append(subject)
        if query:
            clauses.append("lower(content) LIKE %s")
            params.append(f"%{query.lower()}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur = self.conn.cursor()
        cur.execute(
            _SELECT + where + " ORDER BY created_at, memory_id LIMIT %s",
            tuple(params),
        )
        return [_row_to_record(r) for r in cur.fetchall()]

    def delete(self, memory_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM memories WHERE memory_id = %s", (memory_id,))


__all__ = ["PostgresMemoryStore"]
