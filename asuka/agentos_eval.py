"""Asuka 评测的 **AgentOS 侧**：检索 → 带引用回答，跑成一条 AgentOS Run。

--------------------------------------------------------------------------
为什么它在 AgentOS 这一侧

Asuka 不再自带 LLM 客户端（`asuka/deepseek.py` 已删）。模型调用归 AgentOS：
Runtime 的 `ModelGateway` + `LLMCallExecutor`。所以"答题"这件事变成一次真实
的 AgentOS Run —— 有 Harness、有 Kernel、有账本、有成本。

    Run 的每一步：
        1. TOOL_CALL  kb.search   —— 检索（Asuka 的语料 / 检索器）
        2. LLM_CALL   build_prompt(question, 检到的片)  —— 带 [[key]] 引用作答
        3. FINISH

Asuka 侧只剩：语料、检索器、提示词、判据、报告。**谁去调模型不归它管。**

--------------------------------------------------------------------------
一题一条 Run（B2）

Asuka 的评测是 24 道题，所以是 24 条 Run。每条 Run 一份账本；适配器
（`asuka.agentos_adapter`）把它们的样本合起来评一次。

--------------------------------------------------------------------------
检索为什么是**工具**而不是写在决策引擎里

检索会留下 `retrieved`（检到的）与 `available`（过完预算真喂给模型的）两个
不同的集合，而这个区分是引用判据的地基（被预算丢掉的片模型没看见，引用了
它就是编造）。把它做成一次 Tool Execution，这两段就都落在**可审计的执行结果**
里，而不是活在某个对象的私有字段里。
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.agent_context.assembler import ContextAssembler
from packages.agent_context.memory import InMemoryMemoryStore, MemoryManager
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    SideEffect,
    ToolRuntime,
    ToolSpec,
)
from packages.agent_runtime.tool_runtime import ToolRegistry as RuntimeToolRegistry

from .context import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_RESERVED_FOR_OUTPUT,
    assemble,
)
from .prompting import DEFAULT_PROMPT_VERSION, build_prompt, parse_response, system_prompt
from .textutil import CHARS_PER_TOKEN

#: 这条评测 Run 的 agent 身份。
EVAL_AGENT_ID = "asuka-eval"


@dataclass
class _Ctx:
    """`build_prompt` 只要求 `.key` / `.text`（duck-typed）。"""

    key: str
    text: str


# ---------------------------------------------------------------------------
# 工具：检索
# ---------------------------------------------------------------------------


def build_tool_runtime(
    kb: Any,
    *,
    top_k: int = 10,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> ToolRuntime:
    """`kb.search`：检索 + **受预算约束的装配**，返回两个集合 + 丢弃原因。"""

    def _search(args: Mapping[str, Any]) -> Mapping[str, Any]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or top_k)
        started = time.perf_counter()
        result = kb.search(query, limit=limit)
        assembled = assemble(
            kb.pipeline,
            result,
            budget=context_budget,
            reserved_for_output=reserved_for_output,
            chars_per_token=chars_per_token,
        )
        return {
            "query": query,
            # 检索边界留下的（过了 C-9 权限 + C-10 citation，**没**过预算）
            "retrieved": [c.chunk_id for c in result.kept],
            # 真正喂给模型的（过了 C-3 预算）
            "available": [
                {"key": item.key, "text": item.text, "citation": item.reference}
                for item in assembled.items
            ],
            "dropped": [list(x) for x in assembled.dropped_reasons],
            "context_tokens": assembled.total_tokens,
            "retrieval_ms": (time.perf_counter() - started) * 1000.0,
        }

    registry = RuntimeToolRegistry()
    registry.register(
        ToolSpec(
            name="kb.search",
            version="1.0.0",
            description="检索技术文档知识库，返回带 citation 的片段",
            side_effect=SideEffect.READ,
            input_schema={"required": ["query"]},
        ),
        FunctionInvoker(_search),
    )
    return ToolRuntime(registry)


# ---------------------------------------------------------------------------
# 智能体
# ---------------------------------------------------------------------------


class EvalInterpreter:
    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Any:
        from packages.agent_domain.intelligence.goal import Budget, Goal

        return Goal(
            run_id=str(context.get("run_id", "")),
            objective=user_request,
            success_criteria=("an answer is produced",),
            budget=Budget(max_steps=4),
        )


class EvalPlanner:
    def plan(self, state: Any) -> Any:
        from packages.agent_domain.intelligence.plan import Plan, PlanNode

        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n1", name="retrieve-then-answer"),))


def _last_tool_result(state: Any, tool_name: str) -> Mapping[str, Any] | None:
    """从 State 的观测里取最近一次该工具的输出（Tool Execution 的结果）。"""
    for observation in reversed(tuple(getattr(state, "observations", ()) or ())):
        content = observation.content if isinstance(observation.content, Mapping) else {}
        result = content.get("result")
        if not isinstance(result, Mapping) or result.get("tool") != tool_name:
            continue
        inner = result.get("result")
        if isinstance(inner, Mapping):
            return inner
    return None


class EvalDecisionEngine:
    """检索一次 → 带引用作答一次 → 收尾。"""

    def __init__(self, *, top_k: int) -> None:
        self.calls = 0
        self.top_k = top_k

    def progress(self) -> object:
        return {"calls": self.calls}

    def resume(self, progress: object) -> None:
        if not isinstance(progress, Mapping):
            raise TypeError(f"cannot resume from {type(progress).__name__}")
        if "calls" not in progress:
            raise KeyError("calls")
        self.calls = int(progress["calls"])

    def decide(self, state: Any) -> Any:
        from packages.agent_domain.intelligence.action import Action, ActionType
        from packages.agent_domain.intelligence.decision import Decision

        self.calls += 1
        question = state.goal.objective
        if self.calls == 1:
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "kb.search", "args": {"query": question, "limit": self.top_k}},
                rationale="检索与问题相关的文档片段",
            )
        elif self.calls == 2:
            found = _last_tool_result(state, "kb.search") or {}
            contexts = [
                _Ctx(str(c.get("key")), str(c.get("text")))
                for c in found.get("available", ())
                if isinstance(c, Mapping)
            ]
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.LLM_CALL,
                payload={
                    "prompt": build_prompt(question, contexts),
                    # M95：提示词是被测系统的一部分。把 system 也交给 Runtime 的
                    # ContextAssembler —— 于是"这次模型看到的 Context"有一份快照
                    # （`context.built`），而不是只活在决策引擎的私有字符串里。
                    "system": system_prompt(DEFAULT_PROMPT_VERSION),
                },
                rationale="依据检索片段作答（要求自述引用）",
            )
        else:
            action = Action(run_id=state.run_id, action_type=ActionType.FINISH)
        return Decision(run_id=state.run_id, selected_action=action, rationale="asuka-eval")


def build_stack_factory(
    config: Any = None,
    *,
    kb: Any,
    gateway: Any = None,
    top_k: int = 10,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
    kernel: Any = None,
    clock: Any = None,
    child_registry: Any = None,
    snapshots: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    context_snapshots: Any = None,
) -> Any:
    """`(agent_id, approvals) -> RuntimeStack`，供 `InProcessControlPlane` 或 runner 用。"""
    from packages.agent_runtime.assembly import assemble_runtime_stack

    if gateway is None:
        from packages.agent_runtime.model_gateway.deepseek import build_model_gateway

        gateway = build_model_gateway()
    tool_runtime = build_tool_runtime(
        kb,
        top_k=top_k,
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )
    interpreter = EvalInterpreter()
    planner = EvalPlanner()
    # M95：Memory —— 一份**跨 Run 共享**的 MemoryManager。
    # Loop 在完成时写一条 episodic；下面的 recall provider 从**同一份**里读。
    # ⚠️ subject 用 agent_id（不是 run_id），否则每个 Run 各记一份、谁也读不到。
    memory = MemoryManager(InMemoryMemoryStore())

    def make_stack(agent_id: str, approvals: Any) -> Any:
        assembler = ContextAssembler(
            memory_provider=_recall(memory, agent_id),
            **({"snapshots": context_snapshots} if context_snapshots is not None else {}),
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=interpreter,
            planner=planner,
            decision_engine=EvalDecisionEngine(top_k=top_k),
            gateway=gateway,
            tool_runtime=tool_runtime,
            kernel=kernel,
            clock=clock,
            approval_store=approvals,
            context_assembler=assembler,
            memory=memory,
            snapshots=snapshots,
            compensations=compensations,
            cancellations=cancellations,
        )

    return make_stack


def _recall(manager: MemoryManager, subject: str) -> Any:
    """`MemoryManager.recall()` 外面包一层（C-1 的唯一转换口）。"""

    def provider(request: Any) -> Any:
        query = request.messages[-1][1] if request.messages else ""
        return manager.recall(subject=subject, query=query)

    return provider


# ---------------------------------------------------------------------------
# Runner：把整份数据集跑成 Run，再交给适配器评分
# ---------------------------------------------------------------------------


def _extract_sample(stack: Any, task: Any, *, top_k: int) -> Any:
    """从一条已跑完的 Run 里抽出 Asuka 的一个样本。"""
    from packages.agent_context.items import knowledge_chunk

    from .agentos_adapter import AgentOSAdapterError, AgentOSSample
    from .answers import Answer

    loop = stack.loop
    observations = tuple(getattr(loop.state, "observations", ()) or ())

    # LLM 的 execution_id 从**账本**取，不从观测取：生成失败时没有带 response 的
    # 观测，但 `task.submitted` 那条**一定在** —— 失败也要能被归因到一个 execution。
    llm_execution_id = ""
    for entry in getattr(getattr(loop, "trace", None), "entries", ()) or ():
        if getattr(entry, "kind", "") == "task.submitted" and (
            entry.payload or {}
        ).get("action_type") == "llm_call":
            llm_execution_id = str(getattr(entry, "execution_id", "") or "")

    tool_out: Mapping[str, Any] | None = None
    llm_result: Mapping[str, Any] | None = None
    for observation in observations:
        content = observation.content if isinstance(observation.content, Mapping) else {}
        result = content.get("result")
        if not isinstance(result, Mapping):
            continue
        if result.get("tool") == "kb.search" and isinstance(result.get("result"), Mapping):
            tool_out = result["result"]
        response = result.get("response")
        if isinstance(response, Mapping) and response.get("text"):
            llm_result = result
            if not llm_execution_id:
                llm_execution_id = str(observation.execution_id or "")

    if tool_out is None:
        raise AgentOSAdapterError(f"task {task.task_id!r} 的 Run 里没有 kb.search 的执行结果")

    contexts = tuple(
        knowledge_chunk(str(c["key"]), str(c["text"]), citation=str(c["citation"]))
        for c in tool_out.get("available", ())
        if isinstance(c, Mapping)
    )
    status = str(getattr(loop.agent_run, "status").value)

    # ⚠️ 生成失败写 `error`，**不**用空文本冒充"答了但答错"（与 Asuka 同源纪律）。
    # 真模型会偶发网络错（本项目基线上就记过 SSL 中断）——一次抖动不该让整份
    # 24 题的评测作废：那一条记成 error，其余照常评。
    if llm_result is None:
        answer = Answer(text="", error=f"agentos: run {stack.run_id} status={status} 没有 LLM 回答")
    else:
        raw_text = str(llm_result["response"]["text"])
        text, citations = parse_response(raw_text, contexts)
        usage = dict(llm_result.get("usage") or {})
        answer = Answer(
            text=text,
            latency_ms=float(llm_result.get("latency_ms") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(llm_result.get("cost_usd") or 0.0),
            citations=citations,
        )

    return AgentOSSample(
        agentos_run_id=stack.run_id,
        execution_id=llm_execution_id,
        task_id=task.task_id,
        question=task.question,
        answer=answer,
        retrieved=tuple(str(x) for x in tool_out.get("retrieved", ())),
        context=contexts,
        dropped_budget=tuple(
            (str(d[0]), int(d[1]), str(d[2])) for d in tool_out.get("dropped", ())
        ),
        context_tokens_override=int(tool_out.get("context_tokens") or 0),
        retrieval_ms=float(tool_out.get("retrieval_ms") or 0.0),
        agentos_status=status,
    )


def run_evaluation(
    *,
    topic: str,
    corpus_dir: Path,
    datasets_dir: Path,
    retriever: str = "bm25",
    top_k: int = 10,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
    limit: int = 0,
    gateway: Any = None,
    store: Any = None,
    embedder: Any = None,
    allow_non_semantic: bool = False,
) -> Any:
    """跑完整份数据集（一题一条 Run），返回 `AgentOSEvaluation`。"""
    from packages.agent_harness.approval import InMemoryApprovalStore

    from .corpus import read_chunks
    from .dataset import load_dataset
    from .kb import build_knowledge_base
    from .agentos_adapter import evaluate_samples

    chunks = read_chunks(corpus_dir / topic / "chunks.jsonl")
    dataset = load_dataset(datasets_dir, topic)
    dataset.validate(chunks)
    dataset.resolve(chunks)

    kb = build_knowledge_base(
        topic,
        corpus_dir=corpus_dir,
        kind=retriever,
        store=store,
        embedder=embedder,
        allow_non_semantic=allow_non_semantic,
    )
    factory = build_stack_factory(
        kb=kb,
        gateway=gateway,
        top_k=top_k,
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )
    approvals = InMemoryApprovalStore()
    items: Sequence[Any] = dataset.items[:limit] if limit else dataset.items

    samples = []
    for task in items:
        stack = factory(EVAL_AGENT_ID, approvals)
        stack.start(task.question)
        stack.run()
        samples.append(_extract_sample(stack, task, top_k=top_k))

    return evaluate_samples(
        tuple(samples),
        dataset,
        retriever=f"agentos-{retriever}",
        top_k=top_k,
        corpus_chunks=len(chunks),
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
    )


def build_parser() -> Any:
    import argparse

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Asuka 评测（AgentOS 侧）：一题一条 Run，模型调用由 AgentOS 执行"
    )
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument("--retriever", default="bm25", choices=["bm25", "dense"])
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET)
    parser.add_argument("--reserved-for-output", type=int, default=DEFAULT_RESERVED_FOR_OUTPUT)
    parser.add_argument("--chars-per-token", type=int, default=CHARS_PER_TOKEN)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用；0 = 全部）")
    parser.add_argument("--embedder", default="auto", choices=["auto", "api", "local", "hashing"])
    parser.add_argument("--model-path", default="")
    parser.add_argument("--allow-non-semantic", action="store_true")
    parser.add_argument("--corpus-dir", default=str(root / "corpus"))
    parser.add_argument("--datasets-dir", default=str(root / "datasets"))
    parser.add_argument("--out", default=str(root / "runs"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    from .dataset import DatasetError
    from .embedding import EmbeddingError
    from .vectorstore import VectorStoreError

    try:
        return _run(args)
    except (EmbeddingError, VectorStoreError, DatasetError, ValueError) as exc:
        print(f"\n! {exc}", file=sys.stderr)
        return 2


def _run(args: Any) -> int:
    from .answers import render_markdown

    store = None
    embedder = None
    if args.retriever == "dense":
        from .embedding import build_embedder
        from .vectorstore import QdrantStore

        embedder = build_embedder(args.embedder, model_path=args.model_path)
        store = QdrantStore()

    result = run_evaluation(
        topic=args.topic,
        corpus_dir=Path(args.corpus_dir),
        datasets_dir=Path(args.datasets_dir),
        retriever=args.retriever,
        top_k=args.top_k,
        context_budget=args.context_budget,
        reserved_for_output=args.reserved_for_output,
        chars_per_token=args.chars_per_token,
        limit=args.limit,
        store=store,
        embedder=embedder,
        allow_non_semantic=args.allow_non_semantic,
    )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    (out_dir / "answers").mkdir(parents=True, exist_ok=True)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    base = f"{args.topic}-agentos-{args.retriever}-k{args.top_k}-{stamp}"
    result.report.save(out_dir / "answers" / f"{base}.json")
    (out_dir / "answers" / f"{base}.md").write_text(
        render_markdown(result.report), encoding="utf-8"
    )
    result.trace.save(out_dir / "traces" / f"{base}.jsonl")

    overall = result.report.overall
    print(
        f"[{args.topic}/agentos-{args.retriever}] top_k={args.top_k}  样本 {overall.n_samples}"
    )
    print(
        f"  要点召回={overall.mean_recall:.4f}  pass@1={overall.pass_at_1:.4f}  "
        f"每要点字符={overall.chars_per_hit_point:.1f}"
    )
    print(
        f"  上下文：{overall.context_tokens:.0f} / {result.report.context_available} tokens"
        f"（{overall.context_size:.1f} 片）"
    )
    print(f"  产物：{out_dir / 'answers' / base}.json")
    return 0


__all__ = [
    "EVAL_AGENT_ID",
    "build_parser",
    "build_stack_factory",
    "build_tool_runtime",
    "main",
    "run_evaluation",
]


if __name__ == "__main__":
    raise SystemExit(main())
