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

        self._assert_source_matches_binding()

        # I-7：大对象必须走 Artifact，不允许内联
        if _estimate_size(self.content) > MAX_INLINE_BYTES and not self.artifact_refs:
            raise InvariantViolation(
                "I-7: observation content exceeds "
                f"{MAX_INLINE_BYTES} bytes and has no artifact_refs; "
                "large payloads must be stored as Artifact (S3) and referenced"
            )

    # ------------------------------------------------- I-6 的绑定关系（M85）
    def _assert_source_matches_binding(self) -> None:
        """I-6：**来源**与**绑定**必须互相印证，且这个方向是双向的。

        ------------------------------------------------------------------
        为什么这条判据不能只写在 `from_execution_result` 里

        I-6 原来只有一句 docstring（"只能由这个工厂构造"）加一个工厂方法，
        但 `Observation` 是 `@dataclass(frozen=True)` —— `__init__` 是**公开**的，
        而 `execution_id: str | None = None` 这个默认值本身就是漏洞的形状。
        探针（`probe84.py`）实测四扇门全开：

            直接构造 EXECUTION_RESULT 且不带 execution_id   → ★ 成功
            execution_id=""（空串，同样没绑定）              → ★ 成功
            非执行来源凭空挂一个 execution_id="exec_FAKE"    → ★ 成功
            attempt_no=0 / -1（工厂拦得住，__init__ 拦不住）  → ★ 成功

        也就是说：**"只能由工厂构造"是一句愿望，不是机制。**
        这与 M81/M82 是同族病 —— 一个横跨两层的保证，只在其中一层实施。
        而它破坏的是账本的**可解释性**：一条声称"我来自某次执行"的
        Observation 可以完全没有 execution_id，于是"这条结论有没有实证"
        在账本上**问不出答案**（判据是"宁可拒绝，不许编造"）。

        ------------------------------------------------------------------
        判据（双向，缺一不可）

            来源是 EXECUTION_RESULT  ⟹  必须绑定 execution_id + attempt_no >= 1
            来源不是 EXECUTION_RESULT ⟹  不许绑定 execution_id

        第二句看着严，其实正是它让第一句有意义：
        如果非执行来源也能挂 execution_id，那么"有 execution_id"就不再能
        推出"它真的来自执行" —— 一条 `human_input` 顺手挂个 id，
        和一条真正的执行产出在账本上**长得一模一样**。

        ------------------------------------------------------------------
        为什么允许 Non-EXECUTION_RESULT 仍然带 attempt_no

        `attempt_no` 是"这是第几次尝试"这种过程性注记（如超时重试的提示），
        单独出现不冒充实证。而 `execution_id` 是**指向实证的指针** ——
        不能空着，也不能乱指。
        """
        source_is_execution = self.source is ObservationSource.EXECUTION_RESULT

        if source_is_execution:
            # 空串与 None 同罪：都表示"没有指向任何一次真实执行"。
            # 只判 `is None` 会被 `execution_id=""` 绕过（探针 case 2 实测）。
            if not self.execution_id:
                raise InvariantViolation(
                    "I-6: source=EXECUTION_RESULT requires a non-empty execution_id; "
                    "an observation claiming to come from an execution must point at one "
                    "(use Observation.from_execution_result to build it)"
                )
            if self.attempt_no is None or self.attempt_no < 1:
                raise InvariantViolation(
                    "I-6: source=EXECUTION_RESULT requires attempt_no >= 1, "
                    f"got {self.attempt_no!r}"
                )
        elif self.execution_id is not None:
            raise InvariantViolation(
                "I-6: only source=EXECUTION_RESULT may carry an execution_id; "
                f"got source={self.source.value!r} with execution_id={self.execution_id!r}. "
                "Letting a non-execution observation point at an execution would make "
                "'has an execution_id' stop meaning 'came from an execution'"
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
