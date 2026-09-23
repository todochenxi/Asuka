"""文本原语：token 估算、原子块、以及"代码围栏 / 表格绝不跨切"的保护层。

--------------------------------------------------------------------------
为什么需要这一层

社区推荐用 LangChain 的 splitter（`MarkdownHeaderTextSplitter` +
`RecursiveCharacterTextSplitter`），这是对的 —— 它们是基准验证过的默认。

但有一个**它不保证、而我们必须保证**的事：

    `MarkdownHeaderTextSplitter` **不认代码围栏**。

Markdown 文档里的 shell 示例常常带 `# 这是注释`：

    ```bash
    # 阻塞 0 秒（不等待）
    BRPOP mylist 0
    ```

header splitter 会把 `# 阻塞 0 秒（不等待）` 当成一个一级标题，
**在代码块中间切开**。而社区结论很明确：

    "A table that crosses a chunk boundary is useless to retrieve."

所以这一层把代码围栏与表格**先替换成占位符**（各占一个独立段落），
让 splitter 在"没有这些结构"的文本上做它的启发式切分，
最后再把原子块**原样还原**回去。

    LangChain 负责「切分策略」；这一层负责「原子性」。
    两者不是替代关系 —— 前者是启发式，后者是硬约束。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

#: 占位符：独占一行、不含任何 Markdown 元字符，splitter 不会对它动手
PLACEHOLDER_FMT = "@@ASUKA_ATOM_{:04d}@@"
_PLACEHOLDER_RE = re.compile(r"@@ASUKA_ATOM_(\d{4})@@")


#: 每 token 几个字符 —— **这是语料的属性，不是实现细节**。
#:
#: 4 是英文文本的通行经验值，也是这份语料（redis.io 官方英文文档）的取值。
#: ⚠️ 内核 `agent_context.tokens.HeuristicTokenizer` 默认是 **3**（中文保守）。
#: 两边不一样**不是 bug** —— 但**必须都说出来**：同一个 chunk 在语料清单里
#: 和在 Context 装配时算出的 token 数如果不一样，而两边都不报错，
#: 那就没人能解释"预算为什么在这里就满了"。
#: 所以全包只有这一个常量，并且它**进报告**（见 `asuka/context.py`）。
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str, *, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """估算 token 数。

    **刻意用可复现的近似**，不引第三方 tokenizer —— 它的用途是
    **切分预算与成本核算**，不是精确计费。真实计费应该在拿到模型响应后
    用服务端返回的 usage 覆盖它。把"近似"写成"精确"才是这里唯一不可接受的错。

    算法**委托给内核的 `HeuristicTokenizer`**，不自己再写一遍 ——
    同族算法两处实现，改了一处就会出现"语料清单说 120 token、装配说 160 token"。
    """
    from packages.agent_context.tokens import HeuristicTokenizer

    return HeuristicTokenizer(chars_per_token).count(text)


@dataclass(frozen=True)
class Block:
    """一个**不可再切**的原子块：段落 / 代码围栏 / 表格。"""

    kind: str          # paragraph / code / table / piece
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def split_blocks(body: str) -> tuple[Block, ...]:
    """把一段 Markdown 切成原子块（代码围栏与表格各自成块）。"""
    lines = body.splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            buf = [line]
            i += 1
            while i < len(lines):
                buf.append(lines[i])
                if lines[i].strip().startswith(marker * 3):
                    i += 1
                    break
                i += 1
            blocks.append(Block("code", "\n".join(buf).strip()))
            continue
        if _TABLE_ROW_RE.match(line):
            buf = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(Block("table", "\n".join(buf).strip()))
            continue
        if not line.strip():
            i += 1
            continue
        buf = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not _FENCE_RE.match(lines[i])
            and not _TABLE_ROW_RE.match(lines[i])
        ):
            buf.append(lines[i])
            i += 1
        blocks.append(Block("paragraph", "\n".join(buf).strip()))
    return tuple(b for b in blocks if b.text)


def protect_atoms(markdown: str) -> tuple[str, tuple[Block, ...]]:
    """把代码围栏与表格换成占位符，返回 (受保护的文本, 原子块表)。"""
    lines = markdown.splitlines()
    atoms: list[Block] = []
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            buf = [line]
            i += 1
            while i < len(lines):
                buf.append(lines[i])
                if lines[i].strip().startswith(marker * 3):
                    i += 1
                    break
                i += 1
            atoms.append(Block("code", "\n".join(buf).strip()))
            out.extend(["", PLACEHOLDER_FMT.format(len(atoms) - 1), ""])
            continue
        if _TABLE_ROW_RE.match(line):
            buf = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            atoms.append(Block("table", "\n".join(buf).strip()))
            out.extend(["", PLACEHOLDER_FMT.format(len(atoms) - 1), ""])
            continue
        out.append(line)
        i += 1
    return "\n".join(out), tuple(atoms)


def restore_atoms(text: str, atoms: Sequence[Block]) -> tuple[str, tuple[str, ...]]:
    """还原占位符，返回 (还原后的文本, 这一片里出现的原子块种类)。"""
    kinds: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(atoms):
            kinds.append(atoms[idx].kind)
            return atoms[idx].text
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text), tuple(sorted(set(kinds)))


def pack_blocks(
    blocks: Sequence[Block],
    *,
    budget: int,
    overlap_budget: int,
) -> list[tuple[Block, ...]]:
    """把原子块贪心装箱（stdlib 后端与"还原后仍超预算"的兜底都用它）。

    预算是**字符**（与 LangChain `RecursiveCharacterTextSplitter` 的默认
    `length_function=len` 同一语义，便于两个后端 A/B 对照）。

    * 一个块永远不被切开；超预算的块**独占一个 chunk**
    * 重叠以**块**为单位（重复上一箱的最后一块，如果它装得下）——
      不按字符重叠，就不会把代码块或表格切成两半
    """
    out: list[tuple[Block, ...]] = []
    current: list[Block] = []
    used = 0
    for block in blocks:
        if block.chars > budget:
            if current:
                out.append(tuple(current))
                current, used = [], 0
            out.append((block,))
            continue
        if used + block.chars > budget and current:
            out.append(tuple(current))
            tail = current[-1]
            current = [tail] if tail.chars <= overlap_budget else []
            used = sum(b.chars for b in current)
        current.append(block)
        used += block.chars
    if current:
        out.append(tuple(current))
    return out


def heading_path_from(metadata: dict[str, str], *, keys: Sequence[str]) -> tuple[str, ...]:
    """从 splitter 的 metadata 里取出标题路径（社区结构感知切分的核心收益）。"""
    return tuple(metadata[k] for k in keys if metadata.get(k))


def split_code_block(block: Block, budget: int) -> tuple[Block, ...]:
    """把一个超预算的代码块按**空行**拆成若干段，每段重新加上围栏。

    这是社区"代码按函数 / 类级别切分"在 shell / 示例代码上的对应做法：

    * **不按字符切** —— 那会把一行命令劈成两半
    * 按空行分段（示例之间通常以空行分隔），每段仍是**语法完整**的
    * 每段重新加上围栏标记，所以每一段单独看仍是一个合法的代码块

    返回的每一段都可能仍超预算（一整段无法再分），那种情况由调用方标记 oversized。
    """
    lines = block.text.splitlines()
    if len(lines) < 3:
        return (block,)
    opener = lines[0]
    closer = lines[-1] if lines[-1].strip().startswith(("```", "~~~")) else ""
    inner = lines[1:-1] if closer else lines[1:]
    if not inner:
        return (block,)

    runs: list[list[str]] = []
    current: list[str] = []
    for line in inner:
        if not line.strip() and current:
            runs.append(current)
            current = []
            continue
        current.append(line)
    if current:
        runs.append(current)
    if len(runs) <= 1:
        return (block,)

    overhead = len(opener) + len(closer) + 2
    groups: list[list[str]] = []
    buf: list[str] = []
    used = overhead
    for run in runs:
        size = sum(len(x) + 1 for x in run)
        if used + size > budget and buf:
            groups.append(buf)
            buf = []
            used = overhead
        buf.extend(run)
        used += size
    if buf:
        groups.append(buf)
    if len(groups) <= 1:
        return (block,)
    return tuple(
        Block("code", "\n".join([opener, *g, closer]) if closer else "\n".join([opener, *g]))
        for g in groups
    )


def fit_blocks(blocks: Sequence[Block], *, budget: int) -> tuple[Block, ...]:
    """把原子块整理成"每块都不超预算"的序列（超预算的代码块按空行再分）。"""
    out: list[Block] = []
    for block in blocks:
        if block.kind == "code" and block.chars > budget:
            out.extend(split_code_block(block, budget))
        else:
            out.append(block)
    return tuple(out)
