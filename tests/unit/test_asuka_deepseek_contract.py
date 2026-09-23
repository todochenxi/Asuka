"""真模型答案器（DeepSeek）的契约。

--------------------------------------------------------------------------
这批测试锁的是 #116 的另一半：**模型侧**那几个数（`prompt_tokens` /
`completion_tokens` / `cost_usd` / `latency_ms`）**现在真的测了**，
而不是像 `oracle` / `null` 那样填 0（"没测"≠"免费"，见 `answers.py`）。

纪律（和答案级判据同源）：

1. 引用必须是模型**自述**的 chunk_id，不是我们推的 ⇒ 提示词显式标 `[[key]]`，
   解析只接受模型回的那些 key（序号只在范围内才回退成 key）。
2. 代价必须真测 ⇒ 取自 DeepSeek 的 `usage`，cost 按**声明单价**算。

⚠️ 全程**不发网络请求**：`DeepSeekAnswerer._post_chat` 是注入点，测试用替身替换。
所以这批测试能在零依赖解释器上跑。
"""
from __future__ import annotations

import dataclasses
import io
import json
import math
import unittest
from contextlib import redirect_stderr
from dataclasses import dataclass
from typing import Any
from unittest import mock

import asuka.deepseek as dsmod
from asuka.answers import Answer, build_parser, evaluate_answers, main as answers_main
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter
from asuka.textutil import CHARS_PER_TOKEN


# ---------------------------------------------------------------- 微型脚手架


@dataclass
class _Ctx:
    """只给 `build_prompt` / `_parse_response` 用的极简上下文替身。"""

    key: str
    text: str


def _chunk(cid: str, text: str, unit: str, visibility: str = "public") -> Any:
    from packages.agent_context.retrieval import Chunk

    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · overview",
        attributes={"unit_id": unit, "section": "overview", "visibility": visibility},
    )


_CHUNKS = (
    _chunk("redis:expire:000", "# EXPIRE\n\nSets a timeout on a key.", "expire"),
    _chunk("redis:ttl:000", "# TTL\n\nReturns the remaining time to live.", "ttl"),
)


def _item(
    task_id: str,
    *,
    points: tuple[RequiredPoint, ...],
    evidence: tuple[Evidence, ...],
    reference_answer: str = "EXPIRE sets a timeout on a key, so the key is deleted.",
    question: str = "What does EXPIRE do?",
    source_document: str = "redis:expire",
) -> TaskItem:
    return TaskItem(
        task_id=task_id,
        question=question,
        reference_answer=reference_answer,
        source_document=source_document,
        difficulty="simple",
        evidence=evidence,
        required_points=points,
    )


def _dataset() -> Dataset:
    items = [
        _item(
            "t-expire",
            points=(RequiredPoint("设超时", ("sets a timeout",)),),
            evidence=(Evidence("expire", "overview"),),
        ),
        _item(
            "t-ttl",
            points=(RequiredPoint("返回剩余", ("remaining",)),),
            evidence=(Evidence("ttl", "overview"),),
            reference_answer="TTL returns the remaining time to live of a key.",
            question="What does TTL return?",
            source_document="redis:ttl",
        ),
    ]
    ds = Dataset(topic="redis", items=tuple(items))
    ds.resolve(_CHUNKS)
    return ds


def _kb() -> KnowledgeBase:
    r = BM25Retriever(tuple(_CHUNKS))
    return KnowledgeBase(
        topic="redis",
        pipeline=__import__("packages.agent_context.retrieval", fromlist=["RetrievalPipeline"]).RetrievalPipeline(
            retriever=r, permission=PublicCorpusFilter()
        ),
        retriever=r,
        kind="bm25",
    )


def _fake_post(content: str, *, prompt_tokens: int = 120, completion_tokens: int = 20):
    """返回一个 DeepSeek 风格的响应字典（替身）。"""

    def _post(answerer: Any, messages: Any) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    return _post


def _fake_auto_cite(*, prompt_tokens: int = 120, completion_tokens: int = 20):
    """替身：把提示词里出现的全部 `[[key]]` 都引上 —— 模拟"老实引用了拿到的所有片段"。"""

    import re

    def _post(answerer: Any, messages: Any) -> dict[str, Any]:
        user = messages[1]["content"]
        # 只认含冒号的真实 chunk_id（提示词示例里的 `[[key]]` 占位符不含冒号，排除）。
        keys = [k for k in re.findall(r"\[\[([^\[\]]+)\]\]", user) if ":" in k]
        payload = json.dumps({"answer": "据片段回答。", "citations": keys}, ensure_ascii=False)
        return {
            "choices": [{"message": {"content": payload}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    return _post


class TestPromptCarriesWhatTheModelNeeds(unittest.TestCase):
    def test_prompt_includes_the_question_and_every_context_key(self) -> None:
        """模型要答什么、能引哪些 key，都得在提示词里写明白。"""
        ctxs = [_Ctx("redis:expire:000", "EXPIRE sets a timeout."), _Ctx("redis:ttl:000", "TTL remains.")]
        p = dsmod.build_prompt("What does EXPIRE do?", ctxs)
        self.assertIn("What does EXPIRE do?", p)
        self.assertIn("[[redis:expire:000]]", p)
        self.assertIn("[[redis:ttl:000]]", p)


class TestResponseParsing(unittest.TestCase):
    def test_answer_text_and_citations_are_extracted(self) -> None:
        j = '{"answer":"EXPIRE sets a timeout.","citations":["[[redis:expire:000]]"]}'
        text, cites = dsmod._parse_response(j, [_Ctx("redis:expire:000", "x")])
        self.assertEqual(text, "EXPIRE sets a timeout.")
        self.assertEqual(cites, ("redis:expire:000",))

    def test_index_citations_map_to_real_keys(self) -> None:
        """模型回序号 `[1]` 时，只在范围内才映射成真正的 key。"""
        ctxs = [_Ctx("redis:expire:000", "a"), _Ctx("redis:ttl:000", "b")]
        j = '{"answer":"q","citations":["[[redis:expire:000]]","2"]}'
        _, cites = dsmod._parse_response(j, ctxs)
        self.assertEqual(cites, ("redis:expire:000", "redis:ttl:000"))

    def test_empty_citations_mean_zero_not_unmeasured(self) -> None:
        """明确说"没引任何来源" ⇒ `()`（可测、召回 0），**不是** `None`。"""
        _, cites = dsmod._parse_response('{"answer":"x","citations":[]}', [_Ctx("k", "v")])
        self.assertEqual(cites, ())

    def test_unparseable_response_is_unmeasured_not_silent_zero(self) -> None:
        """解析不到 JSON ⇒ 原文保留、引用 `None`（不可测、会点名）。

        绝不许静默当成"引了 0 条"——那会把"没测"读成"没依据"。"""
        text, cites = dsmod._parse_response("模型说了些没 JSON 的话", [_Ctx("k", "v")])
        self.assertEqual(text, "模型说了些没 JSON 的话")
        self.assertIsNone(cites)


class TestDeepSeekAnswererInjected(unittest.TestCase):
    def test_cost_is_computed_from_returned_usage(self) -> None:
        a = dsmod.DeepSeekAnswerer(
            input_cost_per_million=0.27, output_cost_per_million=1.10
        )
        a._post_chat = _fake_post('{"answer":"x","citations":[]}', prompt_tokens=100, completion_tokens=20)
        ans = a.answer(_item("t-x", points=(), evidence=()), [_Ctx("k", "v")])
        expected = 100 / 1_000_000 * 0.27 + 20 / 1_000_000 * 1.10
        self.assertTrue(math.isclose(ans.cost_usd, expected, rel_tol=1e-9))
        self.assertEqual(ans.prompt_tokens, 100)
        self.assertEqual(ans.completion_tokens, 20)
        self.assertGreaterEqual(ans.latency_ms, 0.0)

    def test_citation_flows_through_to_the_score(self) -> None:
        a = dsmod.DeepSeekAnswerer()
        a._post_chat = _fake_auto_cite()
        rep = evaluate_answers(_kb(), _dataset(), a, top_k=2, corpus_chunks=len(_CHUNKS))
        self.assertFalse(rep.calibration, "真模型必须自述 is_calibration=False")
        # 引用自述了真实 key ⇒ grounded_rate 可测且为 1.0（没编）
        self.assertEqual(rep.overall.grounded_rate, 1.0)
        # 代价真的记了，不是 0
        self.assertGreater(rep.overall.total_tokens, 0)
        self.assertGreater(rep.overall.total_cost_usd, 0.0)

    def test_a_fabricated_citation_is_caught(self) -> None:
        """模型引了一个没给它的 key ⇒ 算编造，报告里点名。"""
        a = dsmod.DeepSeekAnswerer()
        a._post_chat = _fake_post('{"answer":"x","citations":["ghost-chunk-0001"]}')
        rep = evaluate_answers(_kb(), _dataset(), a, top_k=2, corpus_chunks=len(_CHUNKS))
        self.assertGreater(rep.overall.fabricated_total, 0, "引了没给它的 key 必须被记成编造")

    def test_chat_failure_is_recorded_not_raised(self) -> None:
        """单次生成失败写 `error`，**不**炸掉整轮评测。"""

        def _boom(answerer: Any, messages: Any) -> dict[str, Any]:
            raise dsmod._ChatError("boom")

        a = dsmod.DeepSeekAnswerer()
        a._post_chat = _boom
        rep = evaluate_answers(_kb(), _dataset(), a, top_k=2, corpus_chunks=len(_CHUNKS))
        # 至少有一条样本带着 error
        self.assertTrue(any(s.error for s in rep.items))
        self.assertFalse(rep.calibration)


class TestCliWiring(unittest.TestCase):
    def test_deepseek_is_a_valid_answerer_choice(self) -> None:
        ns = build_parser().parse_args(["--answerer", "deepseek"])
        self.assertEqual(ns.answerer, "deepseek")

    def test_default_answerer_is_oracle(self) -> None:
        self.assertEqual(build_parser().parse_args([]).answerer, "oracle")

    def test_deepseek_answerer_module_is_importable_on_zero_dep_interpreter(self) -> None:
        # 触达一次 import 路径，确保没在顶部引入第三方。
        self.assertTrue(hasattr(dsmod, "DeepSeekAnswerer"))

    def test_missing_api_key_exits_with_error_not_silent(self) -> None:
        """没 DEEPSEEK_API_KEY 时，**早退**并说清原因，不静默当免费。"""
        stderr = io.StringIO()
        # 确定性地把 key 压成空（不管运行环境有没有设）。
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), redirect_stderr(stderr):
            code = answers_main(["redis", "--answerer", "deepseek"])
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", stderr.getvalue())


class _FakeResp:
    """模拟 `urllib` 的成功响应（上下文管理器 + `read()`）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._bytes


class TestRealHttpPath(unittest.TestCase):
    """不走注入替身，验证**真实** `urllib` 路径（请求构造 + 响应解析 + 错误）。

    不发网络请求：`urllib.request.urlopen` 被 mock 掉。
    """

    def test_real_path_builds_request_parses_response(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"answer": "EXPIRE sets a timeout.", "citations": ["[[redis:expire:000]]"]}
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        a = dsmod.DeepSeekAnswerer()
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=_FakeResp(payload)
        ):
            ans = a.answer(_item("t-x", points=(), evidence=()), [_Ctx("redis:expire:000", "x")])
        self.assertEqual(ans.text, "EXPIRE sets a timeout.")
        self.assertEqual(ans.citations, ("redis:expire:000",))
        self.assertEqual(ans.prompt_tokens, 10)
        self.assertEqual(ans.completion_tokens, 5)
        self.assertAlmostEqual(ans.cost_usd, 10 / 1e6 * 0.27 + 5 / 1e6 * 1.10)
        self.assertEqual(ans.error, "")

    def test_http_error_is_recorded_not_raised(self) -> None:
        import urllib.error

        a = dsmod.DeepSeekAnswerer()
        err = urllib.error.HTTPError(
            url="https://api.deepseek.com/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,  # type: ignore[arg-type]
        )

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise err

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "bad-key"}), mock.patch(
            "urllib.request.urlopen", side_effect=_raise
        ):
            ans = a.answer(_item("t-x", points=(), evidence=()), [_Ctx("k", "v")])
        self.assertEqual(ans.text, "")
        self.assertTrue(ans.error.startswith("deepseek: HTTP 401"))


if __name__ == "__main__":
    unittest.main()
