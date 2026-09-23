"""切分：标题级 → 章节内递归，且代码围栏 / 表格绝不跨切。

--------------------------------------------------------------------------
后端

**`langchain`（默认）** —— 社区标准，基准验证过的默认：

    MarkdownHeaderTextSplitter(headers_to_split_on=[("#","h1"),("##","h2"),("###","h3")],
                               strip_headers=False)
        ↓ split_text
    RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        ↓ split_documents          ← 用 split_documents 而不是逐段 split_text：
                                     它会**保留 metadata**（标题路径），
                                     而那正是结构感知切分的全部收益。

**`stdlib`** —— 零依赖兜底，用于"没装 LangChain 也要能跑"的环境，
以及**做 A/B 对照**（切分策略本身就该被评测，不该靠信仰）。

--------------------------------------------------------------------------
为什么还包一层：LangChain 的 header splitter **不认代码围栏**

Markdown 文档里的 shell 示例常常带 `# 这是注释`：

    ```bash
    # 阻塞 0 秒（不等待）
    BRPOP mylist 0
    ```

`MarkdownHeaderTextSplitter` 会把 `# 阻塞 0 秒（不等待）` 当成**一级标题**，
在代码块中间切开 —— 而社区结论很明确：

    "A table that crosses a chunk boundary is useless to retrieve."

所以：`protect_atoms()` 把原子块换成占位符 → 交给 splitter → `restore_atoms()` 还原。

    **策略归 LangChain，原子性归这一层。** 前者是启发式，后者是硬约束。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .textutil import (
    Block,
    estimate_tokens,
    fit_blocks,
    heading_path_from,
    pack_blocks,
    protect_atoms,
    restore_atoms,
    split_blocks,
)

#: 标题级分割的层级**预设**。
#:
#: `spec` —— 用户指定的那一组（h1/h2/h3），也是社区标准默认。**默认用它。**
#:
#: `deep` —— `spec` + h4/h5。为什么会有这个：
#:   redis.io 的代码示例是"同一操作 × 14 种语言"的 codetabs，用 `##### <语言名>` 划 tab。
#:   `#####` 对 `spec` 是**不可见的** —— 于是 `RecursiveCharacterTextSplitter`
#:   按字符切到哪算哪，实测 **55% 的 Examples chunk 混了 ≥2 种语言**
#:   （如 `redis:set:008 = ['C#','Go']`，一片里既有 C# 尾巴又有 Go 开头）。
#:   citation 只能说到 `Examples`，说不出"这是 Go 的例子"。
#:
#: 这不是"spec 错了"，是 spec 写在看到原始文档之前。
#: 两个预设都留着，**用 A/B 的数字决定**，不靠信仰。
HEADER_PRESETS: dict[str, tuple[tuple[str, str], ...]] = {
    "spec": (("#", "h1"), ("##", "h2"), ("###", "h3")),
    "deep": (
        ("#", "h1"),
        ("##", "h2"),
        ("###", "h3"),
        ("####", "h4"),
        ("#####", "h5"),
    ),
}
DEFAULT_HEADERS = "spec"

#: 递归分隔符层级（LangChain 默认）
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", " ", "")

#: 章节内递归的字符预算 —— 与用户指定的 spec 一致
#: ⚠️ 单位是**字符**：LangChain `RecursiveCharacterTextSplitter` 默认
#:    `length_function=len`。想按 token 切就换 length_function，别只改数字。
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150


def headers_for(preset: str) -> tuple[tuple[str, str], ...]:
    """预设名 → `headers_to_split_on`。也接受 `h1,h2,h3` 这样的显式串。"""
    if preset in HEADER_PRESETS:
        return HEADER_PRESETS[preset]
    levels = tuple(part.strip() for part in preset.split(",") if part.strip())
    if levels and all(part.startswith("h") and part[1:].isdigit() for part in levels):
        return tuple(("#" * int(part[1:]), part) for part in levels)
    raise ValueError(f"unknown header preset: {preset!r} (known: {sorted(HEADER_PRESETS)})")


def header_keys(headers: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(key for _marker, key in headers)


def langchain_available() -> bool:
    try:
        import langchain_text_splitters  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def active_backend(requested: str = "auto") -> str:
    if requested == "auto":
        return "langchain" if langchain_available() else "stdlib"
    if requested == "langchain" and not langchain_available():
        raise RuntimeError(
            "chunking backend 'langchain' requested but langchain-text-splitters "
            "is not importable; install it or pass backend='stdlib'"
        )
    return requested


@dataclass(frozen=True)
class Piece:
    """切分结果的一片。`heading_path` 是它的标题路径（citation 的来源）。"""

    heading_path: tuple[str, ...]
    text: str
    kinds: tuple[str, ...] = ()
    oversized: bool = False
    backend: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


# ---------------------------------------------------------------- 预算收敛


def _fit(
    text: str,
    *,
    heading_path: tuple[str, ...],
    chunk_size: int,
    chunk_overlap: int,
    backend: str,
) -> tuple[Piece, ...]:
    """把一段文本收敛到预算内，**且不破坏原子块**。

    为什么必须有这一步：保护层把代码块换成了短占位符，于是 LangChain
    **看不见它有多大** —— 一个 6KB 的示例代码块，在它眼里只有 20 个字符。
    还原回来就撑爆了预算（实测 p90 达 4211 字符，max 13400）。

    ⇒ 还原之后必须再收敛一次：超预算的代码块按空行分段（见 `split_code_block`），
      再按原子块装箱。**策略归 LangChain，原子性归这一层。**
    """
    blocks = fit_blocks(split_blocks(text), budget=chunk_size)
    pieces: list[Piece] = []
    for group in pack_blocks(blocks, budget=chunk_size, overlap_budget=chunk_overlap):
        body = "\n\n".join(b.text for b in group)
        if not body.strip():
            continue
        pieces.append(
            Piece(
                heading_path=heading_path,
                text=body,
                kinds=tuple(sorted({b.kind for b in group})),
                oversized=len(body) > chunk_size,
                backend=backend,
            )
        )
    return tuple(pieces)


# ---------------------------------------------------------------- langchain


def _split_langchain(
    markdown: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    headers: Sequence[tuple[str, str]],
) -> tuple[Piece, ...]:
    from langchain_text_splitters import (  # noqa: PLC0415 - 惰性 import（PR-14 精神）
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    protected, atoms = protect_atoms(markdown)

    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=list(headers),
        strip_headers=False,
    )
    recursive = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=list(_SEPARATORS),
    )

    sections = header_splitter.split_text(protected)
    # `split_documents` 会保留 metadata（标题路径）—— 这是结构感知切分的核心收益
    docs = recursive.split_documents(sections)

    keys = header_keys(headers)
    pieces: list[Piece] = []
    for doc in docs:
        path = heading_path_from(dict(doc.metadata), keys=keys)
        restored, _kinds = restore_atoms(doc.page_content, atoms)
        if not restored.strip():
            continue
        pieces.extend(
            _fit(
                restored,
                heading_path=path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                backend="langchain",
            )
        )
    return tuple(pieces)


# ---------------------------------------------------------------- stdlib


def _split_stdlib(
    markdown: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> tuple[Piece, ...]:
    """零依赖兜底：按 `##` 切节，节内按原子块装箱（字符预算）。

    与 langchain 后端的**差异**：不做"章节内字符级递归"，只做块级装箱 ——
    所以一个超长段落会被整体留下（不切开），而 langchain 会把它切小。
    这正是 A/B 对照要看的东西。

    ⚠️ 第一版这里有**静默内容丢失**：`heading` 初始为 `None`，
    而 `if heading is not None: buf.append(line)` 把"第一个 `##` 之前"的行
    —— 也就是 **h1 之下的整篇概述** —— 全丢掉了，一声不响。
    是 `test_every_unit_has_an_overview` 抓出来的。
    ⇒ 现在前导内容单独成一节，路径是 `(title,)`，上层据此标成 `overview`。
    """
    lines = markdown.splitlines()
    title = ""
    #: `heading is None` 的那一节就是 h1 之下的前导内容（概述）
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append((heading, text.splitlines()))

    for line in lines:
        stripped = line.strip()
        # h1 只认**第一行**的 `# ` —— `## ` 不匹配（第二个字符是 `#` 不是空格）
        if heading is None and not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            flush()
            heading = stripped[3:].strip()
            buf = []
            continue
        buf.append(line)
    flush()

    pieces: list[Piece] = []
    for section_heading, section_lines in sections:
        if section_heading is None:
            path: tuple[str, ...] = (title,) if title else ()
        else:
            path = (title, section_heading) if title else (section_heading,)
        pieces.extend(
            _fit(
                "\n".join(section_lines),
                heading_path=path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                backend="stdlib",
            )
        )
    return tuple(pieces)


# ---------------------------------------------------------------- 入口


def split_markdown(
    markdown: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    backend: str = "auto",
    headers: str = DEFAULT_HEADERS,
) -> tuple[Piece, ...]:
    """把一份 Markdown 切成带标题路径的片段。

    重叠以**原子块**为单位发生（不按字符重叠），才不会把代码/表格切成两半 ——
    这也是对 LangChain 那条 `chunk_overlap` 的收紧：它按字符重叠，
    在代码块密集的文档里可能把围栏撕开。

    `headers` 见 `HEADER_PRESETS`（`spec` / `deep`）。
    """
    chosen = active_backend(backend)
    resolved = headers_for(headers)
    if chosen == "langchain":
        return _split_langchain(
            markdown, chunk_size=chunk_size, chunk_overlap=chunk_overlap, headers=resolved
        )
    return _split_stdlib(markdown, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def pieces_to_blocks(piece: Piece) -> tuple[Block, ...]:
    """把一片还原成原子块（供上层按块统计 kind / 预算）。"""
    return split_blocks(piece.text)


def describe(pieces: Sequence[Piece], *, chunk_size: int = CHUNK_SIZE) -> dict[str, object]:
    """切分结果的自述（进 manifest，便于审计与 A/B 对照）。"""
    if not pieces:
        return {"pieces": 0}
    chars = [p.chars for p in pieces]
    toks = [p.tokens for p in pieces]
    return {
        "pieces": len(pieces),
        "backend": pieces[0].backend,
        "chunk_size": chunk_size,
        "chars_total": sum(chars),
        "chars_min": min(chars),
        "chars_max": max(chars),
        "chars_mean": round(sum(chars) / len(chars), 1),
        "tokens_total": sum(toks),
        "oversized": sum(1 for p in pieces if p.oversized),
        "with_code": sum(1 for p in pieces if "code" in p.kinds),
        "with_table": sum(1 for p in pieces if "table" in p.kinds),
    }
