"""文档处理：抓取 → 结构化 → 切分 → 落盘（带 manifest 与 citation）。

--------------------------------------------------------------------------
为什么直接取官方 Markdown，而不是解析 HTML

redis.io 为 AI agent 直接提供每页的 Markdown：

    <link rel="alternate" type="text/markdown" href=".../brpop/index.html.md">

它比 HTML 多两样**恰好是切分与评测需要**的东西：

    · 一个 ```json metadata 块 —— syntax / complexity / group / since / tableOfContents
    · 干净的 ## 章节结构 —— 直接就是 chunk 的边界与 citation 的锚点

所以这一层不写 HTML 清洗器（那是给没有 Markdown 出口的站点准备的），
而是**用官方给的结构**。少一层启发式，就少一层错误。

--------------------------------------------------------------------------
切分

交给 `asuka.splitters`（默认 LangChain 的 header + recursive，
外加一层"代码围栏 / 表格绝不跨切"的保护）。这里只负责：

    元数据块 → 人读的 spec 表（独立成一个 chunk）
    切分结果 → 带 citation 的 Chunk（C-10 的载体）
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from packages.agent_context.retrieval import Chunk

from .splitters import CHUNK_OVERLAP, CHUNK_SIZE
from .splitters import split_markdown
from .textutil import Block, estimate_tokens

# ---------------------------------------------------------------- 常量

#: 章节内递归的**字符**预算（对齐 LangChain `RecursiveCharacterTextSplitter`
#: 的默认 `length_function=len`；参数由用户指定为 1000 / 150）
#: ⚠️ 单位是字符，不是 token —— 想按 token 切要换 length_function，别只改数字。
MAX_CHARS = CHUNK_SIZE
OVERLAP_CHARS = CHUNK_OVERLAP

#: 上下文悬崖（社区基准，token 计）：超过它的 chunk 会被标记，便于审计
CONTEXT_CLIFF_TOKENS = 2500

#: 低于这个长度的 chunk 多半是导航/残留，直接丢弃（社区：语义分块的碎片问题）
MIN_CHARS = 40

USER_AGENT = "Asuka-Eval-MVP/0.1 (+https://github.com/todochenxi/Asuka)"


# ---------------------------------------------------------------- 注册表


@dataclass(frozen=True)
class Unit:
    """一个可判分的文档单元（Redis 的一条命令 / Python 的一个函数 …）。"""

    topic: str
    unit_id: str
    title: str
    group: str = ""
    description: str = ""
    syntax: str = ""

    @property
    def document_id(self) -> str:
        return f"{self.topic}:{self.unit_id}"


@dataclass(frozen=True)
class Topic:
    """一类文档（redis / kubernetes / python / …）。"""

    topic: str
    display_name: str
    markdown_url_template: str
    page_url_template: str
    units: tuple[Unit, ...]
    vendor: str = ""
    license_note: str = ""
    #: 这份语料用哪组标题层级（见 `splitters.HEADER_PRESETS`）。
    #: 它是**语料的属性**，不是库的默认值 —— 同一套切分器喂不同结构的文档，
    #: 该用的层级本来就不同。库里默认 `spec`（社区标准），语料各自覆盖。
    headers: str = "spec"
    #: 可见性。写进每个 chunk 的 attributes，供 `kb.PublicCorpusFilter` 判。
    #: 默认 `public`（官方文档人人可读）；**没有标记的一律拒绝**（fail-closed）——
    #: 这样"往语料里塞了一份内部文档但忘了标记"的后果是**检不到**，而不是**泄漏**。
    visibility: str = "public"

    def markdown_url(self, unit: Unit) -> str:
        return self.markdown_url_template.format(unit=unit.unit_id)

    def page_url(self, unit: Unit) -> str:
        return self.page_url_template.format(unit=unit.unit_id)


def registry_dir() -> Path:
    return Path(__file__).resolve().parent / "registry"


def load_topic(topic: str, *, root: Path | None = None) -> Topic:
    base = root or registry_dir()
    path = base / f"{topic}.json"
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    units = tuple(
        Unit(
            topic=str(raw["topic"]),
            unit_id=str(u["unit_id"]),
            title=str(u["title"]),
            group=str(u.get("group", "")),
            description=str(u.get("description", "")),
            syntax=str(u.get("syntax", "")),
        )
        for u in raw["units"]
    )
    if not units:
        raise ValueError(f"registry {path} declares no units")
    return Topic(
        topic=str(raw["topic"]),
        display_name=str(raw["display_name"]),
        markdown_url_template=str(raw["markdown_url_template"]),
        page_url_template=str(raw["page_url_template"]),
        units=units,
        vendor=str(raw.get("vendor", "")),
        license_note=str(raw.get("license_note", "")),
        headers=str(raw.get("headers", "spec")),
        visibility=str(raw.get("visibility", "public")),
    )


# ---------------------------------------------------------------- 抓取


@dataclass(frozen=True)
class FetchRecord:
    """一次抓取的可复现证据。"""

    unit_id: str
    url: str
    status: str          # ok / http_error / network_error / missing_cache
    http_code: int = 0
    bytes_: int = 0
    sha256: str = ""
    fetched_at: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "url": self.url,
            "status": self.status,
            "http_code": self.http_code,
            "bytes": self.bytes_,
            "sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "note": self.note,
        }


def fetch_text(url: str, *, timeout: float = 30.0) -> tuple[str, int]:
    """抓一页。返回 (text, http_code)。异常由调用方归类。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        code = int(getattr(resp, "status", 0) or 0)
        raw = resp.read()
    return raw.decode("utf-8", errors="replace"), code


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 结构化

#: spec 表里按这个顺序渲染（取自官方 metadata 块）
_SPEC_FIELDS: tuple[tuple[str, str], ...] = (
    ("syntax_fmt", "syntax"),
    ("description", "description"),
    ("group", "group"),
    ("complexity", "complexity"),
    ("since", "since"),
    ("command_flags", "flags"),
    ("acl_categories", "acl categories"),
    ("arity", "arity"),
)


def extract_metadata_block(markdown: str) -> tuple[Mapping[str, Any], str]:
    """取出 ```json metadata 块，并返回**去掉它之后**的正文。

    ⚠️ 它必须被取出来单独处理，不能留在正文里当检索目标：
    原始 JSON 里的 `"key_specs"` / `"arity": -3` 这类内容会污染词法检索
    （它们既不是英文散文，也不承载"考点"）。
    但它也**不能丢** —— 复杂度、语法、版本这类问题的答案**只在它里面**。
    所以：取出来 → 渲染成人读的表 → 作为一个独立的 spec chunk 参与检索。
    """
    lines = markdown.splitlines()
    out: list[str] = []
    meta: Mapping[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```") and "metadata" in stripped.lower():
            buf: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blob = "\n".join(buf).strip()
            try:
                meta, _end = json.JSONDecoder().raw_decode(blob)
            except json.JSONDecodeError:
                meta = {}
            continue
        out.append(line)
        i += 1
    return meta, "\n".join(out)


#: redis.io 页面模板的"代码示例图例"节。
#: 它**不是文档结构的一部分** —— 证据在官方 metadata 自己的 `tableOfContents` 里：
#: 那里列了 required-arguments / optional-arguments / examples / details /
#: redis-software-and-redis-cloud-compatibility / return-information，
#: **没有** "Code Examples Legend"。
_TEMPLATE_LEGEND_HEADING = "## Code Examples Legend"

_HEADING_RE = re.compile(r"^#{1,6}\s")


def strip_template_legend(body: str) -> tuple[str, int]:
    """去掉 redis.io 页面模板的 `## Code Examples Legend` 样板块。

    它的形状是固定的（14/20 份文档里图例在第 25 行、`---` 在第 44 行）::

        ## Code Examples Legend
        <一段话 + 12 条语言 bullet + 一段话>
        ---
        <没有标题的命令概述正文>        ← 真正属于这份文档的内容
        ## Required arguments

    **为什么要去掉**（两个独立理由，缺一都不够）:

    1. 它是**逐字相同的样板** —— 同一份 12 条语言列表在 14 份文档里重复出现。
       留着就会产生 14 个近乎重复的 chunk，直接拉低检索精度。

    2. 它把紧跟其后的"无标题概述"**拖进自己的 heading 路径**，
       于是 citation 变成 `Redis · EXPIRE · Code Examples Legend`，
       而内容是 "Set a timeout on `key`..." ——
       **citation 在说谎，而 Citation 指标正是拿 citation 判分的。**

    ⚠️ 这不是"修 LangChain 的 bug"：LangChain 按标题切分，
    把标题之后的内容归给该标题，**完全正确**。错的是这份模板结构。
    所以修在**切分之前**（归一化），而不是在切分之后猜。

    返回 `(归一化后的正文, 被移除的字符数)`。字符数要记账，
    不能静默丢弃 —— "丢掉了什么"必须能从 manifest 里读出来。
    """
    lines = body.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == _TEMPLATE_LEGEND_HEADING),
        None,
    )
    if start is None:
        return body, 0

    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s == "---":
            end = j + 1  # 连分隔线一起去掉（它属于模板）
            break
        if _HEADING_RE.match(s):
            end = j      # 兜底：没有分隔线时，切到下一个标题为止
            break

    removed = "\n".join(lines[start:end])
    kept = lines[:start] + lines[end:]
    return "\n".join(kept), len(removed)


def prepare_document(markdown: str) -> tuple[Mapping[str, Any], str, int]:
    """原始 Markdown → (metadata, 可切分正文, 被移除的模板字符数)。"""
    meta, body = extract_metadata_block(markdown)
    body, legend_chars = strip_template_legend(body)
    return meta, body, legend_chars


def render_spec(meta: Mapping[str, Any]) -> str:
    """把 metadata 渲染成人读的 Markdown 表（可检索、可判分）。"""
    if not meta:
        return ""
    rows: list[str] = ["| field | value |", "|---|---|"]
    for key, label in _SPEC_FIELDS:
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = str(value).replace("|", "\\|").strip()
        if text:
            rows.append(f"| {label} | {text} |")
    return "\n".join(rows) if len(rows) > 2 else ""


def page_title(markdown: str, meta: Mapping[str, Any]) -> str:
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return str(meta.get("title", ""))


# ---------------------------------------------------------------- 组装


def chunk_document(
    markdown: str,
    unit: Unit,
    *,
    display_name: str,
    url: str,
    chunk_size: int = MAX_CHARS,
    chunk_overlap: int = OVERLAP_CHARS,
    min_chars: int = MIN_CHARS,
    backend: str = "auto",
    headers: str = "spec",
    visibility: str = "public",
) -> tuple[Chunk, ...]:
    """一份文档 → 带 citation 的 Chunk 序列。

    citation 形如 `Redis · BRPOP · Required arguments`：
    **可人读、可定位、可判分** —— 它同时是 C-10 的载体和 Citation 指标的输入。
    """
    meta, body, legend_chars = prepare_document(markdown)
    title = page_title(body, meta) or unit.title
    chunks: list[Chunk] = []
    index = 0

    def emit(
        section: str,
        blocks: Sequence[Block],
        *,
        kind_hint: str = "",
        oversized: bool = False,
        kinds: Sequence[str] | None = None,
    ) -> None:
        nonlocal index
        text_body = "\n\n".join(b.text for b in blocks)
        header = f"{display_name} · {title} · {section}"
        text = f"{header}\n\n{text_body}"
        if len(text) < min_chars:
            return
        # 优先用切分器给出的 kinds（code / table / paragraph）——
        # 直接读 Block.kind 会把它退化成 "piece"，丢掉"这段是不是代码"这个事实。
        resolved_kinds = list(kinds) if kinds else (sorted({b.kind for b in blocks}) or ([kind_hint] if kind_hint else []))
        tokens = estimate_tokens(text)
        chunks.append(
            Chunk(
                chunk_id=f"{unit.document_id}:{index:03d}",
                document_id=unit.document_id,
                text=text,
                citation=header,
                score=0.0,
                attributes={
                    "topic": unit.topic,
                    "unit_id": unit.unit_id,
                    "unit_title": unit.title,
                    "group": unit.group,
                    "section": section,
                    "url": url,
                    "visibility": visibility,
                    "index": index,
                    "chars": len(text),
                    "body_chars": len(text_body),
                    "tokens": tokens,
                    "kinds": resolved_kinds,
                    # ⚠️ 比的是**正文**与预算，不含 citation 前缀 ——
                    # 把前缀算进去会让"刚好装下"的 chunk 假阳性超预算。
                    "oversized": oversized,
                    "over_cliff": tokens > CONTEXT_CLIFF_TOKENS,
                    "backend": backend,
                    # 归一化账本：这份文档被移除的模板样板块字符数（0 = 本来就没有）。
                    # 每个 chunk 都带一份，是为了让审计**不需要回看 manifest**
                    # 就能回答"我引用的这段话，它的来源被改动过吗、改动了多少"。
                    "legend_removed_chars": legend_chars,
                },
            )
        )
        index += 1

    spec = render_spec(meta)
    if spec:
        emit("command spec", (Block("table", spec),), kind_hint="table", oversized=len(spec) > chunk_size)

    for piece in split_markdown(
        body,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        backend=backend,
        headers=headers,
    ):
        section = piece.heading_path[-1] if piece.heading_path else "overview"
        if section.strip() == title.strip():
            section = "overview"
        emit(section, (Block("piece", piece.text),), oversized=piece.oversized, kinds=piece.kinds)
    return tuple(chunks)


# ---------------------------------------------------------------- 主流程


@dataclass
class IngestReport:
    topic: str
    backend: str = "auto"
    fetched: list[FetchRecord] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    units_ok: list[str] = field(default_factory=list)
    units_failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        legend_removed = sum(
            int(c.attributes.get("legend_removed_chars", 0))
            for c in self.chunks
            if int(c.attributes.get("index", -1)) == 0
        )
        docs_with_legend = sum(
            1
            for c in self.chunks
            if int(c.attributes.get("index", -1)) == 0
            and int(c.attributes.get("legend_removed_chars", 0)) > 0
        )
        return {
            "topic": self.topic,
            "backend": self.backend,
            "units_total": len(self.fetched),
            "units_ok": len(self.units_ok),
            "units_failed": len(self.units_failed),
            "chunks": len(self.chunks),
            "tokens": sum(int(c.attributes.get("tokens", 0)) for c in self.chunks),
            "oversized": sum(1 for c in self.chunks if c.attributes.get("oversized")),
            "normalization": {
                "template_legend_docs": docs_with_legend,
                "template_legend_chars_removed": legend_removed,
            },
            "fetched": [r.as_dict() for r in self.fetched],
        }


def chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "citation": chunk.citation,
        "text": chunk.text,
        "attributes": dict(chunk.attributes),
    }


def chunk_from_dict(d: Mapping[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=str(d["chunk_id"]),
        document_id=str(d["document_id"]),
        text=str(d["text"]),
        citation=str(d["citation"]),
        score=float(d.get("score", 0.0)),
        attributes=dict(d.get("attributes", {})),
    )


def write_chunks(path: Path, chunks: Iterable[Chunk]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk_to_dict(chunk), ensure_ascii=False) + "\n")
            n += 1
    return n


def read_chunks(path: Path) -> list[Chunk]:
    out: list[Chunk] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(chunk_from_dict(json.loads(line)))
    return out


def ingest(
    topic_name: str,
    *,
    raw_dir: Path,
    corpus_dir: Path,
    offline: bool = False,
    only: Sequence[str] | None = None,
    backend: str = "auto",
    headers: str | None = None,
) -> IngestReport:
    """抓取一类文档的全部单元 → 结构化 → 切分 → 落盘。

    `offline=True` 时只用 `raw_dir` 里已有的 Markdown（可复现重跑，不再打网络）。
    `headers=None` 时用**语料自己声明的**标题层级（见 `registry/<topic>.json`）。
    """
    from .splitters import active_backend  # noqa: PLC0415 - 延迟到运行期，便于报错清晰

    resolved = active_backend(backend)
    topic = load_topic(topic_name)
    headers = headers or topic.headers
    report = IngestReport(topic=topic.topic, backend=resolved)
    raw_topic_dir = raw_dir / topic.topic
    raw_topic_dir.mkdir(parents=True, exist_ok=True)

    wanted = set(only) if only else None
    for unit in topic.units:
        if wanted is not None and unit.unit_id not in wanted:
            continue
        url = topic.markdown_url(unit)
        cached = raw_topic_dir / f"{unit.unit_id}.md"
        if offline:
            if not cached.exists():
                report.fetched.append(
                    FetchRecord(unit.unit_id, url, "missing_cache", note="offline 且无缓存")
                )
                report.units_failed.append(unit.unit_id)
                continue
            text = cached.read_text(encoding="utf-8")
            record = FetchRecord(
                unit.unit_id,
                url,
                "ok",
                bytes_=len(text.encode("utf-8")),
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                fetched_at=_now(),
                note="offline（用缓存）",
            )
        else:
            try:
                text, code = fetch_text(url)
            except urllib.error.HTTPError as exc:
                report.fetched.append(
                    FetchRecord(unit.unit_id, url, "http_error", http_code=int(exc.code), note=str(exc.reason))
                )
                report.units_failed.append(unit.unit_id)
                continue
            except Exception as exc:  # noqa: BLE001 - 归类后如实记录，不吞
                report.fetched.append(
                    FetchRecord(unit.unit_id, url, "network_error", note=f"{type(exc).__name__}: {exc}")
                )
                report.units_failed.append(unit.unit_id)
                continue
            cached.write_text(text, encoding="utf-8", newline="\n")
            record = FetchRecord(
                unit.unit_id,
                url,
                "ok",
                http_code=code,
                bytes_=len(text.encode("utf-8")),
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                fetched_at=_now(),
            )

        report.fetched.append(record)
        chunks = chunk_document(
            text,
            unit,
            display_name=topic.display_name,
            url=topic.page_url(unit),
            backend=resolved,
            headers=headers,
            visibility=topic.visibility,
        )
        if not chunks:
            report.units_failed.append(unit.unit_id)
            continue
        report.chunks.extend(chunks)
        report.units_ok.append(unit.unit_id)

    out_dir = corpus_dir / topic.topic
    chunks_path = out_dir / "chunks.jsonl"
    write_chunks(chunks_path, report.chunks)
    manifest = {
        "topic": topic.topic,
        "display_name": topic.display_name,
        "vendor": topic.vendor,
        "license_note": topic.license_note,
        "generated_at": _now(),
        "params": {
            "backend": resolved,
            "headers": headers,
            "chunk_size": MAX_CHARS,
            "chunk_overlap": OVERLAP_CHARS,
            "length_unit": "characters",
            "context_cliff_tokens": CONTEXT_CLIFF_TOKENS,
            "min_chars": MIN_CHARS,
        },
        "summary": report.as_dict(),
        "chunks_file": chunks_path.name,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
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
    parser = argparse.ArgumentParser(description="Asuka 文档处理：抓取 → 结构化 → 切分")
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument("--offline", action="store_true", help="只用已缓存 Markdown")
    parser.add_argument("--only", nargs="*", default=None, help="只处理这些 unit_id")
    parser.add_argument("--backend", default="auto", choices=["auto", "langchain", "stdlib"])
    parser.add_argument(
        "--headers",
        default=None,
        help="标题级预设：留空=用语料声明的（registry/<topic>.json 的 headers 字段）；"
        "可显式给 spec(=h1,h2,h3) / deep(=+h4,h5) / h1,h2,h3",
    )
    parser.add_argument("--raw-dir", default=str(root / "raw"))
    parser.add_argument("--corpus-dir", default=str(root / "corpus"))
    args = parser.parse_args(argv)

    report = ingest(
        args.topic,
        raw_dir=Path(args.raw_dir),
        corpus_dir=Path(args.corpus_dir),
        offline=args.offline,
        only=args.only,
        backend=args.backend,
        headers=args.headers,
    )
    summary = report.as_dict()
    print(
        f"[{summary['topic']}] backend={summary['backend']} "
        f"headers={args.headers or '(语料声明)'} "
        f"units {summary['units_ok']}/{summary['units_total']} ok, "
        f"chunks={summary['chunks']}, tokens={summary['tokens']}, "
        f"oversized={summary['oversized']}"
    )
    norm = summary["normalization"]
    print(
        f"  normalization: 模板样板块移除 {norm['template_legend_chars_removed']} 字符 "
        f"/ {norm['template_legend_docs']} 份文档"
    )
    for rec in report.fetched:
        if rec.status != "ok":
            print(f"  ! {rec.unit_id}: {rec.status} {rec.note}")
    return 0 if not report.units_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
