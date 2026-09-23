"""向量库：把 chunk 连同它的向量存进 Qdrant，并支持按语义检索。

--------------------------------------------------------------------------
为什么 collection 名字里带模型名和维度

基准实验的目的是**比较**：同一份语料、同一个 Agent，换 embedding 模型会怎样。
如果所有模型都往一个 collection 里写，就没法比 —— 后写的会盖掉先写的，
而且维度不同直接炸，维度相同则**悄悄混在一起**（更糟）。

所以：

    asuka_redis_bge-m3_1024
    asuka_redis_bge-large-zh-v1.5_1024      ← 同一个语料、不同模型，各占一格

换模型 = 换一格，两份都在，可以直接 A/B。这是"可比较的实验平台"最省事的实现。

--------------------------------------------------------------------------
为什么还要写 manifest

`asuka/corpus/<topic>/vectorstore.json` 记录：collection 名、embedder 身份、
维度、距离函数、点数、**语料的 sha256**。它的用途是**回答"这批向量是谁生成的"**：

    · 查询时 embedder 身份对不上 → **拒绝**，不返回结果
    · 语料 sha256 对不上    → **警告**（向量比语料旧，索引该重建了）

Qdrant 本身不提供自由格式的 collection 元数据，而"向量是谁生成的"
恰好是这类实验里最容易丢、丢了最致命的一条信息。
所以它落在 manifest 里 —— 与语料自己的 manifest 同一套纪律。

--------------------------------------------------------------------------
关于 sparse / BM25

bge-m3 原生有 sparse 那一头，Qdrant 也支持 sparse vector。
本 MVP **先只做 dense**：先把 dense 的端到端链路（语料 → 索引 → 检索 → 评测）
跑通、指标能读，再决定要不要上混合检索。
理由：混合检索会引入一个"权重怎么调"的新变量，
而在一开始就引入它，会让"检索差"这件事**无法归因**到底是 dense 的问题还是权重的问题。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .corpus import Chunk, read_chunks
from .embedding import Embedder, EmbedderInfo, EmbeddingError, build_embedder

#: uuid5 的命名空间 —— 让 `redis:set:007` 稳定映射到一个合法的 Qdrant point id。
#: 用 uuid5 而不是自增整数，是为了**幂等**：同一份语料重复索引，
#: 覆盖的是同一批点，不会翻倍。
_NS = uuid.NAMESPACE_URL

DEFAULT_URL = "http://localhost:6333"
DEFAULT_DISTANCE = "Cosine"
UPSERT_BATCH = 128


class VectorStoreError(RuntimeError):
    """向量库层的失败。**不吞**。"""


# ---------------------------------------------------------------- 命名


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "-" for c in text]
    return "".join(keep).strip("-").lower()


def collection_name(topic: str, info: EmbedderInfo) -> str:
    """`asuka_redis_bge-m3_1024` —— 模型与维度进名字，换模型就是换一格。"""
    return f"asuka_{_slug(topic)}_{_slug(info.name)}_{info.dim}"


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_NS, chunk_id))


def corpus_fingerprint(chunks: Sequence[Chunk]) -> str:
    """语料指纹：chunk_id + text 的稳定哈希。用来判断"向量比语料旧了没有"。"""
    h = hashlib.sha256()
    for c in sorted(chunks, key=lambda c: c.chunk_id):
        h.update(c.chunk_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(c.text.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def manifest_path(corpus_dir: Path, topic: str) -> Path:
    return corpus_dir / topic / "vectorstore.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 结果


@dataclass(frozen=True)
class ScoredChunk:
    """一次检索命中的一条。带得回 citation —— 它是 Citation 指标的输入。"""

    chunk_id: str
    document_id: str
    citation: str
    text: str
    score: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "citation": self.citation,
            "score": self.score,
            "text": self.text,
        }


# ---------------------------------------------------------------- 客户端


class QdrantStore:
    """Qdrant 的一层薄封装。`qdrant_client` 惰性 import（PR-14 精神）。"""

    def __init__(self, url: str = DEFAULT_URL, *, api_key: str = "", prefer_grpc: bool = False):
        self.url = url
        self.api_key = api_key
        self.prefer_grpc = prefer_grpc
        self._client: object | None = None

    # ---------------------------------------------------------- 连接

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(
                    "需要 qdrant-client：pip install qdrant-client"
                ) from exc
            kwargs: dict[str, Any] = {"url": self.url, "prefer_grpc": self.prefer_grpc}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = QdrantClient(**kwargs)
        return self._client

    def ping(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:  # noqa: BLE001
            return False

    def collections(self) -> list[str]:
        return [c.name for c in self.client.get_collections().collections]

    # ---------------------------------------------------------- 建表

    def ensure_collection(
        self,
        name: str,
        *,
        dim: int,
        distance: str = DEFAULT_DISTANCE,
        recreate: bool = False,
    ) -> bool:
        """建 collection。返回 True 表示**这次真的建了**（或重建了）。"""
        from qdrant_client import models  # noqa: PLC0415

        exists = name in self.collections()
        if exists and not recreate:
            info = self.client.get_collection(name)
            have = _dense_dim(info)
            if have != dim:
                raise VectorStoreError(
                    f"collection {name} 已存在且维度是 {have}，但当前 embedder 是 {dim} 维。\n"
                    f"  ⇒ 拒绝复用（维度不同、语义必不同）。\n"
                    f"  改用别的 collection，或显式 --recreate 重建。"
                )
            return False
        if exists:
            self.client.delete_collection(name)

        self.client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=dim, distance=_distance(distance)),
        )
        # 建 payload 索引：评测时按 unit / section / kind 切片统计是常规操作
        for fld in ("topic", "unit_id", "section", "document_id"):
            try:
                self.client.create_payload_index(
                    collection_name=name,
                    field_name=fld,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception:  # noqa: BLE001 - 索引是优化，建不上不该让索引失败
                pass
        return True

    # ---------------------------------------------------------- 写入

    def upsert(
        self,
        name: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        *,
        embedder: EmbedderInfo,
        headers: str = "",
        batch: int = UPSERT_BATCH,
    ) -> int:
        from qdrant_client import models  # noqa: PLC0415

        if len(chunks) != len(vectors):
            raise VectorStoreError(
                f"chunks 与 vectors 数量不符：{len(chunks)} vs {len(vectors)}"
            )
        written = 0
        for start in range(0, len(chunks), batch):
            group = chunks[start : start + batch]
            vecs = vectors[start : start + batch]
            points = [
                models.PointStruct(
                    id=point_id(c.chunk_id),
                    vector=[float(x) for x in v],
                    payload=_payload(c, embedder, headers),
                )
                for c, v in zip(group, vecs)
            ]
            self.client.upsert(collection_name=name, points=points, wait=True)
            written += len(points)
        return written

    # ---------------------------------------------------------- 读取

    def count(self, name: str) -> int:
        return int(self.client.count(collection_name=name, exact=True).count)

    def search(
        self,
        name: str,
        vector: Sequence[float],
        *,
        top_k: int = 10,
        query_filter: Any | None = None,
        score_threshold: float | None = None,
    ) -> list[ScoredChunk]:
        res = self.client.query_points(
            collection_name=name,
            query=[float(x) for x in vector],
            limit=top_k,
            query_filter=query_filter,
            score_threshold=score_threshold,
            with_payload=True,
        ).points
        out: list[ScoredChunk] = []
        for p in res:
            pl = dict(p.payload or {})
            out.append(
                ScoredChunk(
                    chunk_id=str(pl.get("chunk_id", p.id)),
                    document_id=str(pl.get("document_id", "")),
                    citation=str(pl.get("citation", "")),
                    text=str(pl.get("text", "")),
                    score=float(p.score),
                    payload=pl,
                )
            )
        return out

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            self._client = None


def _distance(name: str) -> Any:
    from qdrant_client import models  # noqa: PLC0415

    table = {
        "Cosine": models.Distance.COSINE,
        "Euclid": models.Distance.EUCLID,
        "Dot": models.Distance.DOT,
        "Manhattan": models.Distance.MANHATTAN,
    }
    if name not in table:
        raise VectorStoreError(f"未知距离函数 {name!r}（可选 {sorted(table)}）")
    return table[name]


def _dense_dim(info: Any) -> int | None:
    params = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    return getattr(vectors, "size", None)


def _payload(chunk: Chunk, embedder: EmbedderInfo, headers: str) -> dict[str, Any]:
    """进 Qdrant 的 payload。**每条都带 embedder 身份** —— 见模块 docstring。"""
    attrs = dict(chunk.attributes)
    payload: dict[str, Any] = {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "citation": chunk.citation,
        "text": chunk.text,
        "embedder": embedder.as_dict(),
    }
    if headers:
        payload["headers"] = headers
    for key in (
        "topic",
        "unit_id",
        "unit_title",
        "group",
        "section",
        "url",
        "visibility",
        "index",
        "chars",
        "body_chars",
        "tokens",
        "kinds",
        "oversized",
        "over_cliff",
        "backend",
        "legend_removed_chars",
    ):
        if key in attrs:
            payload[key] = attrs[key]
    return payload


# ---------------------------------------------------------------- 高层


@dataclass
class IndexReport:
    topic: str
    collection: str
    embedder: EmbedderInfo
    chunks: int = 0
    points_written: int = 0
    points_total: int = 0
    created: bool = False
    corpus_sha256: str = ""
    seconds: float = 0.0
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "collection": self.collection,
            "url": self.url,
            "embedder": self.embedder.as_dict(),
            "chunks": self.chunks,
            "points_written": self.points_written,
            "points_total": self.points_total,
            "created": self.created,
            "corpus_sha256": self.corpus_sha256,
            "seconds": round(self.seconds, 2),
            "generated_at": _now(),
        }


def index_topic(
    topic: str,
    *,
    corpus_dir: Path,
    store: QdrantStore,
    embedder: Embedder | None = None,
    recreate: bool = False,
    distance: str = DEFAULT_DISTANCE,
    limit: int | None = None,
) -> IndexReport:
    """语料 → embedding → Qdrant。幂等（point id 由 chunk_id 决定）。"""
    import time as _time

    t0 = _time.time()
    chunks_path = corpus_dir / topic / "chunks.jsonl"
    if not chunks_path.exists():
        raise VectorStoreError(f"没有语料：{chunks_path}（先跑 python -m asuka.corpus {topic}）")
    chunks = read_chunks(chunks_path)
    if limit:
        chunks = chunks[:limit]

    manifest_file = corpus_dir / topic / "manifest.json"
    headers = ""
    if manifest_file.exists():
        try:
            headers = str(json.loads(manifest_file.read_text(encoding="utf-8"))["params"].get("headers", ""))
        except Exception:  # noqa: BLE001
            headers = ""

    emb = embedder or build_embedder()
    info = emb.info
    name = collection_name(topic, info)
    created = store.ensure_collection(
        name, dim=info.dim, distance=distance, recreate=recreate
    )

    vectors = emb.embed([c.text for c in chunks])
    written = store.upsert(name, chunks, vectors, embedder=info, headers=headers)

    report = IndexReport(
        topic=topic,
        collection=name,
        embedder=info,
        chunks=len(chunks),
        points_written=written,
        points_total=store.count(name),
        created=created,
        corpus_sha256=corpus_fingerprint(chunks),
        seconds=_time.time() - t0,
        url=store.url,
    )
    path = manifest_path(corpus_dir, topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def load_index_manifest(corpus_dir: Path, topic: str) -> dict[str, Any]:
    path = manifest_path(corpus_dir, topic)
    if not path.exists():
        raise VectorStoreError(
            f"没有索引 manifest：{path}（先跑 python -m asuka.index {topic}）"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def assert_embedder_matches(
    manifest: Mapping[str, Any],
    info: EmbedderInfo,
    *,
    allow_non_semantic: bool = False,
) -> str:
    """查询用的 embedder 必须与建索引时**同一个**。对不上就拒绝。

    这是本模块存在的**首要理由**：向量库里躺着 1024 维的 bge-m3 向量，
    拿另一个 1024 维模型来查 —— 维度一样、语义不同、结果全是垃圾、**一声不响**。

    两道门，顺序不能换：

    1. **签名**（`name@dim`）必须一致 —— 不一致直接拒绝。
    2. 索引若由**不承载语义**的 embedder 建成，则拒绝一切检索 ——
       哪怕签名一致。因为这种索引的分数**只能用于冒烟**，
       而"冒烟跑通了"最容易被误读成"检索没问题"。
       要真跑冒烟请显式 `allow_non_semantic=True`，让这个选择**出现在代码里**。

    返回 collection 名。
    """
    recorded = manifest.get("embedder", {})
    if recorded.get("signature") != info.signature:
        raise VectorStoreError(
            "embedder 与索引不符，拒绝检索：\n"
            f"  索引是用：{recorded.get('signature')}（kind={recorded.get('kind')}）\n"
            f"  现在要查：{info.signature}（kind={info.kind}）\n"
            "  ⇒ 向量空间不同，比出来的分数没有意义。\n"
            "     要么换回原 embedder，要么重建索引。"
        )
    if not recorded.get("semantic", True) and not allow_non_semantic:
        raise VectorStoreError(
            f"索引是用**不承载语义**的 embedder（{recorded.get('name')}）建的：\n"
            f"  签名对得上（{recorded.get('signature')}），但它的分数**不能当检索质量**。\n"
            "  ⇒ 拒绝检索。要跑冒烟请显式说出口：\n"
            "     Python：allow_non_semantic=True\n"
            "     CLI   ：--allow-non-semantic"
        )
    return str(manifest["collection"])


def search_topic(
    topic: str,
    query: str,
    *,
    corpus_dir: Path,
    store: QdrantStore,
    embedder: Embedder | None = None,
    top_k: int = 10,
    allow_non_semantic: bool = False,
) -> list[ScoredChunk]:
    """按自然语言查一个 topic。embedder 不匹配 → 拒绝（见 `assert_embedder_matches`）。"""
    manifest = load_index_manifest(corpus_dir, topic)
    emb = embedder or build_embedder()
    name = assert_embedder_matches(
        manifest, emb.info, allow_non_semantic=allow_non_semantic
    )
    vec = emb.embed([query])[0]
    return store.search(name, vec, top_k=top_k)


def stale_corpus(manifest: Mapping[str, Any], corpus_dir: Path, topic: str) -> bool:
    """索引是否比语料旧。旧了不是错，但必须**说出来**。"""
    chunks_path = corpus_dir / topic / "chunks.jsonl"
    if not chunks_path.exists():
        return True
    return corpus_fingerprint(read_chunks(chunks_path)) != manifest.get("corpus_sha256")


def iter_payloads(chunks: Iterable[Chunk], embedder: EmbedderInfo, headers: str = "") -> list[dict[str, Any]]:
    """给测试用的 payload 构造（不连 Qdrant）。"""
    return [_payload(c, embedder, headers) for c in chunks]
