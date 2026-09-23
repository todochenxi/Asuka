"""M92 探针：展示 AgentOS 样本如何进入 Asuka，而不混合两套 Run 语义。"""
from __future__ import annotations

from asuka.agentos_adapter import AgentOSSample, evaluate_samples
from asuka.answers import Answer
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from packages.agent_context.items import knowledge_chunk


def main() -> None:
    item = TaskItem(
        task_id="redis-expire",
        question="How do I set a timeout on a key?",
        reference_answer="Use EXPIRE key seconds to set a timeout.",
        source_document="redis",
        difficulty="simple",
        evidence=(Evidence("redis:expire"),),
        required_points=(
            RequiredPoint("expire", ("EXPIRE",)),
            RequiredPoint("timeout", ("timeout",)),
        ),
    )
    dataset = Dataset(topic="redis", items=(item,))
    dataset.resolved = {item.task_id: ("redis:expire:001",)}
    sample = AgentOSSample(
        agentos_run_id="run_agentos_probe96",
        execution_id="exec_probe96",
        task_id=item.task_id,
        question=item.question,
        answer=Answer(
            text=item.reference_answer,
            latency_ms=18.0,
            prompt_tokens=120,
            completion_tokens=12,
            cost_usd=0.002,
            citations=("redis:expire:001",),
        ),
        retrieved=("redis:expire:001", "redis:expire:002"),
        context=(
            knowledge_chunk(
                "redis:expire:001",
                "EXPIRE key seconds",
                citation="redis:expire:001",
                score=0.9,
            ),
        ),
        dropped_budget=(("redis:expire:002", 40, "token budget exhausted (80/120)"),),
        retrieval_ms=0.4,
    )
    result = evaluate_samples(
        (sample,),
        dataset,
        retriever="agentos-bm25",
        top_k=2,
        corpus_chunks=428,
        corpus_sha256="corpus-probe96",
        dataset_sha256="dataset-probe96",
        embedder="lexical",
    )
    print("AgentOS run:", sample.agentos_run_id)
    print("Asuka answerer:", result.report.answerer)
    print("mean recall:", result.report.overall.mean_recall)
    print("grounded:", result.report.overall.grounded_rate)
    print("retrieved:", result.trace.events[1].data["kept"])
    print("available:", result.trace.events[1].data["context"])
    print("dropped:", result.trace.events[1].data["dropped_budget"])
    print("trace kinds:", [event.kind for event in result.trace.events])


if __name__ == "__main__":
    main()
