"""Asuka 第二类语料的 RST 转换器：把 `.rst` 的结构翻成切分器认的 Markdown。

判据集中在一条：**切分器真正依赖的结构**要被翻出来（节标题 / 代码块 / 行内等宽），
其余的**原样留着**（宁可留噪声，不许把"材料在"变成"材料没了"）。
"""
from __future__ import annotations

import unittest

from asuka.rst import (
    RstConversionReport,
    convert_rst,
    split_module_title,
    strip_rst_header_comment,
)


class SectionTest(unittest.TestCase):
    def test_an_underline_title_becomes_a_markdown_heading(self) -> None:
        md, rep = convert_rst("Coroutines and tasks\n====================\n\nbody\n")
        self.assertIn("# Coroutines and tasks", md)
        self.assertEqual(rep.sections, 1)

    def test_nested_levels_follow_first_appearance_order(self) -> None:
        """`=` 先出现 → h1；`-` 后出现 → h2；`~` → h3（不是写死映射）。"""
        rst = (
            "Module\n======\n\n"
            "Section\n-------\n\n"
            "Sub\n~~~\n\n"
            "body\n"
        )
        md, rep = convert_rst(rst)
        self.assertIn("# Module", md)
        self.assertIn("## Section", md)
        self.assertIn("### Sub", md)
        self.assertEqual(rep.max_depth, 3)

    def test_a_short_underline_is_not_a_title(self) -> None:
        """装饰比文本短 → 不是标题（RST 规范），别误翻。"""
        md, rep = convert_rst("long long long title\n====\n")
        self.assertEqual(rep.sections, 0)
        self.assertNotIn("#", md)


class CodeBlockTest(unittest.TestCase):
    def test_a_code_block_becomes_a_fenced_block_with_language(self) -> None:
        rst = ".. code-block:: python\n\n    x = 1\n    print(x)\n"
        md, rep = convert_rst(rst)
        self.assertIn("```python", md)
        self.assertIn("x = 1", md)
        self.assertEqual(rep.code_blocks, 1)

    def test_code_block_handles_blank_lines_inside(self) -> None:
        rst = '.. code-block:: text\n\n    a\n\n    b\n'
        md, _ = convert_rst(rst)
        self.assertIn("a", md)
        self.assertIn("b", md)
        self.assertEqual(md.count("```"), 2)


class InlineTest(unittest.TestCase):
    def test_double_backticks_become_single(self) -> None:
        md, rep = convert_rst("Use ``asyncio.run()`` to run it.\n")
        self.assertIn("`asyncio.run()`", md)
        self.assertNotIn("``", md)
        self.assertEqual(rep.inline_literals, 1)


class KeptVerbatimTest(unittest.TestCase):
    """读不懂的不许丢 —— 原样留着（最坏是噪声，不是丢证据）。"""

    def test_versionadded_is_kept_as_a_marker(self) -> None:
        md, rep = convert_rst(".. versionadded:: 3.7\n\n    Some detail.\n")
        self.assertIn("versionadded", md)
        self.assertEqual(rep.directives_kept, 1)

    def test_an_unknown_directive_is_not_silently_dropped(self) -> None:
        md, rep = convert_rst(".. total-speculation:: whatever\n")
        self.assertIn("total-speculation", md)


class HeaderCommentTest(unittest.TestCase):
    def test_currentmodule_is_stripped(self) -> None:
        rst = ".. currentmodule:: asyncio\n\nTitle\n=====\n\nbody\n"
        out = strip_rst_header_comment(rst)
        self.assertNotIn("currentmodule", out)
        self.assertIn("Title", out)

    def test_module_and_default_role_are_stripped(self) -> None:
        rst = ".. module:: asyncio\n.. default-role:: \n\nTitle\n=====\n"
        out = strip_rst_header_comment(rst)
        self.assertNotIn("module::", out)


class TitleTest(unittest.TestCase):
    def test_module_title_is_the_first_section(self) -> None:
        rst = "Coroutines and tasks\n====================\n\n##?\n"
        self.assertEqual(split_module_title(rst), "Coroutines and tasks")

    def test_no_title_returns_empty(self) -> None:
        self.assertEqual(split_module_title("just a paragraph\n"), "")


class ReportTest(unittest.TestCase):
    def test_the_report_adds_up(self) -> None:
        rst = (
            "Mod\n===\n\n"
            "Sub\n---\n\n"
            "see ``x``\n\n"
            ".. code-block:: text\n\n    code\n\n"
            ".. versionchanged:: 3.0\n\n    note\n"
        )
        _md, rep = convert_rst(rst)
        self.assertEqual(rep.sections, 2)
        self.assertEqual(rep.code_blocks, 1)
        self.assertEqual(rep.inline_literals, 1)
        self.assertEqual(rep.directives_kept, 1)
        self.assertIsInstance(rep.as_dict(), dict)


if __name__ == "__main__":
    unittest.main()
