"""M17：Context / Memory（基线 §21 / §22 / §14）。

    Memory（记住的） / Knowledge（外部资料）
            ↓ 显式转换（C-1）
        ContextItem
            ↓ ContextAssembler（排序 + 取舍 + 预算）
        Context → Model
            ↓
        ContextSnapshot（审计）

覆盖的不变量：

    C-1  Memory / Knowledge / Context / Artifact 是四种对象，转换必须显式
    C-2  Context 是每次模型调用的工作台（一次调用一份 Snapshot）
    C-3  Token Budget 是硬约束；pinned 装不下 → 直接失败，不静默降级
    C-4  静默截断是 bug：被丢的每条都要留原因；分配必须确定
    C-5  ContextSnapshot ≠ Checkpoint（字段互不重叠，可断言）
    C-6  Artifact 只以 reference 进 Context
    C-7  Memory 分层；Qdrant ≠ Truth（索引派生、可重建）
    C-8  Memory 写入可溯源
    C-9  Retrieval 必须过 Permission Filter
    C-10 Knowledge 进 Context 必须带 Citation
    C-11 组装归 Runtime，准入归 Harness
    C-12 排序（语义）与取舍（priority）是两个维度
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from packages.agent_context import (
    AllowAll,
    Chunk,
    ContextAssembler,
    ContextBudgetError,
    ContextItem,
    ContextRequest,
    ContextSnapshot,
    ContextSource,
    DenyAll,
    HeuristicTokenizer,
    InMemoryContextSnapshotStore,
    InMemoryMemoryStore,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    RetrievalPipeline,
    RetrievalQuery,
    TenantFilter,
    TokenBudget,
    allocate,
    knowledge_chunk,
    message,
    system,
    tool_contract,
)
from packages.agent_domain.execution import RunCheckpoint
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.model_gateway import (
    CompletionRequest,
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    ToolRegistry,
    ToolRuntime,
    ToolSpec,
)
from packages.execution_kernel.inmemory import ManualClock


# ---------------------------------------------------------------- 替身
class FixedClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now_ = start or datetime(2026, 1, 1, 0, 0, 0)
        self.ticks = 0

    def now(self) -> datetime:
        return self.now_


class FakeRetriever:
    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self.chunks = list(chunks)

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        return list(self.chunks)


class _RawChunk:
    """duck-typed 的检索结果：故意**不**经过 `Chunk` 的构造校验。"""

    def __init__(self, chunk_id: str, *, citation: str = "", tenant: str = "t1") -> None:
        self.chunk_id = chunk_id
        self.document_id = f"doc-{chunk_id}"
        self.text = f"content of {chunk_id}"
        self.citation = citation
        self.score = 0.5
        self.attributes: dict[str, object] = {"tenant_id": tenant}


def chunk(chunk_id: str, *, tenant: str = "t1", citation: str | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        text=f"content of {chunk_id}",
        # citation=None → 给个默认引用；citation="" → 故意造一条**没有引用**的
        citation=f"s3://docs/{chunk_id}" if citation is None else citation,
        score=0.9,
        attributes={"tenant_id": tenant},
    )


# ---------------------------------------------------------------- Budget
class BudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tok = HeuristicTokenizer(chars_per_token=10)   # 10 字符 = 1 token

    def test_items_that_fit_are_all_kept(self) -> None:
        items = [system("a" * 10), message("user", "b" * 20)]
        plan = allocate(items, TokenBudget(total=100), tokenizer=self.tok)
        self.assertEqual(len(plan.kept), 2)
        self.assertEqual(plan.total_tokens, 3)
        self.assertEqual(plan.dropped, ())

    def test_c3_overflow_drops_the_lowest_priority_first(self) -> None:
        items = [
            ContextItem(ContextSource.KNOWLEDGE, "low", "x" * 50, priority=1,
                        reference="s3://a"),
            ContextItem(ContextSource.KNOWLEDGE, "high", "y" * 50, priority=9,
                        reference="s3://b"),
        ]
        plan = allocate(items, TokenBudget(total=6), tokenizer=self.tok)
        self.assertEqual([i.key for i in plan.kept], ["high"])
        self.assertEqual([d.key for d in plan.dropped], ["low"])

    def test_c4_every_drop_carries_a_reason(self) -> None:
        items = [ContextItem(ContextSource.MEMORY, "m", "z" * 100, priority=0)]
        plan = allocate(items, TokenBudget(total=2), tokenizer=self.tok)
        self.assertEqual(len(plan.dropped), 1)
        self.assertTrue(plan.dropped[0].reason)
        self.assertEqual(plan.dropped[0].source, "memory")
        self.assertEqual(plan.dropped[0].tokens, 10)

    def test_c3_pinned_that_does_not_fit_raises_instead_of_silently_degrading(self) -> None:
        """系统指令被丢掉不是"降级"，是换了一个 Agent —— 那种情况宁可炸掉。"""
        items = [system("s" * 500)]
        with self.assertRaises(ContextBudgetError) as ctx:
            allocate(items, TokenBudget(total=10), tokenizer=self.tok)
        self.assertGreater(ctx.exception.needed, ctx.exception.available)

    def test_c4_allocation_is_deterministic(self) -> None:
        items = [
            ContextItem(ContextSource.MEMORY, f"m{i}", "q" * 30, priority=i)
            for i in range(8)
        ]
        budget = TokenBudget(total=10)
        first = allocate(items, budget, tokenizer=self.tok)
        import random

        shuffled = list(items)
        for seed in range(5):
            random.Random(seed).shuffle(shuffled)
            again = allocate(shuffled, budget, tokenizer=self.tok)
            self.assertEqual([i.key for i in first.kept], [i.key for i in again.kept])
            self.assertEqual([d.key for d in first.dropped], [d.key for d in again.dropped])

    def test_reserved_for_output_is_subtracted(self) -> None:
        """不预留输出位置的后果：上下文刚好塞满窗口，模型一个字都吐不出来。"""
        items = [ContextItem(ContextSource.MEMORY, "m", "a" * 50, priority=0)]   # 5 tokens
        tight = allocate(items, TokenBudget(total=10, reserved_for_output=8), tokenizer=self.tok)
        loose = allocate(items, TokenBudget(total=10), tokenizer=self.tok)
        self.assertEqual(tight.kept, ())            # 只剩 2 个位置，装不下 5
        self.assertEqual(len(loose.kept), 1)        # 同样的 total，不预留就装得下

    def test_budget_rejects_nonsense(self) -> None:
        with self.assertRaises(ValueError):
            TokenBudget(total=0)
        with self.assertRaises(ValueError):
            TokenBudget(total=10, reserved_for_output=10)


# ---------------------------------------------------------------- Items
class ItemTest(unittest.TestCase):
    def test_c10_knowledge_without_citation_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            knowledge_chunk("c1", "some text", citation="")
        self.assertIn("C-10", str(ctx.exception))

    def test_c6_artifact_goes_by_reference_not_content(self) -> None:
        item = ContextItem(
            ContextSource.KNOWLEDGE,
            key="report",
            text="(节选) 2025 Q4 营收 …",      # 只是摘要
            reference="s3://bucket/report.pdf",  # 大对象在这里
        )
        self.assertTrue(item.reference)
        self.assertLess(len(item.text), 100)

    def test_system_and_tool_contracts_are_pinned(self) -> None:
        self.assertTrue(system("...").pinned)
        self.assertTrue(tool_contract("calculator", "...").pinned)


# ---------------------------------------------------------------- Assembler / 排序
class AssemblerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.snapshots = InMemoryContextSnapshotStore()

    def _assembler(self, **kw) -> ContextAssembler:
        return ContextAssembler(
            tokenizer=HeuristicTokenizer(chars_per_token=10),
            budget=TokenBudget(total=kw.pop("total", 200)),
            snapshots=self.snapshots,
            clock=self.clock,
            **kw,
        )

    def test_c12_system_always_comes_first_regardless_of_priority(self) -> None:
        """顺序是语义，priority 只在取舍时用 —— 两者不能混。"""
        build = self._assembler().build(
            ContextRequest(
                run_id="run_1",
                system="you are a careful assistant",
                messages=(("user", "hi"),),
            )
        )
        self.assertEqual(build.items[0].source, ContextSource.SYSTEM)

    def test_render_follows_source_order(self) -> None:
        build = self._assembler().build(
            ContextRequest(
                run_id="run_1",
                system="SYS",
                messages=(("user", "USER"),),
                knowledge=(knowledge_chunk("k1", "KNOW", citation="s3://k"),),
                tools=(tool_contract("calc", "TOOL"),),
            )
        )
        rendered = build.render()
        self.assertEqual(rendered[0], "SYS")
        self.assertEqual(rendered[1], "USER")
        self.assertEqual(rendered[-1], "TOOL")

    def test_c2_one_snapshot_per_call_not_per_run(self) -> None:
        """Run 级单例会让第 5 次调用盖掉第 1 次的 —— 那就查不到"它第一次为什么这么答"。"""
        assembler = self._assembler()
        for i in range(3):
            assembler.build(ContextRequest(run_id="run_1", system=f"sys-{i}"))
        saved = self.snapshots.list_for("run_1")
        self.assertEqual(len(saved), 3)
        self.assertEqual(
            [s.items_of("system")[0].text for s in saved], ["sys-0", "sys-1", "sys-2"]
        )

    def test_snapshot_records_what_was_dropped(self) -> None:
        assembler = self._assembler(total=12)
        build = assembler.build(
            ContextRequest(
                run_id="run_1",
                system="S" * 10,
                memory=(ContextItem(ContextSource.MEMORY, "m", "M" * 500, priority=0),),
            )
        )
        self.assertTrue(build.snapshot.dropped)
        self.assertEqual(build.snapshot.dropped[0].key, "m")

    def test_memory_and_knowledge_providers_are_plug_in_points(self) -> None:
        """§22 的 MemoryAssembler / RetrievalAssembler 就挂在这两个槽上。"""
        assembler = self._assembler(
            memory_provider=lambda req: (
                ContextItem(ContextSource.MEMORY, "mem", "user prefers short answers",
                            priority=40),
            ),
            knowledge_provider=lambda req: (
                knowledge_chunk("k", "from the handbook", citation="s3://h"),
            ),
        )
        build = assembler.build(ContextRequest(run_id="run_1", system="S"))
        sources = {i.source for i in build.items}
        self.assertIn(ContextSource.MEMORY, sources)
        self.assertIn(ContextSource.KNOWLEDGE, sources)


class SnapshotVsCheckpointTest(unittest.TestCase):
    def test_c5_snapshot_has_no_recovery_fields(self) -> None:
        for forbidden in ("current_step", "completed_tasks", "step_id"):
            self.assertNotIn(forbidden, ContextSnapshot.__dataclass_fields__)

    def test_c5_checkpoint_has_no_context_fields(self) -> None:
        for forbidden in ("items", "total_tokens", "dropped", "snapshot_id"):
            self.assertNotIn(forbidden, RunCheckpoint.__dataclass_fields__)


# ---------------------------------------------------------------- Memory
class MemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMemoryStore()
        self.manager = MemoryManager(self.store)

    def test_c7_layers_exist_and_are_distinct(self) -> None:
        self.assertEqual(
            {l.value for l in MemoryLayer},
            {"working", "episodic", "semantic", "procedural"},
        )

    def test_c8_episodic_memory_must_be_traceable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            MemoryRecord(layer=MemoryLayer.EPISODIC, subject="u1", content="did X")
        self.assertIn("C-8", str(ctx.exception))

    def test_subject_is_required_for_tenant_isolation(self) -> None:
        with self.assertRaises(ValueError):
            MemoryRecord(layer=MemoryLayer.SEMANTIC, subject="", content="x")

    def test_c1_recall_returns_context_items_not_memory_records(self) -> None:
        """Memory ≠ Context：两者之间必须有一次显式转换。"""
        self.manager.remember(
            layer=MemoryLayer.SEMANTIC, subject="u1", content="prefers concise answers",
            source_run_id="run_1",
        )
        items = self.manager.recall(subject="u1")
        self.assertTrue(items)
        for item in items:
            self.assertIsInstance(item, ContextItem)
            self.assertIs(item.source, ContextSource.MEMORY)
            # Memory 的内部字段不该泄漏进 Context 的一等字段
            self.assertNotIn("layer", item.text)

    def test_recall_is_scoped_by_subject(self) -> None:
        self.manager.remember(layer=MemoryLayer.SEMANTIC, subject="u1", content="A")
        self.manager.remember(layer=MemoryLayer.SEMANTIC, subject="u2", content="B")
        self.assertEqual(len(self.manager.recall(subject="u1")), 1)
        self.assertEqual(self.manager.recall(subject="u1")[0].attributes["subject"], "u1")

    def test_forgetting_is_supported(self) -> None:
        record = self.manager.remember(
            layer=MemoryLayer.SEMANTIC, subject="u1", content="delete me"
        )
        self.store.delete(record.memory_id)
        self.assertEqual(self.manager.recall(subject="u1"), ())

    def test_c7_store_is_the_truth_source_not_the_index(self) -> None:
        """Qdrant ≠ Truth：索引是派生的，store 才是事实源。"""
        self.manager.remember(layer=MemoryLayer.SEMANTIC, subject="u1", content="fact")
        # 没有 retriever、没有向量，也能查到 —— 索引丢了只是"检索不到"
        self.assertEqual(len(self.store.search(subject="u1")), 1)


# ---------------------------------------------------------------- Retrieval
class RetrievalTest(unittest.TestCase):
    def test_c9_permission_filter_is_required(self) -> None:
        with self.assertRaises(TypeError):
            RetrievalPipeline(retriever=FakeRetriever([]))      # type: ignore[call-arg]

    def test_c9_out_of_tenant_chunks_are_denied_and_recorded(self) -> None:
        pipeline = RetrievalPipeline(
            retriever=FakeRetriever([chunk("a", tenant="t1"), chunk("b", tenant="t2")]),
            permission=TenantFilter(),
        )
        result = pipeline.run(RetrievalQuery(text="q", tenant_id="t1", subject="u1"))
        self.assertEqual([c.chunk_id for c in result.kept], ["a"])
        self.assertEqual([c.chunk_id for c in result.denied], ["b"])

    def test_c9_no_tenant_means_no_results(self) -> None:
        pipeline = RetrievalPipeline(
            retriever=FakeRetriever([chunk("a", tenant="t1")]), permission=TenantFilter()
        )
        result = pipeline.run(RetrievalQuery(text="q", subject="u1"))
        self.assertEqual(result.kept, ())

    def test_c10_chunks_without_citation_are_dropped(self) -> None:
        """故意绕开 `Chunk` 的构造校验 —— 证明 pipeline 自己也会守 C-10。

        `Retriever` 是外部实现，可能返回 dict 或 duck-typed 对象；
        只靠 `Chunk.__post_init__` 的话，换一个 Retriever 就没有引用了。
        """
        pipeline = RetrievalPipeline(
            retriever=FakeRetriever([_RawChunk("a", citation="")]), permission=AllowAll()
        )
        result = pipeline.run(RetrievalQuery(text="q", tenant_id="t1"))
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.dropped_no_citation), 1)

    def test_c10_chunk_constructor_itself_rejects_missing_citation(self) -> None:
        with self.assertRaises(ValueError):
            chunk("a", citation="")

    def test_c9_deny_all_blocks_everything(self) -> None:
        pipeline = RetrievalPipeline(
            retriever=FakeRetriever([chunk("a"), chunk("b")]), permission=DenyAll()
        )
        result = pipeline.run(RetrievalQuery(text="q", tenant_id="t1"))
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.denied), 2)

    def test_c1_chunks_become_context_items_with_citation(self) -> None:
        pipeline = RetrievalPipeline(
            retriever=FakeRetriever([chunk("a")]), permission=AllowAll()
        )
        result = pipeline.run(RetrievalQuery(text="q", tenant_id="t1"))
        items = pipeline.to_context_items(result)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0].source, ContextSource.KNOWLEDGE)
        self.assertTrue(items[0].reference)          # C-10 传递到 Context


# ---------------------------------------------------------------- Loop 接线
class CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def __call__(self, dep: Deployment, request: CompletionRequest):
        self.requests.append(request)
        return ok_response(dep, request, text="ok")


@dataclass
class OneShotDecisionEngine:
    """一次 LLM_CALL，然后 FINISH。"""

    used: bool = False

    def decide(self, state: State):
        from packages.agent_domain.intelligence.action import Action, ActionType
        from packages.agent_domain.intelligence.decision import Decision

        if self.used:
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            )
        self.used = True
        return Decision(
            run_id=state.run_id,
            selected_action=Action(
                run_id=state.run_id,
                action_type=ActionType.LLM_CALL,
                payload={"prompt": "what now?", "system": "you are a careful assistant"},
            ),
        )


class LoopWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.unit.test_agent_loop_full import Interpreter, Planner

        self.provider = CapturingProvider()
        model = Model(model_id="gpt-test", name="test")
        dep = Deployment(deployment_id="gpt-test@p", model_id="gpt-test",
                         provider="scripted")
        self.gateway = ModelGateway(
            ModelRouter([model], [dep]),
            {"scripted": FunctionProvider("scripted", self.provider)},
            max_fallbacks=0,
            default_model_id="gpt-test",
        )
        registry = ToolRegistry()
        registry.register(ToolSpec(name="noop"), FunctionInvoker(lambda args: {"ok": True}),
                          make_default=True)
        self.tool_runtime = ToolRuntime(registry)
        self.snapshots = InMemoryContextSnapshotStore()
        self.assembler = ContextAssembler(
            tokenizer=HeuristicTokenizer(chars_per_token=10),
            budget=TokenBudget(total=500),
            snapshots=self.snapshots,
            clock=FixedClock(),
        )
        self.interpreter, self.planner = Interpreter(), Planner()

    def _stack(self):
        from packages.agent_runtime.assembly import assemble_runtime_stack

        return assemble_runtime_stack(
            agent_id="agent-ctx",
            interpreter=self.interpreter,
            planner=self.planner,
            decision_engine=OneShotDecisionEngine(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=ManualClock(),
            context_assembler=self.assembler,
        )

    def test_context_reaches_the_provider(self) -> None:
        """C-11：Context 由 Runtime 组装，并真的送到模型调用里。"""
        stack = self._stack()
        stack.start("hello")
        stack.run()

        self.assertEqual(len(self.provider.requests), 1)
        request = self.provider.requests[0]
        self.assertTrue(request.context)
        self.assertIn("you are a careful assistant", request.context[0])

    def test_context_build_is_traced_and_snapshot_is_kept(self) -> None:
        from packages.agent_runtime.trace import CONTEXT

        stack = self._stack()
        stack.start("hello")
        stack.run()

        self.assertIn(CONTEXT, stack.trace.kinds())
        snapshots = self.snapshots.list_for(stack.run_id)
        self.assertEqual(len(snapshots), 1)

    def test_c11_assembler_is_optional(self) -> None:
        """没有配置 Context 时按老样子只发 prompt —— 不留半吊子分支。"""
        from packages.agent_runtime.assembly import assemble_runtime_stack

        stack = assemble_runtime_stack(
            agent_id="agent-ctx",
            interpreter=self.interpreter,
            planner=self.planner,
            decision_engine=OneShotDecisionEngine(),
            gateway=self.gateway,
            tool_runtime=self.tool_runtime,
            clock=ManualClock(),
        )
        stack.start("hello")
        stack.run()
        self.assertEqual(self.provider.requests[0].context, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
