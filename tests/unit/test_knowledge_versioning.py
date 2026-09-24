"""M106 · M5 Knowledge Versioning（基线 §21 / §8.3）。

补的空洞：Knowledge 被定义成"客观、版本化的资料"，但全仓此前没有"版本"这个概念
—— 一次检索拿到的片说不出自己属于哪一版，也拦不住旧版本的片混进锁定检索。

三条不变量：
    K-1  没有版本声明的片不许被当成锁定版本里的
    K-2  锁定检索绝不返回别的版本的片（抛 VersionMismatch，不静默混入）
    K-3  未锁定检索解析到**当前**版本；没有当前版本 → 拒
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Sequence

from packages.agent_context import (
    AllowAll,
    Chunk,
    ContextSnapshot,
    KnowledgeVersion,
    KnowledgeVersionRegistry,
    NoCurrentVersion,
    RetrievalPipeline,
    RetrievalQuery,
    TenantFilter,
    UnknownVersion,
    VersionedRetriever,
    VersionMismatch,
    build_snapshot,
)
from packages.execution_kernel import ManualClock


def _version(vid: str) -> KnowledgeVersion:
    return KnowledgeVersion(vid, created_at=datetime(2026, 9, 24, tzinfo=timezone.utc))


def _chunk(cid: str, version: str = "", *, tenant: str = "acme", score: float = 1.0) -> Chunk:
    attrs: dict[str, Any] = {"tenant_id": tenant}
    if version:
        attrs["knowledge_version"] = version
    return Chunk(
        chunk_id=cid, document_id="doc", text=f"t-{cid}", citation=f"cite:{cid}",
        score=score, attributes=attrs,
    )


class _RecordingRetriever:
    """记录底层拿到的 query，返回脚本化的片 —— 用来验证 push-down。"""

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self._chunks = list(chunks)
        self.queries: list[RetrievalQuery] = []

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        self.queries.append(query)
        return self._chunks[: query.limit]


class VersionTest(unittest.TestCase):
    def test_version_id_is_required(self) -> None:
        with self.assertRaises(ValueError):
            KnowledgeVersion("", created_at=datetime.now(timezone.utc))


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = KnowledgeVersionRegistry()

    def test_the_first_registered_version_becomes_current(self) -> None:
        self.registry.register(_version("v1"))
        self.registry.register(_version("v2"))
        self.assertEqual(self.registry.current().version_id, "v1")

    def test_switching_current_is_explicit(self) -> None:
        self.registry.register(_version("v1"))
        self.registry.register(_version("v2"))
        self.registry.set_current("v2")
        self.assertEqual(self.registry.current().version_id, "v2")

    def test_a_duplicate_version_is_refused(self) -> None:
        self.registry.register(_version("v1"))
        with self.assertRaises(ValueError):
            self.registry.register(_version("v1"))

    def test_resolving_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(UnknownVersion):
            self.registry.resolve("ghost")

    def test_setting_current_to_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(UnknownVersion):
            self.registry.set_current("ghost")

    def test_current_without_any_version_is_refused(self) -> None:
        with self.assertRaises(NoCurrentVersion):
            self.registry.current()


class VersionedRetrieverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = KnowledgeVersionRegistry()
        self.registry.register(_version("v1"))
        self.registry.register(_version("v2"), make_current=True)

    def _retriever(self, chunks: Sequence[Chunk]) -> tuple[VersionedRetriever, _RecordingRetriever]:
        base = _RecordingRetriever(chunks)
        return VersionedRetriever(retriever=base, registry=self.registry), base

    def test_unpinned_retrieval_resolves_to_current(self) -> None:
        retriever, base = self._retriever([_chunk("a", "v2")])
        retriever.search(RetrievalQuery(text="q", limit=5))
        self.assertEqual(retriever.resolved_version().version_id, "v2")

    def test_it_pushes_the_version_down_to_the_base_retriever(self) -> None:
        retriever, base = self._retriever([_chunk("a", "v2")])
        retriever.search(RetrievalQuery(text="q", limit=5))
        self.assertEqual(base.queries[0].filters["knowledge_version"], "v2")

    def test_a_pinned_retrieval_returns_that_versions_chunks(self) -> None:
        base = _RecordingRetriever([_chunk("a", "v1")])
        retriever = VersionedRetriever(
            retriever=base, registry=self.registry, version_id="v1"
        )
        out = retriever.search(RetrievalQuery(text="q", limit=5))
        self.assertEqual([c.chunk_id for c in out], ["a"])
        # push-down 带着被锁定的版本下去
        self.assertEqual(base.queries[0].filters["knowledge_version"], "v1")

    def test_k1_a_chunk_without_a_version_is_refused(self) -> None:
        retriever, _ = self._retriever([_chunk("a", "")])
        with self.assertRaises(VersionMismatch) as ctx:
            retriever.search(RetrievalQuery(text="q", limit=5))
        self.assertIn("K-2", str(ctx.exception))

    def test_k2_a_chunk_from_another_version_is_refused(self) -> None:
        retriever, _ = self._retriever([_chunk("old", "v1")])
        with self.assertRaises(VersionMismatch):
            retriever.search(RetrievalQuery(text="q", limit=5))

    def test_k3_no_current_version_is_refused(self) -> None:
        empty = KnowledgeVersionRegistry()
        retriever = VersionedRetriever(retriever=_RecordingRetriever([]), registry=empty)
        with self.assertRaises(NoCurrentVersion):
            retriever.search(RetrievalQuery(text="q", limit=5))

    def test_an_unknown_pinned_version_is_refused(self) -> None:
        retriever = VersionedRetriever(
            retriever=_RecordingRetriever([]), registry=self.registry, version_id="ghost"
        )
        with self.assertRaises(UnknownVersion):
            retriever.search(RetrievalQuery(text="q", limit=5))


class PipelineIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = KnowledgeVersionRegistry()
        self.registry.register(_version("v2"), make_current=True)
        self.base = _RecordingRetriever([_chunk("a", "v2"), _chunk("denied", "v2", tenant="other")])

    def _pipeline(self, permission=AllowAll()) -> RetrievalPipeline:
        return RetrievalPipeline(
            retriever=VersionedRetriever(retriever=self.base, registry=self.registry),
            permission=permission,
        )

    def test_the_pipeline_reads_the_pinned_version(self) -> None:
        result = self._pipeline().run(RetrievalQuery(text="q", tenant_id="acme", limit=5))
        self.assertEqual({c.chunk_id for c in result.kept}, {"a", "denied"})
        self.assertEqual(result.knowledge_versions, ("v2",))

    def test_c9_permission_still_denies_after_versioning(self) -> None:
        result = self._pipeline(TenantFilter()).run(
            RetrievalQuery(text="q", tenant_id="acme", limit=5)
        )
        self.assertEqual([c.chunk_id for c in result.kept], ["a"])
        self.assertEqual([c.chunk_id for c in result.denied], ["denied"])

    def test_the_version_rides_into_the_context_item_and_snapshot(self) -> None:
        pipeline = self._pipeline()
        result = pipeline.run(RetrievalQuery(text="q", tenant_id="acme", limit=5))
        items = pipeline.to_context_items(result)
        self.assertEqual(items[0].attributes["knowledge_version"], "v2")

        plan = type("Plan", (), {"kept": items, "dropped": (), "total_tokens": 0})()
        snap = build_snapshot(run_id="run_1", plan=plan, clock=ManualClock())
        self.assertIsInstance(snap, ContextSnapshot)
        self.assertEqual(
            snap.items_of("knowledge")[0].attributes["knowledge_version"], "v2"
        )

    def test_a_mixed_version_result_reports_all_versions(self) -> None:
        """没锁版本（或锁了没生效）时，如实列出所有版本，而不是挑一个说。"""
        from packages.agent_context.retrieval import RetrievalResult

        result = RetrievalResult(
            kept=(_chunk("a", "v1"), _chunk("b", "v2"), _chunk("c", ""))
        )
        self.assertEqual(result.knowledge_versions, ("", "v1", "v2"))


if __name__ == "__main__":
    unittest.main()
