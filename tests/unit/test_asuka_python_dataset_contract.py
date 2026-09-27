"""Asuka Python 任务集：形状与判据契约。

重点不是"题目对不对"（那要人读），而是**尺子本身成立**：
每个 required_point 至少有一种说法出现在参考答案里（否则那条要点无人能答），
每条 evidence 能解析到真实存在的 chunk。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from asuka.corpus import read_chunks
from asuka.datasets import build_python

ROOT = Path(__file__).resolve().parents[2]
CHUNKS = ROOT / "asuka" / "corpus" / "python" / "chunks.jsonl"


class PythonDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ds = build_python()

    def test_the_three_difficulties_are_all_present(self) -> None:
        levels = {i.difficulty for i in self.ds.items}
        self.assertEqual(levels, {"simple", "medium", "hard"})

    def test_every_task_has_a_reference_answer(self) -> None:
        for item in self.ds.items:
            self.assertTrue(item.reference_answer.strip(), item.task_id)
            self.assertTrue(item.required_points, item.task_id)

    def test_every_required_point_is_answerable(self) -> None:
        """`any_of` 里至少有一种说法出现在参考答案里 —— 否则是条无人能答的要点。"""
        for item in self.ds.items:
            for point in item.required_points:
                self.assertTrue(
                    point.matched_by(item.reference_answer),
                    f"{item.task_id}: required_point {point.label!r} "
                    f"has no variant present in the reference answer",
                )

    @unittest.skipUnless(CHUNKS.exists(), "python corpus missing")
    def test_evidence_resolves_against_the_real_corpus(self) -> None:
        """`resolve()` 会核对每个 (unit, section) 真实存在 —— 对不上就抛。

        没有独立断言就没有红灯：这里显式断言"语料里真的能查到那一节"。
        """
        chunks = read_chunks(CHUNKS)
        self.ds.resolve(chunks)  # 写错节名会在这里抛 DatasetError
        known = {(c.attributes["unit_id"], c.attributes["section"]) for c in chunks}
        for item in self.ds.items:
            for ev in item.evidence:
                self.assertIn(
                    (ev.unit_id, ev.section), known,
                    f"{item.task_id}: evidence {ev.unit_id}/{ev.section} not in corpus",
                )

    def test_task_ids_are_unique(self) -> None:
        ids = [i.task_id for i in self.ds.items]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
