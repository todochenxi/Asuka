"""AgentOS -> Asuka 评测适配层。

这层只做一件事：把真实 AgentOS Run 中已经发生的事实，转换成
Asuka 的答案级样本与评测 Trace。它不把 Asuka 的 task/sample Run
伪装成 AgentOS 的 AgentRun，也不把 Asuka 的评测判据塞回 Runtime。

一次评测可以接**多条** AgentOS Run（B2 / M94）：Asuka 的评测是 24 道题，
一题一条 Run 是最自然的形状 —— 每 Run 一份账本、一条 trace。参与本次
评测的 Run 会全部记进 `run.started.agentos_run_ids`。

边界：

    AgentOS Runtime / Kernel
        -> AgentOSSample（显式交接契约）
        -> AnswerReport + Asuka Trace

`retrieved` 必须由 AgentOS 的检索边界显式提供；不能从 ContextSnapshot
反推。ContextSnapshot 只知道模型**实际看到**的 `available`，不知道检索器
曾经留下但后来被预算丢掉的 `retrieved`。把两者混成一个会把预算问题误报成
检索问题，所以缺失时适配器直接拒绝。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from packages.agent_domain.execution.task import Task
from packages.agent_context.items import ContextItem
from packages.agent_context.snapshot import ContextSnapshot

from .answers import (
    Answer,
    AnswerGroup,
    AnswerReport,
    AnswerScore,
    CitationVerdict,
    _group,
    score_answer,
    score_citations,
)
from .context import DEFAULT_CONTEXT_BUDGET, DEFAULT_RESERVED_FOR_OUTPUT
from .dataset import Dataset, TaskItem
from .textutil import CHARS_PER_TOKEN
from .trace import RunIdentity, Trace


class AgentOSAdapterError(ValueError):
    """AgentOS 交接数据不足或自相矛盾。"""


@dataclass(frozen=True)
class AgentOSSample:
    """AgentOS 给 Asuka 的**显式**一次答案样本。

    `retrieved` 是检索边界留下的 chunk_id；`context` 是真正喂给模型的
    ContextItem。两者不能省略其一：前者用于检索归因，后者用于引用硬判据。
    """

    agentos_run_id: str
    execution_id: str
    task_id: str
    question: str
    answer: Answer
    retrieved: tuple[str, ...]
    context: tuple[ContextItem, ...] = ()
    dropped_budget: tuple[tuple[str, int, str], ...] = ()
    context_tokens_override: int | None = None
    retrieval_ms: float = 0.0
    #: AgentOS 实际终态；只写入 Asuka trace，不改变 Asuka 的 Run 语义。
    agentos_status: str = "completed"

    def __post_init__(self) -> None:
        required = {
            "agentos_run_id": self.agentos_run_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "question": self.question,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise AgentOSAdapterError(
                f"AgentOS sample 缺少必填交接字段：{missing}；"
                "不能把缺字段读成空答案或空检索。"
            )
        if self.retrieved is None:
            raise AgentOSAdapterError(
                f"sample {self.execution_id!r} 没有 retrieved 集合；"
                "ContextSnapshot 只能证明 available，不能反推检索器留下了什么。"
            )
        if self.retrieval_ms < 0:
            raise AgentOSAdapterError("retrieval_ms 必须 >= 0")

    @property
    def available(self) -> tuple[str, ...]:
        """模型实际看到的 chunk_id（预算之后）。"""
        return tuple(item.key for item in self.context if item.source.value == "knowledge")

    @property
    def context_tokens(self) -> int:
        return (
            self.context_tokens_override
            if self.context_tokens_override is not None
            else sum(item.tokens() for item in self.context)
        )


@dataclass(frozen=True)
class AgentOSEvaluation:
    """一次 AgentOS Run 对应的 Asuka 评测产物。"""

    report: AnswerReport
    trace: Trace


def _task_map(dataset: Dataset) -> dict[str, TaskItem]:
    result: dict[str, TaskItem] = {}
    duplicates: list[str] = []
    for item in dataset.items:
        if item.task_id in result:
            duplicates.append(item.task_id)
        result[item.task_id] = item
    if duplicates:
        raise AgentOSAdapterError(f"数据集 task_id 重复：{sorted(set(duplicates))}")
    return result


def _status_for_trace(samples: Sequence[AgentOSSample]) -> str:
    statuses = {sample.agentos_status for sample in samples}
    if not statuses:
        return "failed"
    if statuses == {"completed"}:
        return "completed"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    return sorted(statuses)[0]


def _score_sample(sample: AgentOSSample, item: TaskItem, *, resolved: tuple[str, ...]) -> AnswerScore:
    hits, missed, hit_by = score_answer(item, sample.answer.text)
    verdict = score_citations(
        sample.answer.citations,
        available=sample.available,
        retrieved=sample.retrieved,
        evidence=resolved,
    )
    return AnswerScore(
        task_id=item.task_id,
        difficulty=item.difficulty,
        question=item.question,
        points_total=len(item.required_points),
        hits=hits,
        missed=missed,
        hit_by=hit_by,
        answer_chars=len(sample.answer.text),
        latency_ms=sample.answer.latency_ms,
        retrieval_ms=sample.retrieval_ms,
        prompt_tokens=sample.answer.prompt_tokens,
        completion_tokens=sample.answer.completion_tokens,
        cost_usd=sample.answer.cost_usd,
        error=sample.answer.error,
        out_of_corpus=tuple(item.out_of_corpus),
        citation=verdict,
        context_tokens=sample.context_tokens,
        context_size=len(sample.context),
        context_dropped=sample.dropped_budget,
    )


def _emit_sample(trace: Trace, sample: AgentOSSample, item: TaskItem, score: AnswerScore) -> None:
    """写 Asuka 的三步事件；正文只存答案，不复制知识片正文。"""
    trace.emit(
        "retrieval",
        task_id=sample.task_id,
        query=sample.question,
        limit=len(sample.retrieved),
        kept=list(sample.retrieved),
        citations=[],
        denied=[],
        dropped_no_citation=[],
        context=list(sample.available),
        dropped_budget=[list(x) for x in sample.dropped_budget],
        context_tokens=sample.context_tokens,
        latency_ms=sample.retrieval_ms,
    )
    trace.emit(
        "generation",
        task_id=sample.task_id,
        answerer="agentos",
        text=sample.answer.text,
        chars=len(sample.answer.text),
        latency_ms=sample.answer.latency_ms,
        prompt_tokens=sample.answer.prompt_tokens,
        completion_tokens=sample.answer.completion_tokens,
        cost_usd=sample.answer.cost_usd,
        error=sample.answer.error,
        citations=None if sample.answer.citations is None else list(sample.answer.citations),
    )
    trace.emit(
        "scoring",
        task_id=sample.task_id,
        points_total=score.points_total,
        hits=list(score.hits),
        missed=list(score.missed),
        hit_by=[list(x) for x in score.hit_by],
        out_of_corpus=list(score.out_of_corpus),
        fabricated=list(score.fabricated),
        evidence_ignored=list(score.citation.evidence_ignored),
        evidence_not_retrieved=list(score.citation.evidence_not_retrieved),
        evidence_dropped=list(score.citation.evidence_dropped),
    )


def evaluate_samples(
    samples: Sequence[AgentOSSample],
    dataset: Dataset,
    *,
    retriever: str,
    top_k: int,
    samples_per_task: int = 1,
    corpus_chunks: int = 0,
    corpus_sha256: str = "",
    dataset_sha256: str = "",
    embedder: str = "",
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
    elapsed_ms: float | None = None,
) -> AgentOSEvaluation:
    """把 AgentOS 样本评成 Asuka 报告，并同时生成可核对 Trace。

    要求 `dataset.resolved` 已经准备好，与 `asuka.answers.evaluate_answers`
    相同；适配器不擅自把 Evidence 当 chunk_id，也不在缺失时按空处理。
    """
    if not samples:
        raise AgentOSAdapterError("没有 AgentOS 样本；不能生成一份看起来完整的空报告")
    if not retriever:
        raise AgentOSAdapterError("retriever 身份不能为空")
    if top_k < 1 or samples_per_task < 1:
        raise AgentOSAdapterError("top_k 与 samples_per_task 必须 >= 1")
    if not dataset.resolved:
        raise AgentOSAdapterError(
            "数据集还没 resolve（evidence 未解析成 chunk_id）；拒绝按空 evidence 评测"
        )
    if context_budget <= 0 or reserved_for_output < 0 or reserved_for_output >= context_budget:
        raise AgentOSAdapterError("context_budget / reserved_for_output 不合法")

    items = _task_map(dataset)
    scores: list[AnswerScore] = []
    trace_identity = RunIdentity(
        topic=dataset.topic,
        retriever=retriever,
        top_k=top_k,
        samples_per_task=samples_per_task,
        corpus_chunks=corpus_chunks,
        answerer="agentos",
        corpus_sha256=corpus_sha256,
        dataset_sha256=dataset_sha256,
        embedder=embedder,
        answerer_is_calibration=False,
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )
    trace = Trace(identity=trace_identity)
    # B2（M94）：一次评测可以接**多条** AgentOS Run —— 一题一条 Run 是自然形状，
    # 每 Run 一份账本。这里把参与本次评测的 Run 全部记下来（去重排序，稳定可复现），
    # 而不是像以前那样只认第一条、看到第二条就拒绝。
    run_ids = sorted({sample.agentos_run_id for sample in samples})
    trace.emit(
        "run.started",
        **trace_identity.as_dict(),
        agentos_run_ids=run_ids,
        agentos_run_count=len(run_ids),
        source="agentos",
    )

    counts: dict[str, int] = {}
    for sample in samples:
        item = items.get(sample.task_id)
        if item is None:
            raise AgentOSAdapterError(
                f"AgentOS sample {sample.execution_id!r} 的 task_id {sample.task_id!r} "
                "不在 Asuka Dataset；不允许用 question 文本猜题目身份"
            )
        if sample.question != item.question:
            raise AgentOSAdapterError(
                f"task {sample.task_id!r} 的 question 与 Dataset 不一致；"
                "这会让答案分数挂到错误的题上"
            )
        resolved = dataset.resolved.get(item.task_id)
        if resolved is None:
            raise AgentOSAdapterError(
                f"task {item.task_id!r} 没有 resolved evidence；缺失不等于空 evidence"
            )
        counts[item.task_id] = counts.get(item.task_id, 0) + 1
        score = _score_sample(sample, item, resolved=resolved)
        scores.append(score)
        _emit_sample(trace, sample, item, score)

    wrong_counts = {task_id: n for task_id, n in counts.items() if n != samples_per_task}
    if wrong_counts:
        raise AgentOSAdapterError(
            f"samples_per_task={samples_per_task}，但 AgentOS Run 的题级样本数不一致：{wrong_counts}"
        )

    by_diff: dict[str, AnswerGroup] = {}
    for level in ("simple", "medium", "hard"):
        subset = tuple(score for score in scores if score.difficulty == level)
        if subset:
            by_diff[level] = _group(subset, samples_per_task=samples_per_task)

    report = AnswerReport(
        topic=dataset.topic,
        answerer="agentos",
        retriever=retriever,
        top_k=top_k,
        samples_per_task=samples_per_task,
        items=tuple(scores),
        overall=_group(scores, samples_per_task=samples_per_task),
        by_difficulty=by_diff,
        corpus_chunks=corpus_chunks,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        calibration=False,
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )
    trace.emit(
        "run.finished",
        status=_status_for_trace(samples),
        n_samples=len(scores),
        elapsed_ms=(elapsed_ms if elapsed_ms is not None else sum(s.answer.latency_ms for s in samples)),
    )
    trace.verify()
    trace.verify_against(report)
    return AgentOSEvaluation(report=report, trace=trace)


def samples_from_loop(
    loop: Any,
    *,
    task_id_by_execution: Mapping[str, str],
    questions_by_task: Mapping[str, str],
    retrieved_by_execution: Mapping[str, Sequence[str]],
) -> tuple[AgentOSSample, ...]:
    """从已完成的 AgentOS Loop 提取 LLM 样本。

    这是生产接入的薄适配器：Execution / Attempt / ContextSnapshot 仍由
    AgentOS 持有；Asuka 只拿到一次答案评测需要的事实。检索结果必须由调用方
    显式传入，因为当前 AgentOS Trace 只记 ContextSnapshot id，不记检索器
    被预算丢弃前的 `kept` 集合。
    """
    if loop.state is None or loop.agent_run is None:
        raise AgentOSAdapterError("AgentLoop 尚未 start，不能提取评测样本")
    submissions = [
        entry
        for entry in loop.trace.entries
        if entry.kind == "task.submitted" and entry.payload.get("action_type") == "llm_call"
    ]
    observed = {
        entry.execution_id: entry
        for entry in loop.trace.entries
        if entry.kind == "execution.observed"
    }
    result: list[AgentOSSample] = []
    for entry in submissions:
        execution_id = entry.execution_id
        task_id = task_id_by_execution.get(execution_id)
        if not task_id:
            raise AgentOSAdapterError(
                f"Execution {execution_id!r} 没有 Asuka task_id 映射；"
                "不允许用 execution_id 猜 Dataset 身份"
            )
        question = questions_by_task.get(task_id)
        if not question:
            raise AgentOSAdapterError(f"Asuka task {task_id!r} 没有 question 映射")
        if execution_id not in retrieved_by_execution:
            raise AgentOSAdapterError(
                f"Execution {execution_id!r} 缺少 retrieved；"
                "ContextSnapshot 不能替代检索结果"
            )

        task: Task = loop.kernel.task_of(execution_id)
        payload = dict(task.payload)
        snapshot: ContextSnapshot | None = None
        snapshot_id = str(payload.get("context_snapshot_id") or "")
        if snapshot_id:
            assembler = getattr(loop, "context_assembler", None)
            store = getattr(assembler, "snapshots", None) if assembler is not None else None
            snapshot = store.get(snapshot_id) if store is not None else None
            if snapshot is None:
                raise AgentOSAdapterError(
                    f"Execution {execution_id!r} 指向 ContextSnapshot {snapshot_id!r}，"
                    "但快照不可读；拒绝把 context 当成空"
                )

        observed_entry = observed.get(execution_id)
        attempt_no = observed_entry.attempt_no if observed_entry is not None else entry.attempt_no
        attempts = getattr(loop.kernel, "attempts", None)
        attempt = attempts.get(execution_id, attempt_no) if attempts is not None else None
        raw_result = dict(getattr(attempt, "result", {}) or {}) if attempt is not None else {}
        usage = dict(raw_result.get("usage") or {})
        response = raw_result.get("response")
        response_text = response.get("text") if isinstance(response, Mapping) else ""
        answer = Answer(
            text=str(raw_result.get("text") or response_text or raw_result.get("content") or ""),
            latency_ms=float(raw_result.get("latency_ms") or 0.0),
            prompt_tokens=int(raw_result.get("prompt_tokens") or usage.get("prompt_tokens") or 0),
            completion_tokens=int(
                raw_result.get("completion_tokens") or usage.get("completion_tokens") or 0
            ),
            cost_usd=float(raw_result.get("cost_usd") or 0.0),
            error=str(raw_result.get("error") or ""),
            citations=(
                None
                if raw_result.get("citations") is None
                else tuple(str(x) for x in raw_result.get("citations") or ())
            ),
        )
        result.append(
            AgentOSSample(
                agentos_run_id=loop.agent_run.run_id,
                execution_id=execution_id,
                task_id=task_id,
                question=question,
                answer=answer,
                retrieved=tuple(str(x) for x in retrieved_by_execution[execution_id]),
                context=tuple(snapshot.items) if snapshot is not None else (),
                context_tokens_override=(snapshot.total_tokens if snapshot is not None else None),
                dropped_budget=(
                    tuple((d.key, d.tokens, d.reason) for d in snapshot.dropped)
                    if snapshot is not None
                    else ()
                ),
                retrieval_ms=float(payload.get("retrieval_ms") or 0.0),
                agentos_status=loop.agent_run.status.value,
            )
        )
    if not result:
        raise AgentOSAdapterError("AgentOS Run 没有可评测的 LLM_CALL")
    return tuple(result)
