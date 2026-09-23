"""`agent_context` 的 PostgreSQL 适配器（M95）。"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..budget import DroppedItem
from ..items import ContextItem, ContextSource
from ..snapshot import ContextSnapshot

_INSERT = (
    "INSERT INTO context_snapshots ("
    " snapshot_id, run_id, execution_id, model_id, deployment_id,"
    " total_tokens, items, dropped, attributes, created_at"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_SELECT = (
    "SELECT snapshot_id, run_id, execution_id, model_id, deployment_id,"
    " total_tokens, items, dropped, attributes, created_at"
    " FROM context_snapshots"
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: Any) -> Any:
    """JSONB 列：psycopg 会自动解析，sqlite 替身也注册了转换器 —— 兜底再解析一次。"""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _item_to_dict(item: ContextItem) -> dict[str, Any]:
    return {
        "source": item.source.value,
        "key": item.key,
        "text": item.text,
        "priority": item.priority,
        "reference": item.reference,
        "pinned": item.pinned,
        "attributes": dict(item.attributes),
    }


def _item_from_dict(data: Mapping[str, Any]) -> ContextItem:
    return ContextItem(
        source=ContextSource(str(data.get("source") or ContextSource.KNOWLEDGE.value)),
        key=str(data.get("key") or ""),
        text=str(data.get("text") or ""),
        priority=int(data.get("priority") or 0),
        reference=str(data.get("reference") or ""),
        pinned=bool(data.get("pinned", False)),
        attributes=dict(data.get("attributes") or {}),
    )


def _row_to_snapshot(row: Mapping[str, Any]) -> ContextSnapshot:
    created = row["created_at"]
    if isinstance(created, str):
        from datetime import datetime, timezone

        created = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
    return ContextSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        run_id=str(row["run_id"]),
        created_at=created,
        items=tuple(_item_from_dict(d) for d in _load(row["items"])),
        dropped=tuple(
            DroppedItem(
                key=str(d.get("key") or ""),
                source=str(d.get("source") or ""),
                tokens=int(d.get("tokens") or 0),
                reason=str(d.get("reason") or ""),
            )
            for d in _load(row["dropped"])
        ),
        total_tokens=int(row["total_tokens"] or 0),
        model_id=str(row["model_id"] or ""),
        deployment_id=str(row["deployment_id"] or ""),
        execution_id=str(row["execution_id"] or ""),
        attributes=dict(_load(row["attributes"]) or {}),
    )


class PostgresContextSnapshotStore:
    """`ContextSnapshotStore` 的 PostgreSQL 实现（019_context_snapshots.sql）。

    它替换的是 `InMemoryContextSnapshotStore`：那个版本进程一重启
    "模型当时看到了什么"就查不到了 —— 而那正是 C-5 要回答的问题。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, snapshot: ContextSnapshot) -> None:
        cur = self.conn.cursor()
        cur.execute(
            _INSERT,
            (
                snapshot.snapshot_id,
                snapshot.run_id,
                snapshot.execution_id,
                snapshot.model_id,
                snapshot.deployment_id,
                snapshot.total_tokens,
                _dump([_item_to_dict(i) for i in snapshot.items]),
                _dump(
                    [
                        {
                            "key": d.key,
                            "source": d.source,
                            "tokens": d.tokens,
                            "reason": d.reason,
                        }
                        for d in snapshot.dropped
                    ]
                ),
                _dump(dict(snapshot.attributes)),
                snapshot.created_at,
            ),
        )

    def get(self, snapshot_id: str) -> ContextSnapshot | None:
        cur = self.conn.cursor()
        cur.execute(_SELECT + " WHERE snapshot_id = %s", (snapshot_id,))
        row = cur.fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def list_for(self, run_id: str) -> Sequence[ContextSnapshot]:
        cur = self.conn.cursor()
        cur.execute(
            _SELECT + " WHERE run_id = %s ORDER BY created_at, snapshot_id",
            (run_id,),
        )
        return [_row_to_snapshot(r) for r in cur.fetchall()]


__all__ = ["PostgresContextSnapshotStore"]
