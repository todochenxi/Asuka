"""Asuka 提示词与回答解析的契约（从 `test_asuka_deepseek_contract` 演化而来）。

模型调用已移到 AgentOS（`asuka.agentos_eval`），所以这一层只剩**纯函数**：

    build_prompt      问题 + 上下文 → 提示词
    parse_response    模型输出 → (答案文本, 自述引用 | None)
    prompt_id_for     提示词身份（版本 + 内容指纹）

⚠️ 全程不发网络请求 —— 这里根本没有网络代码了。
"""
from __future__ import annotations

import unittest

import asuka.prompting as p


class _Ctx:
    """带 `.key` / `.text` 的最小上下文（`build_prompt` 是 duck-typed 的）。"""

    def __init__(self, key: str, text: str) -> None:
        self.key = key
        self.text = text


class BuildPromptTest(unittest.TestCase):
    def test_each_context_is_labelled_with_its_key(self) -> None:
        p_text = p.build_prompt(
            "What does EXPIRE do?",
            [_Ctx("redis:expire:000", "EXPIRE sets a timeout"), _Ctx("redis:ttl:001", "TTL")],
        )
        self.assertIn("[[redis:expire:000]]", p_text)
        self.assertIn("[[redis:ttl:001]]", p_text)
        self.assertIn("What does EXPIRE do?", p_text)

    def test_empty_context_says_so_instead_of_looking_like_a_document(self) -> None:
        p_text = p.build_prompt("q", [])
        self.assertIn("没有可用的上下文", p_text)


class ParseResponseTest(unittest.TestCase):
    def _ctxs(self):
        return [_Ctx("redis:expire:000", "x"), _Ctx("redis:ttl:001", "y")]

    def test_plain_json(self) -> None:
        text, cites = p.parse_response(
            '{"answer":"EXPIRE sets a timeout.","citations":["redis:expire:000"]}',
            self._ctxs(),
        )
        self.assertEqual(text, "EXPIRE sets a timeout.")
        self.assertEqual(cites, ("redis:expire:000",))

    def test_fenced_json_is_extracted(self) -> None:
        text, cites = p.parse_response(
            '```json\n{"answer":"a","citations":[]}\n```', self._ctxs()
        )
        self.assertEqual(text, "a")
        self.assertEqual(cites, ())

    def test_numeric_index_maps_to_the_key_it_pointed_at(self) -> None:
        _, cites = p.parse_response('{"answer":"a","citations":[1]}', self._ctxs())
        self.assertEqual(cites, ("redis:expire:000",))

    def test_an_unknown_id_is_kept_so_it_can_be_judged_as_fabricated(self) -> None:
        _, cites = p.parse_response(
            '{"answer":"a","citations":["ghost-chunk-0001"]}', self._ctxs()
        )
        self.assertEqual(cites, ("ghost-chunk-0001",))

    def test_no_json_degrades_to_text_and_unmeasurable(self) -> None:
        """模型说了话但没有 JSON：要点召回照判，引用**不可测**（不是引了 0 条）。"""
        text, cites = p.parse_response("模型说了些没有 JSON 的话", self._ctxs())
        self.assertEqual(text, "模型说了些没有 JSON 的话")
        self.assertIsNone(cites)

    def test_non_list_citations_is_unmeasurable(self) -> None:
        _, cites = p.parse_response('{"answer":"a","citations":"nope"}', self._ctxs())
        self.assertIsNone(cites)

    def test_explicit_empty_list_is_measurable_zero(self) -> None:
        _, cites = p.parse_response('{"answer":"a","citations":[]}', self._ctxs())
        self.assertEqual(cites, ())


class PromptIdentityTest(unittest.TestCase):
    def test_versions_have_distinct_content_and_ids(self) -> None:
        self.assertNotEqual(p.PROMPT_VERSIONS["v1"], p.PROMPT_VERSIONS["v2"])
        self.assertNotEqual(p.prompt_id_for("v1"), p.prompt_id_for("v2"))

    def test_id_is_version_plus_content_fingerprint(self) -> None:
        pid = p.prompt_id_for("v1")
        self.assertTrue(pid.startswith("v1-"))
        self.assertEqual(len(pid.split("-", 1)[1]), 8)

    def test_unknown_version_is_refused_not_silently_defaulted(self) -> None:
        with self.assertRaises(ValueError):
            p.system_prompt("v3")
        with self.assertRaises(ValueError):
            p.prompt_id_for("v3")

    def test_default_version_is_declared(self) -> None:
        self.assertIn(p.DEFAULT_PROMPT_VERSION, p.PROMPT_VERSIONS)


class CliNoLongerOffersDeepseekTest(unittest.TestCase):
    def test_deepseek_is_not_an_answerer_choice_anymore(self) -> None:
        """Asuka 不再直接调模型 —— CLI 里 `deepseek` 必须被 argparse 拒绝。"""
        from asuka.answers import build_parser

        for name in ("oracle", "null", "fabricator"):
            self.assertEqual(
                build_parser().parse_args(["--answerer", name]).answerer, name
            )
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--answerer", "deepseek"])

    def test_prompt_version_flag_is_gone(self) -> None:
        from asuka.answers import build_parser

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--prompt-version", "v2"])


if __name__ == "__main__":
    unittest.main()
