"""Task Dataset 的契约。

--------------------------------------------------------------------------
为什么校验器必须**一次报全部**

这是项目既有的一条纪律（M88）：拒绝"整体做不到"时，**查整份，不查"下一个"**。
一个数据集有 5 处坏就一次说完 5 处 ——
否则修一处跑一次，5 轮才收敛，而每一轮都像是"又发现一个新问题"。

--------------------------------------------------------------------------
为什么 evidence 不写 chunk_id

chunk_id 是**切分参数的函数**（`redis:set:007`）。
改一次 `chunk_size`，所有手写的 chunk_id 全失效 ——
而且失效是**静默的**：数据集还在、跑得通、只是 ground truth 全指向了别的地方。
写 `(unit_id, section)` 跨参数稳定，且解析失败**当场报错**。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from packages.agent_context.retrieval import Chunk

from asuka.dataset import (
    DIFFICULTIES,
    Dataset,
    DatasetError,
    Evidence,
    RequiredPoint,
    TaskItem,
    dataset_path,
)

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = _REPO / "asuka" / "corpus" / "redis" / "chunks.jsonl"
_DATASETS = _REPO / "asuka" / "datasets"


def _chunk(unit: str, section: str, idx: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"redis:{unit}:{idx:03d}",
        document_id=f"redis:{unit}",
        text=f"{section} body",
        citation=f"Redis · {unit.upper()} · {section}",
        attributes={"unit_id": unit, "section": section, "visibility": "public"},
    )


_CHUNKS = (
    _chunk("expire", "overview", 0),
    _chunk("expire", "command spec", 1),
    _chunk("expire", "overview", 2),
    _chunk("get", "overview", 0),
)


def _item(task_id: str = "t-1", **over) -> TaskItem:
    base = dict(
        task_id=task_id,
        question="q?",
        reference_answer="a.",
        source_document="redis:expire",
        difficulty="simple",
        evidence=(Evidence("expire", "overview"),),
    )
    base.update(over)
    return TaskItem(**base)  # type: ignore[arg-type]


class TestValidation(unittest.TestCase):
    def _errors(self, ds: Dataset) -> str:
        with self.assertRaises(DatasetError) as ctx:
            ds.validate(_CHUNKS)
        return str(ctx.exception)

    def test_valid_dataset_passes(self) -> None:
        Dataset(topic="redis", items=(_item(),)).validate(_CHUNKS)

    def test_reports_every_problem_at_once(self) -> None:
        """5 处坏 → 一次说完 5 处，而不是报第一个。"""
        ds = Dataset(
            topic="redis",
            items=(
                _item("t-1", difficulty="impossible"),
                _item("t-2", evidence=(Evidence("nosuchunit", "overview"),)),
                _item("t-3", evidence=(Evidence("expire", "nosuchsection"),)),
                _item("t-4", question="   "),
                _item("t-5", evidence=()),
            ),
        )
        msg = self._errors(ds)
        self.assertIn("5 处问题", msg)
        for frag in ("difficulty", "nosuchunit", "nosuchsection", "question 为空", "没有 evidence"):
            self.assertIn(frag, msg)

    def test_duplicate_task_id(self) -> None:
        ds = Dataset(topic="redis", items=(_item("dup"), _item("dup")))
        self.assertIn("task_id 重复", self._errors(ds))

    def test_unknown_unit_names_the_unit(self) -> None:
        ds = Dataset(topic="redis", items=(_item(evidence=(Evidence("nope", ""),)),))
        self.assertIn("nope", self._errors(ds))

    def test_unknown_section_lists_what_exists(self) -> None:
        """报错要**指出可选项** —— 只说"不对"会让人去翻文档。"""
        ds = Dataset(topic="redis", items=(_item(evidence=(Evidence("expire", "typo"),)),))
        msg = self._errors(ds)
        self.assertIn("typo", msg)
        self.assertIn("command spec", msg)

    def test_empty_section_matches_any(self) -> None:
        """`section=""` 表示"该单元任意节都算" —— 不该报错。"""
        Dataset(topic="redis", items=(_item(evidence=(Evidence("expire", ""),)),)).validate(_CHUNKS)

    def test_difficulty_vocabulary_is_closed(self) -> None:
        self.assertEqual(DIFFICULTIES, ("simple", "medium", "hard"))
        ds = Dataset(topic="redis", items=(_item(difficulty="hardest"),))
        self.assertIn("hardest", self._errors(ds))

    def test_missing_source_document(self) -> None:
        ds = Dataset(topic="redis", items=(_item(source_document=""),))
        self.assertIn("source_document 为空", self._errors(ds))


class TestResolve(unittest.TestCase):
    def test_maps_evidence_to_chunk_ids(self) -> None:
        ds = Dataset(topic="redis", items=(_item(),))
        got = ds.resolve(_CHUNKS)
        self.assertEqual(got["t-1"], ("redis:expire:000", "redis:expire:002"))

    def test_exact_section_is_narrow(self) -> None:
        ds = Dataset(topic="redis", items=(_item(evidence=(Evidence("expire", "command spec"),)),))
        self.assertEqual(ds.resolve(_CHUNKS)["t-1"], ("redis:expire:001",))

    def test_empty_section_widens_to_whole_unit(self) -> None:
        ds = Dataset(topic="redis", items=(_item(evidence=(Evidence("expire", ""),)),))
        got = ds.resolve(_CHUNKS)["t-1"]
        self.assertEqual(len(got), 3, "整个 expire 单元的三片都该算")

    def test_ids_are_deduplicated(self) -> None:
        """两处 evidence 指向同一片时不能算两遍 —— 否则 recall 的分母会虚高。"""
        ds = Dataset(
            topic="redis",
            items=(
                _item(evidence=(Evidence("expire", "overview"), Evidence("expire", "overview"))),
            ),
        )
        self.assertEqual(ds.resolve(_CHUNKS)["t-1"], ("redis:expire:000", "redis:expire:002"))

    def test_resolve_validates_first(self) -> None:
        ds = Dataset(topic="redis", items=(_item(evidence=(Evidence("nope", ""),)),))
        with self.assertRaises(DatasetError):
            ds.resolve(_CHUNKS)


class TestRoundTrip(unittest.TestCase):
    def test_save_load_round_trip(self) -> None:
        import tempfile

        ds = Dataset(topic="redis", version="9.9", items=(_item(),))
        with tempfile.TemporaryDirectory() as tmp:
            path = dataset_path(Path(tmp), "redis")
            self.assertEqual(ds.save(path), 1)
            back = Dataset.load(path)
        self.assertEqual(back.topic, "redis")
        self.assertEqual(back.version, "9.9")
        self.assertEqual(len(back.items), 1)
        self.assertEqual(back.items[0].evidence[0].unit_id, "expire")

    def test_meta_line_is_not_read_as_an_item(self) -> None:
        """`_meta` 行不能被当成一道题 —— 否则题目会凭空多一条。"""
        import tempfile

        ds = Dataset(topic="redis", items=(_item(),))
        with tempfile.TemporaryDirectory() as tmp:
            path = dataset_path(Path(tmp), "redis")
            ds.save(path)
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("_meta", lines[0])


@unittest.skipUnless(_CORPUS.exists(), "没有 asuka/corpus/redis/chunks.jsonl")
class TestRequiredPointsValidation(unittest.TestCase):
    """必答要点是**判分的尺子**，所以它自己也要能被证伪。

    一条要点如果**参考答案自己都答不到**，它是错的声明 —— 不是"这题难"。
    它会让这题**永久**低分，而读者会把它读成"模型不行"。
    """

    def _validate(self, *items):
        Dataset(topic="t", items=items).validate(_CHUNKS)

    def test_point_unreachable_from_the_reference_answer_is_refused(self) -> None:
        item = _item(
            required_points=(RequiredPoint("无人能答", ("zzz never appears",)),)
        )
        with self.assertRaises(DatasetError) as ctx:
            self._validate(item)
        self.assertIn("没有一个", str(ctx.exception))
        self.assertIn("无人能答", str(ctx.exception))

    def test_empty_any_of_is_refused(self) -> None:
        item = _item(required_points=(RequiredPoint("空的", ()),))
        with self.assertRaises(DatasetError) as ctx:
            self._validate(item)
        self.assertIn("any_of 为空", str(ctx.exception))

    def test_duplicate_label_is_refused(self) -> None:
        """同名要点在报告里分不清是哪条 —— 而报告正是按 label 归因的。"""
        item = _item(
            required_points=(
                RequiredPoint("同名", ("a.",)),
                RequiredPoint("同名", ("a.",)),
            )
        )
        with self.assertRaises(DatasetError) as ctx:
            self._validate(item)
        self.assertIn("label '同名' 重复", str(ctx.exception))

    def test_blank_label_is_refused(self) -> None:
        item = _item(required_points=(RequiredPoint("  ", ("a.",)),))
        with self.assertRaises(DatasetError) as ctx:
            self._validate(item)
        self.assertIn("缺 label", str(ctx.exception))

    def test_reports_every_point_problem_at_once(self) -> None:
        """同一条纪律：拒绝"整体做不到"时查整份，不查"下一个"。"""
        item = _item(
            required_points=(
                RequiredPoint("", ("a.",)),                    # 缺 label
                RequiredPoint("空的说法", ()),                  # any_of 为空
                RequiredPoint("找不到", ("zzz never appears",)),  # 参考答案答不到
                RequiredPoint("同名", ("a.",)),
                RequiredPoint("同名", ("a.",)),                 # 重复
            )
        )
        with self.assertRaises(DatasetError) as ctx:
            self._validate(item)
        msg = str(ctx.exception)
        self.assertIn("4 处问题", msg)

    def test_a_valid_point_set_passes(self) -> None:
        item = _item(required_points=(RequiredPoint("答案", ("a.",)), RequiredPoint("短答", ("a.",))))
        self._validate(item)   # 不抛就算过


class TestCuratedRedisDataset(unittest.TestCase):
    """人工整理的题目必须对**真实语料**可解析 —— 否则它只是看起来很整齐。"""

    @classmethod
    def setUpClass(cls) -> None:
        from asuka.corpus import read_chunks

        cls.chunks = read_chunks(_CORPUS)
        from asuka.datasets import build_redis

        cls.ds = build_redis()
        cls.resolved = cls.ds.resolve(cls.chunks)

    def test_validates_against_real_corpus(self) -> None:
        self.ds.validate(self.chunks)   # 不抛就算过

    def test_every_difficulty_is_covered(self) -> None:
        for level in DIFFICULTIES:
            with self.subTest(difficulty=level):
                self.assertGreaterEqual(len(self.ds.by_difficulty(level)), 3)

    def test_every_task_resolves_to_at_least_one_chunk(self) -> None:
        for task_id, ids in self.resolved.items():
            with self.subTest(task=task_id):
                self.assertTrue(ids, f"{task_id} 解析出 0 个 chunk")

    def test_evidence_points_at_distinct_chunks(self) -> None:
        """同一道题的 evidence 不该重复指向同一片。"""
        for task_id, ids in self.resolved.items():
            with self.subTest(task=task_id):
                self.assertEqual(len(ids), len(set(ids)))

    def test_questions_are_unique(self) -> None:
        questions = [i.question for i in self.ds.items]
        self.assertEqual(len(questions), len(set(questions)))

    def test_no_reference_answer_is_a_placeholder(self) -> None:
        for item in self.ds.items:
            with self.subTest(task=item.task_id):
                self.assertGreater(len(item.reference_answer), 60, "参考答案太短，多半是占位符")

    def test_saved_file_is_current(self) -> None:
        """落盘的 jsonl 必须和源码里的题目一致 —— 否则评测跑的是旧题。"""
        path = dataset_path(_DATASETS, "redis")
        if not path.exists():
            self.skipTest("还没落盘")
        self.assertEqual(len(Dataset.load(path).items), len(self.ds.items))

    def test_declared_gaps_survive_the_round_trip(self) -> None:
        """`out_of_corpus` 必须能落盘再读回 —— 否则评测跑的时候它是空的。

        报告里那一节读的是**落盘文件**，不是内存里的对象。
        字段只在内存里活着 = 报告里那一节永远不出现，而且不报错。
        """
        path = dataset_path(_DATASETS, "redis")
        if not path.exists():
            self.skipTest("还没落盘")
        loaded = Dataset.load(path)
        got = {i.task_id: i.out_of_corpus for i in loaded.items if i.out_of_corpus}
        want = {i.task_id: i.out_of_corpus for i in self.ds.items if i.out_of_corpus}
        self.assertEqual(got, want)
        self.assertTrue(want, "redis 题目集应当有声明（否则这条测试是空的）")

    def test_declared_gap_set_is_pinned(self) -> None:
        """钉住"哪些题有语料缺口"这件事本身。

        删掉一条声明会让报告里的告警消失，而**什么都不报错**。
        真要有变化（补了语料、改了题），改这条断言就是显式的动作。
        """
        declared = {i.task_id for i in self.ds.items if i.out_of_corpus}
        self.assertEqual(declared, {"r-hard-04", "r-hard-05", "r-hard-08"})

    def test_every_task_declares_required_points(self) -> None:
        """答案级指标的分母是**声明了要点的题**。漏一条，这题就静默地不在分母里。

        这条测试把"24 题全都有"钉死 —— 加新题时忘了写要点会立刻红。
        """
        missing = [i.task_id for i in self.ds.items if not i.required_points]
        self.assertEqual(missing, [], f"这些题没有必答要点：{missing}")
        self.assertEqual(len(self.ds.items), 24)

    def test_required_points_survive_the_round_trip(self) -> None:
        """报告读的是**落盘文件**。字段只在内存里活着 = 答案级评测永远不可测，且不报错。"""
        path = dataset_path(_DATASETS, "redis")
        if not path.exists():
            self.skipTest("还没落盘")
        loaded = {i.task_id: i.required_points for i in Dataset.load(path).items}
        want = {i.task_id: i.required_points for i in self.ds.items}
        self.assertEqual(loaded, want)

    def test_stats_exposes_the_answer_level_denominator(self) -> None:
        """`items_with_required_points` 就是答案级指标的分母 —— 必须能被读出来。"""
        s = self.ds.stats()
        self.assertEqual(s["items_with_required_points"], len(self.ds.items))
        self.assertEqual(s["required_points_total"], sum(len(i.required_points) for i in self.ds.items))
        self.assertGreater(s["required_points_total"], 50)


class TestCoverageLines(unittest.TestCase):
    """`coverage_lines()` 决定 CLI 什么时候印、印几条。"""

    def test_silent_when_nothing_is_declared(self) -> None:
        """没有声明就**一行都不印** —— 不要印"0 条"这种噪音。"""
        from asuka.datasets import coverage_lines

        ds = Dataset(topic="t", items=(_item("t-1"), _item("t-2", source_document="redis:get")))
        self.assertEqual(coverage_lines(ds), [])

    def test_one_line_per_declaration_with_task_id(self) -> None:
        """一条声明一行，且**带上题号** —— 只说"3 条题有问题"没法去改。"""
        from asuka.datasets import coverage_lines

        ds = Dataset(
            topic="t",
            items=(
                _item("t-1", out_of_corpus=("A 在语料里完全没有", "B 也没有")),
                _item("t-2", source_document="redis:get"),
            ),
        )
        lines = coverage_lines(ds)
        self.assertEqual(len(lines), 3)              # 1 行表头 + 2 行声明
        self.assertIn("1 条题", lines[0])
        self.assertTrue(lines[1].startswith("     t-1  A 在语料里完全没有"), lines[1])
        self.assertTrue(lines[2].startswith("     t-1  B 也没有"), lines[2])


if __name__ == "__main__":
    unittest.main()
