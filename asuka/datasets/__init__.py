"""任务集（人工整理的 ground truth）。

    python -m asuka.datasets redis            # 校验 + 落盘 + 打印统计
    python -m asuka.datasets redis --dry-run  # 只校验，不写文件

schema 与校验契约在 `asuka.dataset`；这里是**具体某一类文档的题目**。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from ..corpus import read_chunks
from ..dataset import Dataset, DatasetError, Evidence, RequiredPoint, TaskItem, dataset_path

__all__ = [
    "Dataset",
    "DatasetError",
    "Evidence",
    "RequiredPoint",
    "TaskItem",
    "build_redis",
    "coverage_lines",
    "main",
]


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_redis() -> Dataset:
    from .redis import build  # noqa: PLC0415 - 惰性，避免 import 一个数据集就拉起全部

    return build()


_BUILDERS = {"redis": build_redis}


def coverage_lines(ds: Dataset) -> list[str]:
    """把 `out_of_corpus` 声明渲染成几行 —— 没有声明时返回**空列表**（一行都不印）。

    抽成函数是为了让"什么时候印、印几条"能被断言，而不是靠读 `main()` 的代码。
    """
    lines: list[str] = []
    n = sum(1 for i in ds.items if i.out_of_corpus)
    if not n:
        return lines
    lines.append(f"  ⚠️ {n} 条题声明了「参考答案里语料支撑不了的部分」：")
    for item in ds.items:
        for entry in item.out_of_corpus:
            lines.append(f"     {item.task_id}  {entry}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="Asuka 任务集：校验 + 落盘")
    parser.add_argument("topic", nargs="?", default="redis", choices=sorted(_BUILDERS))
    parser.add_argument("--corpus-dir", default=str(_root() / "corpus"))
    parser.add_argument("--datasets-dir", default=str(_root() / "datasets"))
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus_dir)
    chunks_path = corpus_dir / args.topic / "chunks.jsonl"
    if not chunks_path.exists():
        print(f"! 没有语料：{chunks_path}（先跑 python -m asuka.corpus {args.topic}）")
        return 2
    chunks = read_chunks(chunks_path)

    ds = _BUILDERS[args.topic]()
    try:
        ds.resolve(chunks)
    except DatasetError as exc:
        print(f"! {exc}")
        return 1

    stats = ds.stats()
    print(f"[{stats['topic']}] 任务 {stats['items']} 条 / 覆盖 {stats['units_covered']} 个单元")
    for level, n in stats["by_difficulty"].items():
        from ..dataset import DIFFICULTY_LABEL

        print(f"  {level:8s} {n:3d}   {DIFFICULTY_LABEL.get(level, '')}")
    print(
        f"  evidence {stats['evidence_total']} 处 → 解析出 "
        f"{stats['resolved_chunks']} 个 chunk（{stats['resolved_tasks']} 条任务全部可解析）"
    )

    # `out_of_corpus` 是**人工声明**（见 `TaskItem.out_of_corpus`）：题目要求里有
    # 语料撑不住的部分。它是**事实**不是缺陷 —— 扩语料还是改题由人定，
    # 评测脚本不替用户下这个决定，所以这里只如实报出来。
    for line in coverage_lines(ds):
        print(line)

    if args.dry_run:
        print("  (dry-run，未写文件)")
        return 0

    path = dataset_path(Path(args.datasets_dir), args.topic)
    n = ds.save(path)
    print(f"  → {path}（{n} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
