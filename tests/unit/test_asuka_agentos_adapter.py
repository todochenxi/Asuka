"""AgentOS -> Asuka 评测适配层契约测试。"""
from __future__ import annotations

import unittest

from asuka.agentos_adapter import (
    AgentOSAdapterError,
    AgentOSSample,
    evaluate_samples,
    samples_from_loop,
)
from asuka.answers import Answer
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from packages.agent_context.items import knowledge_chunk


class AdapterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.task = TaskItem(
            task_id="redis-expire",
            question="How do I set a timeout on a key?",
            reference_answer="Use EXPIRE key seconds to set a timeout.",
            source_document="redis",
            difficulty="simple",
            evidence=(Evidence("redis:expire"),),
            required_points=(
                RequiredPoint("expire command", ("EXPIRE",)),
                RequiredPoint("timeout", ("set a timeout", "timeout")),
            ),
        )
        self.dataset = Dataset(topic="redis", items=(self.task,))
        self.dataset.resolved = {"redis-expire": ("redis:expire:001",)}

    def sample(
        self,
        *,
        answer: Answer | None = None,
        retrieved=("redis:expire:001", "redis:expire:002"),
        context=None,
        **kwargs,
    ) -> AgentOSSample:
        if context is None:
            context = (
                knowledge_chunk(
                    "redis:expire:001",
                    "EXPIRE key seconds",
                    citation="redis:expire:001",
                    score=0.9,
                ),
            )
        return AgentOSSample(
            agentos_run_id="run_agentos_1",
            execution_id="exec_1",
            task_id="redis-expire",
            question=self.task.question,
            answer=answer or Answer(
                text="Use EXPIRE key seconds to set a timeout.",
                latency_ms=12.5,
                prompt_tokens=50,
                completion_tokens=10,
                cost_usd=0.001,
                citations=("redis:expire:001",),
            ),
            retrieved=tuple(retrieved),
            context=tuple(context),
            **kwargs,
        )


class AgentOSAdapterContractTest(AdapterTestBase):
    def test_converts_sample_to_report_and_asuka_trace(self) -> None:
        result = evaluate_samples(
            (self.sample(),),
            self.dataset,
            retriever="agentos-bm25",
            top_k=2,
            corpus_chunks=428,
            corpus_sha256="corpus-hash",
            dataset_sha256="dataset-hash",
            embedder="lexical",
        )

        self.assertEqual(result.report.answerer, "agentos")
        self.assertFalse(result.report.calibration)
        self.assertEqual(result.report.overall.mean_recall, 1.0)
        self.assertEqual(result.report.overall.n_with_citations, 1)
        self.assertEqual(result.report.overall.fabricated_total, 0)
        self.assertEqual(result.report.overall.context_size, 1.0)
        self.assertEqual(
            [event.kind for event in result.trace.events],
            ["run.started", "retrieval", "generation", "scoring", "run.finished"],
        )
        self.assertEqual(result.trace.identity.embedder, "lexical")
        self.assertEqual(result.trace.events[1].data["kept"], ["redis:expire:001", "redis:expire:002"])
        self.assertEqual(result.trace.events[1].data["context"], ["redis:expire:001"])

    def test_context_snapshot_is_not_used_as_retrieved_set(self) -> None:
        sample = self.sample(retrieved=("redis:expire:001", "redis:expire:002"))
        result = evaluate_samples(
            (sample,), self.dataset, retriever="agentos-bm25", top_k=2
        )

        retrieval = result.trace.events[1]
        self.assertEqual(retrieval.data["kept"], ["redis:expire:001", "redis:expire:002"])
        self.assertEqual(retrieval.data["context"], ["redis:expire:001"])
        self.assertEqual(result.report.items[0].citation.evidence_dropped, ())

    def test_budget_dropped_context_is_preserved_in_score(self) -> None:
        sample = self.sample(
            dropped_budget=(("redis:expire:002", 20, "token budget exhausted (10/50)"),)
        )
        result = evaluate_samples(
            (sample,), self.dataset, retriever="agentos-bm25", top_k=2
        )

        self.assertEqual(
            result.report.items[0].context_dropped,
            (("redis:expire:002", 20, "token budget exhausted (10/50)"),),
        )
        self.assertEqual(result.report.overall.context_dropped_total, 1)
        self.assertEqual(result.trace.events[1].data["dropped_budget"], [
            ["redis:expire:002", 20, "token budget exhausted (10/50)"]
        ])

    def test_citation_outside_available_is_fabricated(self) -> None:
        sample = self.sample(
            answer=Answer(
                text="EXPIRE sets a timeout.",
                citations=("redis:expire:002",),
            )
        )
        result = evaluate_samples(
            (sample,), self.dataset, retriever="agentos-bm25", top_k=2
        )

        self.assertEqual(result.report.overall.fabricated_total, 1)
        self.assertEqual(result.report.items[0].fabricated, ("redis:expire:002",))

    def test_missing_retrieved_is_rejected_instead_of_read_as_empty(self) -> None:
        with self.assertRaises(AgentOSAdapterError) as ctx:
            AgentOSSample(
                agentos_run_id="run_agentos_1",
                execution_id="exec_1",
                task_id="redis-expire",
                question=self.task.question,
                answer=Answer(text="answer"),
                retrieved=None,
            )
        self.assertIn("retrieved", str(ctx.exception))

    def test_unknown_task_is_rejected_instead_of_matching_by_question(self) -> None:
        sample = self.sample()
        sample = AgentOSSample(
            agentos_run_id=sample.agentos_run_id,
            execution_id=sample.execution_id,
            task_id="not-in-dataset",
            question=sample.question,
            answer=sample.answer,
            retrieved=sample.retrieved,
            context=sample.context,
        )
        with self.assertRaises(AgentOSAdapterError) as ctx:
            evaluate_samples((sample,), self.dataset, retriever="agentos-bm25", top_k=2)
        self.assertIn("不在 Asuka Dataset", str(ctx.exception))

    def test_question_drift_is_rejected(self) -> None:
        sample = self.sample()
        sample = AgentOSSample(
            agentos_run_id=sample.agentos_run_id,
            execution_id=sample.execution_id,
            task_id=sample.task_id,
            question="a different question",
            answer=sample.answer,
            retrieved=sample.retrieved,
            context=sample.context,
        )
        with self.assertRaises(AgentOSAdapterError) as ctx:
            evaluate_samples((sample,), self.dataset, retriever="agentos-bm25", top_k=2)
        self.assertIn("question 与 Dataset 不一致", str(ctx.exception))

    def test_samples_from_two_agentos_runs_are_allowed_and_recorded(self) -> None:
        """B2：一题一条 Run 是自然形状，多 Run 必须被接受且**如实记进 trace**。"""
        first = self.sample()
        second = AgentOSSample(
            agentos_run_id="run_agentos_2",
            execution_id="exec_2",
            task_id=first.task_id,
            question=first.question,
            answer=first.answer,
            retrieved=first.retrieved,
            context=first.context,
        )
        result = evaluate_samples(
            (first, second),
            self.dataset,
            retriever="agentos-bm25",
            top_k=2,
            samples_per_task=2,
        )
        started = result.trace.events[0]
        self.assertEqual(started.kind, "run.started")
        self.assertEqual(
            started.data["agentos_run_ids"], ["run_agentos_1", "run_agentos_2"]
        )
        self.assertEqual(started.data["agentos_run_count"], 2)

    def test_unresolved_evidence_is_rejected(self) -> None:
        dataset = Dataset(topic="redis", items=(self.task,))
        with self.assertRaises(AgentOSAdapterError) as ctx:
            evaluate_samples(
                (self.sample(),), dataset, retriever="agentos-bm25", top_k=2
            )
        self.assertIn("还没 resolve", str(ctx.exception))

    def test_extracts_a_real_agentos_llm_execution_without_merging_run_semantics(self) -> None:
        from packages.agent_domain.intelligence.action import Action, ActionType
        from packages.agent_runtime.loop import AgentLoop
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
            ScriptedPlanner,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(nodes=1),
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start(self.task.question)
        action = Action(
            run_id=state.run_id,
            action_type=ActionType.LLM_CALL,
            payload={"prompt": "answer the question"},
        )
        loop.decision_engine = ScriptedDecisionEngine([action])
        self.assertEqual(loop.step().value, "executed")

        samples = samples_from_loop(
            loop,
            task_id_by_execution={loop.trace.of_kind("task.submitted")[0].execution_id: self.task.task_id},
            questions_by_task={self.task.task_id: self.task.question},
            retrieved_by_execution={
                loop.trace.of_kind("task.submitted")[0].execution_id: ("redis:expire:001",)
            },
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].agentos_run_id, state.run_id)
        self.assertEqual(samples[0].answer.text, "the answer is 5")
        self.assertEqual(samples[0].execution_id, loop.trace.of_kind("task.submitted")[0].execution_id)
        self.assertIsNone(samples[0].answer.citations)

    def test_extraction_rejects_missing_retrieval_mapping(self) -> None:
        from packages.agent_domain.intelligence.action import Action, ActionType
        from packages.agent_runtime.loop import AgentLoop
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
            ScriptedPlanner,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(nodes=1),
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start(self.task.question)
        loop.decision_engine = ScriptedDecisionEngine([
            Action(run_id=state.run_id, action_type=ActionType.LLM_CALL, payload={"prompt": "answer"})
        ])
        loop.step()
        execution_id = loop.trace.of_kind("task.submitted")[0].execution_id
        with self.assertRaises(AgentOSAdapterError) as ctx:
            samples_from_loop(
                loop,
                task_id_by_execution={execution_id: self.task.task_id},
                questions_by_task={self.task.task_id: self.task.question},
                retrieved_by_execution={},
            )
        self.assertIn("缺少 retrieved", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
