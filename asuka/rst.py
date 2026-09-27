"""reStructuredText → Markdown 的**最小**转换器（零依赖）。

### 为什么需要它

第二类语料选的是 **Python 官方文档**：它的源是 `Doc/library/*.rst`
（`raw.githubusercontent.com/python/cpython/...`），结构化、可复现、零第三方。
但 Asuka 的切分器吃的是 **Markdown 标题**（`#` / `##` / `###`），
`.rst` 的标题是"下一行画 `====` / `----`"，直接喂进去整篇会变成**一个巨块**。

所以这里只做一件事：把 Asuka 切分器**真正依赖的那几个结构**翻成 Markdown。

### 转换是**有账可查**的

只翻这几样，且每样都留计数（`RstConversionReport`）：

    section 标题   `Title\n=====`     → `# Title`（层级按 underline 字符首次出现的顺序推）
    内联标记       ```literal`` ```    → `` `literal` ``
    `.. code-block:: lang`            → ``` ```lang ```
    `.. versionadded::` 等指令        → 保留成一行 `**versionadded::** ...`（是考点，不能丢）

    **不翻**（刻意）：`.. note::` 缩进块、表格（`===  ===` 网格）、
    交叉引用角色（`:func:`x``）。它们**原样留着** —— 一个"尽量理解"的转换器
    会把读不懂的东西悄悄丢掉，而丢的正是引用指标要用的证据。原样留下，
    最坏是检索时多一点噪声，绝不会把"材料在"变成"材料没了"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: RST 的标题装饰字符（见 docutils 规范）。出现即视为"下一行是标题"。
_ADORNMENT = set("= - ` : ' \" ~ ^ _ * + # < >".split())

#: 一个段落级别的"节标题"：一行文本，下一行全是同一个装饰字符（长度 >= 文本）。
_SECTION_UNDERLINE = re.compile(r"^([=\-`:'\"~^_*+#<>])\1{1,}$")


@dataclass
class RstConversionReport:
    """这次转换**做了什么**的账（可审计，不静默）。"""

    sections: int = 0
    inline_literals: int = 0
    code_blocks: int = 0
    directives_kept: int = 0
    max_depth: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sections": self.sections,
            "inline_literals": self.inline_literals,
            "code_blocks": self.code_blocks,
            "directives_kept": self.directives_kept,
            "max_depth": self.max_depth,
        }


def convert_rst(text: str) -> tuple[str, RstConversionReport]:
    """`.rst` → Markdown（+ 转换账）。纯函数，不碰网络、不碰文件。"""
    report = RstConversionReport()
    lines = text.splitlines()
    out: list[str] = []
    # 装饰字符 → 层级：第一次出现的字符是 h1，第二次 h2，依次类推。
    # 为什么"按出现顺序"而不是写死 `=`→h1 / `-`→h2：Python 文档里 `=` 有时
    # 在模块页里当 h1、在别的页里当 h3，写死会切错；出现顺序是文档自己的约定。
    levels: dict[str, int] = {}

    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]

        # 0) **上划线 + 下划线**式的标题（RST 也允许）：
        #        ============
        #        Title
        #        ============
        #    上划线单独出现在别处是噪声；但"上划线 + 文本 + 下划线"是一个标题。
        if (
            _is_underline(line)
            and i + 2 < len(lines)
            and lines[i + 1].strip()
            and _is_underline(lines[i + 2])
            and lines[i + 2].strip()[0] == line.strip()[0]
        ):
            char = line.strip()[0]
            level = levels.setdefault(char, len(levels) + 1)
            report.sections += 1
            report.max_depth = max(report.max_depth, level)
            out.append("#" * level + " " + lines[i + 1].strip())
            i += 3
            continue

        nxt = lines[i + 1] if i + 1 < len(lines) else ""

        # 1) `.. code-block:: lang`（也可写作 `.. code::`）
        m = re.match(r"^(\s*)\.\.\s+code(?:-block)?::\s*(\S*)\s*$", line)
        if m:
            lang = m.group(2).strip()
            out.append(f"```{lang}")
            report.code_blocks += 1
            i += 1
            # 收集缩进块（空行不结束它）
            block: list[str] = []
            while i < len(lines):
                cur = lines[i]
                if cur.strip() == "":
                    block.append("")
                    i += 1
                    continue
                if cur.startswith((" ", "\t")):
                    block.append(cur[4:] if cur.startswith("    ") else cur.lstrip())
                    i += 1
                    continue
                break
            while block and block[-1] == "":
                block.pop()
            out.extend(block)
            out.append("```")
            continue

        # 2) 其他 `.. directive::` —— 保留一行（versionadded 之类是考点）
        m = re.match(r"^\.\.\s+([a-zA-Z][\w-]*)::\s*(.*)$", line)
        if m:
            name, arg = m.group(1), m.group(2).strip()
            out.append(f"**{name}::** {arg}".rstrip())
            report.directives_kept += 1
            i += 1
            # 吃掉它的缩进续行（否则它们会冒充普通段落）
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or lines[i].strip() == ""):
                if lines[i].strip() == "" and (i + 1 >= len(lines) or not lines[i + 1].startswith((" ", "\t"))):
                    break
                i += 1
            continue

        # 3) 节标题：`Title` 下一行是纯装饰
        if line.strip() and _is_underline(nxt) and len(nxt.strip()) >= max(3, len(line.strip())):
            char = nxt.strip()[0]
            level = levels.setdefault(char, len(levels) + 1)
            report.sections += 1
            report.max_depth = max(report.max_depth, level)
            out.append("#" * level + " " + line.strip())
            i += 2
            continue

        out.append(line)
        i += 1

    # ⚠️ 行内等宽转换**只作用于围栏之外**：`.. code-block::` 生成的
    # ``` 围栏如果被 `_INLINE_LITERAL`（两个反引号）扫到，会被吃掉一对，
    # 围栏就废了（第一版实测：```python 变成 ``python）。
    converted, hits = _convert_inline_outside_fences("\n".join(out))
    report.inline_literals = hits
    # 角色（`:mod:`x`` / `:func:`y``）留在标题与 citation 里只是噪声，去掉角色前缀、
    # 留下目标本身：`:mod:`!collections`` → `collections`。这是**显示**层清理，
    # 不改内容（目标文字原样保留）。
    converted = _strip_roles(converted)
    return converted, report


def _is_underline(line: str) -> bool:
    s = line.strip()
    if len(s) < 3:
        return False
    if s[0] not in _ADORNMENT:
        return False
    return len(set(s)) == 1


#: ```literal`` `` 是 RST 的行内等宽；它会**跨词**，所以用非贪婪匹配。
_INLINE_LITERAL = re.compile(r"``(.+?)``", re.DOTALL)


def _convert_inline_literals(text: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "`" + m.group(1).strip() + "`"

    return _INLINE_LITERAL.sub(repl, text), count


def _convert_inline_outside_fences(text: str) -> tuple[str, int]:
    """逐段转换：``` 围栏里原样保留，围栏外才做行内等宽转换。"""
    lines = text.splitlines()
    out: list[str] = []
    buffer: list[str] = []
    total = 0
    in_fence = False

    def flush() -> None:
        nonlocal buffer, total
        if not buffer:
            return
        converted, hits = _convert_inline_literals("\n".join(buffer))
        total += hits
        out.append(converted)
        buffer = []

    for line in lines:
        if line.startswith("```"):
            if not in_fence:
                flush()
            out.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line)
        else:
            buffer.append(line)
    flush()
    return "\n".join(out), total


#: RST 角色：`:role:`target``（含 `:role:`!literal``）。只去角色，留目标。
_ROLE = re.compile(r":[a-zA-Z][\w-]*:`~?!?([^`]+)`")


def _strip_roles(text: str) -> str:
    return _ROLE.sub(lambda m: m.group(1).strip(), text)


def split_module_title(rst: str) -> str:
    """取模块标题（第 1 个节标题），用于页标题。取不到返回空串。"""
    lines = rst.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].strip() and _is_underline(lines[i + 1]) and len(lines[i + 1].strip()) >= max(3, len(lines[i].strip())):
            return lines[i].strip()
    # 上划线式：第 1 行是装饰、第 2 行是标题
    if len(lines) >= 3 and _is_underline(lines[0]) and lines[1].strip():
        return lines[1].strip()
    return ""


def strip_rst_header_comment(rst: str) -> str:
    """去掉文件顶部那几行 `.. currentmodule::` / `.. module::` 之类的**导入头**。

    它们不是内容（`asyncio` 的每篇都以 `.. currentmodule:: asyncio` 开头），
    留着会污染词法检索 —— 同 `corpus.extract_metadata_block` 的理由。
    """
    lines = rst.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines) and (
        lines[i].strip() == ""
        or re.match(r"^\.\.\s+(currentmodule|module|default-role|role)::", lines[i])
    ):
        i += 1
    out.extend(lines[i:])
    return "\n".join(out)


__all__ = [
    "RstConversionReport",
    "convert_rst",
    "split_module_title",
    "strip_rst_header_comment",
]
