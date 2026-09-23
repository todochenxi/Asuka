"""建索引 / 查索引的入口。

    python -m asuka.index redis                        # 建索引（用默认 embedder）
    python -m asuka.index redis --recreate             # 重建
    python -m asuka.index redis --embedder hashing     # 离线冒烟（**不承载语义**）
    python -m asuka.index redis --query "how do I set a TTL on a key"
    python -m asuka.index redis --stats                # 只看现状，不写
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .embedding import EmbeddingError, build_embedder
from .vectorstore import (
    DEFAULT_DISTANCE,
    DEFAULT_URL,
    QdrantStore,
    VectorStoreError,
    index_topic,
    load_index_manifest,
    search_topic,
    stale_corpus,
)


def _root() -> Path:
    return Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="Asuka 向量索引：语料 → bge-m3 → Qdrant")
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument("--corpus-dir", default=str(_root() / "corpus"))
    parser.add_argument("--url", default=os.environ.get("ASUKA_QDRANT_URL", DEFAULT_URL))
    parser.add_argument("--api-key", default=os.environ.get("ASUKA_QDRANT_API_KEY", ""))
    parser.add_argument("--distance", default=DEFAULT_DISTANCE)
    parser.add_argument(
        "--embedder",
        default="auto",
        choices=["auto", "api", "local", "hashing"],
        help="auto=按环境变量决定（没 key 就报错，不静默降级）",
    )
    parser.add_argument("--recreate", action="store_true", help="删掉同名 collection 重建")
    parser.add_argument("--limit", type=int, default=None, help="只索引前 N 个 chunk（冒烟）")
    parser.add_argument(
        "--model-path",
        default="",
        help="本地模型权重目录（不填则读 ASUKA_EMBED_MODEL_PATH，再退回让 HF 自己找）",
    )
    parser.add_argument("--query", default=None, help="查一条，看 top-k")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--stats", action="store_true", help="只打印现状")
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus_dir)
    store = QdrantStore(args.url, api_key=args.api_key)

    # ---------------------------------------------------------- stats
    if args.stats:
        if not store.ping():
            print(f"! 连不上 Qdrant：{args.url}")
            return 2
        print(f"Qdrant {args.url}  collections={store.collections()}")
        try:
            man = load_index_manifest(corpus_dir, args.topic)
        except VectorStoreError as exc:
            print(f"  {exc}")
            return 0
        print(json.dumps(man, ensure_ascii=False, indent=2))
        n = store.count(man["collection"])
        print(f"  points(实际)={n}  points(manifest)={man.get('points_total')}")
        if stale_corpus(man, corpus_dir, args.topic):
            print("  ⚠️ 语料已变，索引比语料旧 —— 该重建了")
        return 0

    # ---------------------------------------------------------- 连通性
    if not store.ping():
        print(
            f"! 连不上 Qdrant：{args.url}\n"
            f"  起一个：docker run -d --name asuka-qdrant -p 6333:6333 "
            f"-v asuka-qdrant-data:/qdrant/storage qdrant/qdrant:latest"
        )
        return 2

    try:
        embedder = build_embedder(args.embedder, model_path=args.model_path)
    except EmbeddingError as exc:
        print(f"! {exc}")
        return 2

    # ---------------------------------------------------------- query
    if args.query:
        try:
            hits = search_topic(
                args.topic,
                args.query,
                corpus_dir=corpus_dir,
                store=store,
                embedder=embedder,
                top_k=args.top_k,
                # 非语义 embedder 的分数只能冒烟 —— 这里显式放行，
                # 让"我知道它不是真检索"这件事**出现在代码里**
                allow_non_semantic=not embedder.info.semantic,
            )
        except VectorStoreError as exc:
            print(f"! {exc}")
            return 2
        print(f'query: "{args.query}"   embedder={embedder.info.signature}')
        if not hits:
            print("  (没有命中)")
        for i, h in enumerate(hits, 1):
            print(f"  {i:2d}. {h.score:.4f}  {h.citation}")
            print(f"      {h.text[:110].replace(chr(10), ' ')!r}")
        return 0

    # ---------------------------------------------------------- index
    print(f"indexing [{args.topic}] → {args.url}  embedder={embedder.info.signature}")
    if not embedder.info.semantic:
        print("  ⚠️ 这个 embedder **不承载语义**，索引只能用于冒烟测试，别拿来评检索质量")
    try:
        rep = index_topic(
            args.topic,
            corpus_dir=corpus_dir,
            store=store,
            embedder=embedder,
            recreate=args.recreate,
            distance=args.distance,
            limit=args.limit,
        )
    except (VectorStoreError, EmbeddingError) as exc:
        print(f"! {exc}")
        return 2

    print(
        f"  collection={rep.collection} {'(新建)' if rep.created else '(复用)'}\n"
        f"  chunks={rep.chunks}  points_written={rep.points_written}  "
        f"points_total={rep.points_total}  {rep.seconds:.2f}s"
    )
    print(f"  manifest → {corpus_dir / args.topic / 'vectorstore.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
