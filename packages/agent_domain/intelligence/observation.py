"""Observation：Agent 对执行结果 / 外部世界的感知。

I-6  Observation 只能来自真实执行结果，不可凭空构造
I-7  大文件必须走 Artifact → S3，Observation 只存引用
I-5  Observation 不可变（frozen，支撑 Replay）
X-4  Event ≠ Observation：Event 给系统与审计，Observation 给 Agent
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from ..errors import InvariantViolation
from ..ids import new_observation_id

# 内联内容上限：超过就必须落 Artifact，Observation 只保留引用
MAX_INLINE_BYTES = 64 * 1024


class ObservationSource(str, Enum):
    """I-6：Observation 必须能说清自己是从哪来的。"""

    EXECUTION_RESULT = "execution_result"   # Task/Execution/Attempt 的真实产出
    EXTERNAL_EVENT = "external_event"       # 外部系统回调 / Webhook
    HUMAN_INPUT = "human_input"             # HITL / AskUser
    SYSTEM = "system"                       # 系统注入（如超时、配额提示）


@dataclass(frozen=True)
class ArtifactRef:
    """大对象的引用。真正的内容在 S3 / MinIO，Observation 只存这个。"""

    artifact_id: str
    uri: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.uri:
            raise InvariantViolation("ArtifactRef.artifact_id / uri are required")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _estimate_size(content: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - 防御性
        return MAX_INLINE_BYTES + 1


@dataclass(frozen=True)
class Observation:
    observation_id: str = field(default_factory=new_observation_id)
    run_id: str = ""
    source: ObservationSource = ObservationSource.SYSTEM
    kind: str = ""                          # tool_result / llm_output / retrieval / human_reply / ...
    summary: str = ""                       # 进 Context 的简短摘要（人/模型可读）
    content: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    execution_id: str | None = None
    attempt_no: int | None = None
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("Observation.run_id is required")
        if not self.kind:
            raise InvariantViolation("Observation.kind is required")
        if not isinstance(self.source, ObservationSource):
            raise InvariantViolation("I-6: Observation.source must be an ObservationSource")
        object.__setattr__(self, "content", dict(self.content))

        # I-7：大对象必须走 Artifact，不允许内联
        if _estimate_size(self.content) > MAX_INLINE_BYTES and not self.artifact_refs:
            raise InvariantViolation(
                "I-7: observation content exceeds "
                f"{MAX_INLINE_BYTES} bytes and has no artifact_refs; "
                "large payloads must be stored as Artifact (S3) and referenced"
            )

    # ------------------------------------------------------- I-6 唯一入口
    @classmethod
    def from_execution_result(
        cls,
        *,
        run_id: str,
        execution_id: str,
        attempt_no: int,
        kind: str,
        summary: str,
        content: Mapping[str, Any] | None = None,
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> "Observation":
        """I-6：来自执行的 Observation 只能由这个工厂构造，必须绑定真实 Execution。"""
        if not execution_id:
            raise InvariantViolation("I-6: execution_id is required for execution_result")
        if attempt_no is None or attempt_no < 1:
            raise InvariantViolation("I-6: attempt_no must be >= 1 for execution_result")
        return cls(
            run_id=run_id,
            source=ObservationSource.EXECUTION_RESULT,
            kind=kind,
            summary=summary,
            content=dict(content or {}),
            artifact_refs=artifact_refs,
            execution_id=execution_id,
            attempt_no=attempt_no,
        )

    def is_large(self) -> bool:
        return bool(self.artifact_refs)
