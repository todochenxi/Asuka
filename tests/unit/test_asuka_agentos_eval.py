"""Asuka 评测的 AgentOS 侧：检索 → 带引用回答（一题一条 Run）。

不发网络请求：模型用确定性的 `FunctionProvider` 替身，它回一段 JSON，
于是整条链路（Tool Execution → LLM Execution → 从观测里抽样本）都被走一遍。
"""
from __future__ import annotations

import unittest

from asuka.agentos_eval import (
    EVAL_AGENT_ID,
    _extract_sample,
    build_stack_factory,
)
from asuka.dataset import Evidence, RequiredPoint, TaskItem
from asuka.kb import KnowledgeBase, PublicCorpusFilter
from packages.agent_context.retrieval import Chunk, RetrievalPipeline
from packages.agent_harness.approval import InMemoryApprovalStore
from packages.agent_runtime.model_gateway import (
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)


class _Retriever:
    def __init__(self, chunks) -> None:
        self._chunks = tuple(chunks)

    def search(self, query):
        return list(self._chunks[: query.limit])


def _kb(chunks) -> KnowledgeBase:
    retriever = _Retriever(chunks)
    return KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=retriever, permission=PublicCorpusFilter()),
        retriever=retriever,
        kind="bm25",
    )


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=cid.rsplit(":", 1)[0],
        text=text,
        citation=f"Redis · {cid}",
        score=1.0,
        attributes={"visibility": "public"},
    )


def _gateway(text: str) -> ModelGateway:
    model = Model(model_id="fake")
    deployment = Deployment(
        deployment_id="fake@in-process", model_id="fake", provider="fake"
    )

    def complete(dep, request):
        return ok_response(
            dep,
            request,
            text=text,
            prompt_tokens=11,
            completion_tokens=7,
            latency_ms=42,
            metadata={"cost_usd": 0.0002},
        )

    return ModelGateway(
        ModelRouter([model], [deployment]),
        {"fake": FunctionProvider("fake", complete)},
        max_fallbacks=0,
        default_model_id="fake",
    )


def _task() -> TaskItem:
    return TaskItem(
        task_id="redis-expire",
        question="How do I set a timeout on a key?",
        reference_answer="Use EXPIRE key seconds to set a timeout.",
        source_document="redis",
        difficulty="simple",
        evidence=(Evidence("redis:expire"),),
        required_points=(RequiredPoint("expire command", ("EXPIRE",)),),
    )


def _run(kb, gateway, *, top_k=2, budget=8192, reserved=1024):
    factory = build_stack_factory(
        kb=kb,
        gateway=gateway,
        top_k=top_k,
        context_budget=budget,
        reserved_for_output=reserved,
    )
    stack = factory(EVAL_AGENT_ID, InMemoryApprovalStore())
    stack.start(_task().question)
    stack.run()
    return _extract_sample(stack, _task(), top_k=top_k)


class EvalAgentTest(unittest.TestCase):
    def test_it_retrieves_then_answers_with_self_reported_citations(self) -> None:
        kb = _kb([_chunk("redis:expire:001", "EXPIRE key seconds sets a timeout")])
        gateway = _gateway(
            '{"answer":"Use EXPIRE key seconds to set a timeout.",'
            '"citations":["redis:expire:001"]}'
        )
        sample = _run(kb, gateway)

        self.assertEqual(sample.task_id, "redis-expire")
        self.assertEqual(sample.answer.text, "Use EXPIRE key seconds to set a timeout.")
        self.assertEqual(sample.answer.citations, ("redis:expire:001",))
        self.assertEqual(sample.retrieved, ("redis:expire:001",))
        self.assertEqual(sample.available, ("redis:expire:001",))
        # 成本/延迟来自 Provider —— Executor 必须把它们搬进结果（M94）。
        self.assertEqual(sample.answer.prompt_tokens, 11)
        self.assertEqual(sample.answer.completion_tokens, 7)
        self.assertEqual(sample.answer.cost_usd, 0.0002)
        self.assertEqual(sample.answer.latency_ms, 42.0)

    def test_a_tool_result_is_not_mistaken_for_an_answer(self) -> None:
        """工具回的是检索结果，不是回答 —— 抽取必须挑到 LLM 那一条。"""
        kb = _kb([_chunk("redis:expire:001", "EXPIRE key seconds")])
        gateway = _gateway('{"answer":"ok","citations":[]}')
        sample = _run(kb, gateway)
        self.assertEqual(sample.answer.text, "ok")

    def test_budget_dropping_keeps_retrieved_and_available_distinct(self) -> None:
        """预算装不下第二片：`retrieved` 仍是两片，`available` 只剩一片。"""
        kb = _kb(
            [
                _chunk("redis:expire:001", "short"),
                _chunk("redis:expire:002", "x" * 400),
            ]
        )
        gateway = _gateway('{"answer":"a","citations":[]}')
        sample = _run(kb, gateway, top_k=2, budget=40, reserved=0)

        self.assertEqual(sample.retrieved, ("redis:expire:001", "redis:expire:002"))
        self.assertEqual(sample.available, ("redis:expire:001",))
        self.assertEqual(len(sample.dropped_budget), 1)
        self.assertEqual(sample.dropped_budget[0][0], "redis:expire:002")

    def test_a_citation_it_was_not_given_is_fabricated(self) -> None:
        """自述引用里出现没给它的 id —— 解析保留原样，交给引用判据记成编造。"""
        kb = _kb([_chunk("redis:expire:001", "EXPIRE key seconds")])
        gateway = _gateway('{"answer":"a","citations":["redis:ghost:999"]}')
        sample = _run(kb, gateway)
        self.assertEqual(sample.answer.citations, ("redis:ghost:999",))


class _FakeObservation:
    def __init__(self, content, execution_id="") -> None:
        self.content = content
        self.execution_id = execution_id


class _FakeEntry:
    def __init__(self, kind, payload, execution_id) -> None:
        self.kind = kind
        self.payload = payload
        self.execution_id = execution_id


class _FakeStack:
    """只有 `_extract_sample` 需要的那几个字段。"""

    def __init__(self, observations) -> None:
        import types

        self.run_id = "run_fake"
        self.loop = types.SimpleNamespace(
            state=types.SimpleNamespace(observations=list(observations)),
            agent_run=types.SimpleNamespace(status=types.SimpleNamespace(value="failed")),
            trace=types.SimpleNamespace(
                entries=[
                    _FakeEntry(
                        "task.submitted", {"action_type": "llm_call"}, "exec_llm"
                    )
                ]
            ),
        )


class MissingLlmAnswerTest(unittest.TestCase):
    def test_a_run_without_an_llm_answer_becomes_an_error_sample(self) -> None:
        """一次生成失败记成 `error`，不抛异常、也不冒充空答案（评测不该整体作废）。"""
        tool_out = {
            "result": {
                "tool": "kb.search",
                "result": {
                    "query": "q",
                    "retrieved": ["redis:expire:001"],
                    "available": [{"key": "redis:expire:001", "text": "t", "citation": "c"}],
                    "dropped": [],
                    "context_tokens": 3,
                    "retrieval_ms": 0.5,
                },
            }
        }
        stack = _FakeStack([_FakeObservation(tool_out, execution_id="exec_tool")])
        sample = _extract_sample(stack, _task(), top_k=2)

        self.assertEqual(sample.answer.text, "")
        self.assertIn("没有 LLM 回答", sample.answer.error)
        self.assertEqual(sample.retrieved, ("redis:expire:001",))
        self.assertEqual(sample.agentos_status, "failed")


if __name__ == "__main__":
    unittest.main()
