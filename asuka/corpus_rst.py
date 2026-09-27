"""第二类语料的**摄取**适配：`.rst` 源 → Asuka 的 chunks.jsonl。

**为什么单独一个模块，而不是改 `corpus.py`**：

`corpus.py` 的摄取链（抓 → 结构化 → 切分 → 落盘 → manifest）是**与语料无关**的，
但它有两处 redis 专属：`prepare_document`（剥 `## Code Examples Legend` + 抽
```json metadata```）和"源本来就是 Markdown"。把它改成"按语料分叉"会往
那条热路径里加条件，而 redis 那条链是**已被上千条判据钉住**的。

所以这里**只换两个钩子**，其余全部复用 `corpus` 里同一批函数：

    · 抓取后先把 `.rst` 过一遍 `convert_rst`（并把转换账记进 manifest）
    · 不做 redis 的 metadata 提取（Python 文档没有那个块）

切分、citation 形状、manifest 结构、`Chunk.attributes` 全部**逐字段相同** ——
于是 python 与 redis 两类语料产出的是**同一种东西**，下游（kb / evaluate /
answers）一行都不用改。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import corpus as _corpus
from .rst import convert_rst, strip_rst_header_comment


@dataclass
class RstIngestReport:
    topic: str
    backend: str = "stdlib"
    units_ok: list[str] = field(default_factory=list)
    units_failed: list[str] = field(default_factory=list)
    fetched: list[dict] = field(default_factory=list)
    chunks: list = field(default_factory=list)
    conversion: dict[str, int] = field(default_factory=dict)
    docs: int = 0

    def as_summary(self) -> dict:
        return {
            "topic": self.topic,
            "backend": self.backend,
            "units_total": len(self.units_ok) + len(self.units_failed),
            "units_ok": len(self.units_ok),
            "units_failed": len(self.units_failed),
            "chunks": len(self.chunks),
            "docs": self.docs,
            "conversion": dict(self.conversion),
        }


#: 这几项是**计数**（累加）；`max_depth` 不是 —— 它跨文档要取**最大**，
#: 累加会得到一个荒谬的数（第一版实测：20 篇加起来报了 55，
#: 而任何一篇的真实层级都 ≤ 5）。计数与"极值"混在一个累加器里是这个 bug 的形状。
_COUNTED = ("sections", "inline_literals", "code_blocks", "directives_kept")


def _accumulate(acc: dict[str, int], rep: dict[str, int]) -> None:
    for k in _COUNTED:
        acc[k] = acc.get(k, 0) + int(rep.get(k, 0))
    acc["max_depth"] = max(int(acc.get("max_depth", 0)), int(rep.get("max_depth", 0)))


def ingest_rst(
    topic_name: str,
    *,
    raw_dir: Path,
    corpus_dir: Path,
    offline: bool = False,
    only: Sequence[str] | None = None,
    backend: str = "auto",
) -> RstIngestReport:
    """抓 `.rst` 类语料 → 转 Markdown → 复用 `corpus.chunk_document` → 落盘。"""
    from .splitters import active_backend

    resolved = active_backend(backend)
    topic = _corpus.load_topic(topic_name)
    report = RstIngestReport(topic=topic.topic, backend=resolved)
    raw_topic_dir = raw_dir / topic.topic
    raw_topic_dir.mkdir(parents=True, exist_ok=True)

    wanted = set(only) if only else None
    for unit in topic.units:
        if wanted is not None and unit.unit_id not in wanted:
            continue
        url = topic.markdown_url(unit)
        cached = raw_topic_dir / f"{unit.unit_id}.rst"

        if offline:
            if not cached.exists():
                report.fetched.append(
                    {"unit_id": unit.unit_id, "url": url, "status": "missing_cache"}
                )
                report.units_failed.append(unit.unit_id)
                continue
            raw_text = cached.read_text(encoding="utf-8")
        else:
            try:
                raw_text, code = _corpus.fetch_text(url)
            except Exception as exc:  # noqa: BLE001 - 归类后如实记录，不吞
                report.fetched.append(
                    {
                        "unit_id": unit.unit_id,
                        "url": url,
                        "status": "network_error",
                        "note": f"{type(exc).__name__}: {exc}",
                    }
                )
                report.units_failed.append(unit.unit_id)
                continue
            cached.write_text(raw_text, encoding="utf-8", newline="\n")

        markdown, conv = convert_rst(strip_rst_header_comment(raw_text))
        _accumulate(report.conversion, conv.as_dict())
        report.docs += 1
        report.fetched.append(
            {"unit_id": unit.unit_id, "url": url, "status": "ok", "bytes": len(raw_text)}
        )

        chunks = _corpus.chunk_document(
            markdown,
            unit,
            display_name=topic.display_name,
            url=topic.page_url(unit),
            backend=resolved,
            headers=topic.headers,
            visibility=topic.visibility,
        )
        if not chunks:
            report.units_failed.append(unit.unit_id)
            continue
        report.chunks.extend(chunks)
        report.units_ok.append(unit.unit_id)

    out_dir = corpus_dir / topic.topic
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = out_dir / "chunks.jsonl"
    _corpus.write_chunks(chunks_path, report.chunks)
    manifest = {
        "topic": topic.topic,
        "display_name": topic.display_name,
        "vendor": topic.vendor,
        "license_note": topic.license_note,
        "source_format": "rst",
        "generated_at": _corpus._now(),
        "params": {
            "backend": resolved,
            "headers": topic.headers,
            "chunk_size": _corpus.MAX_CHARS,
            "chunk_overlap": _corpus.OVERLAP_CHARS,
            "length_unit": "characters",
            "min_chars": _corpus.MIN_CHARS,
        },
        "summary": report.as_summary(),
        "chunks_file": chunks_path.name,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Asuka 文档处理（RST 源，如 Python 官方文档）：抓取 → 转换 → 切分"
    )
    parser.add_argument("topic", nargs="?", default="python")
    parser.add_argument("--offline", action="store_true", help="只用已缓存 .rst")
    parser.add_argument("--only", nargs="*", default=None, help="只处理这些 unit_id")
    parser.add_argument("--backend", default="auto", choices=["auto", "langchain", "stdlib"])
    parser.add_argument("--raw-dir", default=str(root / "raw"))
    parser.add_argument("--corpus-dir", default=str(root / "corpus"))
    args = parser.parse_args(argv)

    report = ingest_rst(
        args.topic,
        raw_dir=Path(args.raw_dir),
        corpus_dir=Path(args.corpus_dir),
        offline=args.offline,
        only=args.only,
        backend=args.backend,
    )
    s = report.as_summary()
    print(
        f"[{s['topic']}] backend={s['backend']} "
        f"units {s['units_ok']}/{s['units_total']} ok, chunks={s['chunks']}"
    )
    print(f"  转换：{s['conversion']}")
    return 0 if not report.units_failed else 1


__all__ = ["RstIngestReport", "ingest_rst", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
