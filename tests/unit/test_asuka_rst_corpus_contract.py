"""Asuka 第二类语料的**摄取**契约：`.rst` 源 → 与 redis 同形的 chunks。

判据：不同语料产出**同一种东西** —— citation 形状、chunk_id 前缀、
attributes 字段、manifest 结构都不变，下游（kb / evaluate / answers）零改动。
用**缓存的 .rst**（`asuka/raw/python/*.rst`，随源码进版本库）跑，不碰网络。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from asuka.corpus import load_topic, read_chunks
from asuka.corpus_rst import ingest_rst

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "asuka" / "raw"


class RegistryTest(unittest.TestCase):
    def test_the_python_topic_loads(self) -> None:
        topic = load_topic("python")
        self.assertEqual(topic.topic, "python")
        self.assertEqual(topic.display_name, "Python")
        self.assertEqual(topic.headers, "deep")
        self.assertGreaterEqual(len(topic.units), 10)

    def test_every_unit_id_is_a_safe_filename(self) -> None:
        """unit_id 会拼进 URL 和文件名 —— 不许有 `/` 之类。"""
        for unit in load_topic("python").units:
            self.assertNotIn("/", unit.unit_id)
            self.assertNotIn("..", unit.unit_id)


@unittest.skipUnless((RAW / "python" / "json.rst").exists(), "python .rst cache missing")
class IngestFromCacheTest(unittest.TestCase):
    def _ingest(self, only: list[str]) -> tuple[object, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="asuka-py-"))
        report = ingest_rst(
            "python", raw_dir=RAW, corpus_dir=tmp, offline=True, only=only, backend="stdlib"
        )
        return report, tmp

    def test_it_produces_chunks_from_cached_rst(self) -> None:
        report, tmp = self._ingest(["json"])
        self.assertEqual(report.units_failed, [])
        self.assertTrue(report.chunks)
        chunks = read_chunks(tmp / "python" / "chunks.jsonl")
        self.assertEqual(len(chunks), len(report.chunks))
        self.assertTrue(all(c.attributes["topic"] == "python" for c in chunks))

    def test_citations_have_the_same_shape_as_redis(self) -> None:
        _report, tmp = self._ingest(["json"])
        chunks = read_chunks(tmp / "python" / "chunks.jsonl")
        for c in chunks:
            self.assertTrue(c.citation.startswith("Python · "), c.citation)
            # citation 里不许残留 RST 角色/装饰
            self.assertNotIn(":mod:", c.citation)
            self.assertNotIn("====", c.citation)

    def test_chunk_ids_carry_the_document_prefix(self) -> None:
        _report, tmp = self._ingest(["json"])
        chunks = read_chunks(tmp / "python" / "chunks.jsonl")
        for c in chunks:
            self.assertTrue(c.chunk_id.startswith("python:json:"), c.chunk_id)

    def test_section_headings_are_converted_not_left_as_rst(self) -> None:
        """节标题必须变成 Markdown `#` —— 否则整篇切不出节，退化成一个巨块。

        判据是"装饰行没了、`#` 有了"，不是"标题文字变成别的"：
        标题文字（`json --- JSON encoder and decoder`）本来就是内容，该留着。
        """
        _report, tmp = self._ingest(["json"])
        chunks = read_chunks(tmp / "python" / "chunks.jsonl")
        joined = "\n".join(c.text for c in chunks)
        # 段落标记 `### Character Encodings` 在 RST 里是纯文本，转换后应是 `### `
        self.assertIn("#", joined)
        # 标题的装饰行（一整行只有装饰字符）不该作为正文出现
        for line in joined.splitlines():
            self.assertFalse(
                line.strip() and set(line.strip()) == {"="} and len(line.strip()) >= 3,
                f"RST 标题装饰行漏进了正文：{line!r}",
            )

    def test_the_manifest_records_the_rst_conversion(self) -> None:
        _report, tmp = self._ingest(["json"])
        manifest = json.loads((tmp / "python" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_format"], "rst")
        self.assertIn("conversion", manifest["summary"])
        self.assertGreater(manifest["summary"]["conversion"]["sections"], 0)

    def test_two_units_produce_more_than_one(self) -> None:
        report, tmp = self._ingest(["json", "dataclasses"])
        self.assertEqual(len(report.units_ok), 2)
        chunks = read_chunks(tmp / "python" / "chunks.jsonl")
        self.assertEqual({c.document_id for c in chunks}, {"python:json", "python:dataclasses"})


class RealCachedPythonCorpusTest(unittest.TestCase):
    """整份 Python 语料（20 篇）—— 只在缓存齐了才跑。"""

    def test_the_full_corpus_ingests(self) -> None:
        raw = RAW / "python"
        if len(list(raw.glob("*.rst"))) < 20:
            self.skipTest("python .rst cache is incomplete")
        tmp = Path(tempfile.mkdtemp(prefix="asuka-py-all-"))
        report = ingest_rst(
            "python", raw_dir=RAW, corpus_dir=tmp, offline=True, backend="stdlib"
        )
        self.assertEqual(report.units_failed, [])
        self.assertEqual(len(report.units_ok), 20)
        self.assertGreater(len(report.chunks), 300)
        # max_depth 是**跨文档取最大**，不是累加（累加过一次，报了 55，实为 bug）
        self.assertLessEqual(report.conversion["max_depth"], 6)


if __name__ == "__main__":
    unittest.main()
