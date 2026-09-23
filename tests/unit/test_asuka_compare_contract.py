"""对照模块的契约：**先验可比性，不可比就拒绝**。

--------------------------------------------------------------------------
锁两件事

**一、报告文件必须与自己的输入自洽**

`recall` / `precision` / `mrr` / 上限都是**派生量**。文件里存了一份，
但 `load()` 会按 `evidence` / `retrieved` / `hits` **重算**并核对。

这不是洁癖 —— 实测抓到过：`redis-bm25-20260923-012425.json` 里
`context_recall_ceiling` 存的是 `1.0000`，按单题重算是 `0.9568`。
它写于**加上限之前的版本**。把这种报告和新的并排比，
比出来的差**全是指标定义的差，不是检索的差**。

**二、不可比的对照表比没有对照表更糟**

因为它**看起来是结论**。所以 `top_k` 不同、题目集合不同、ground truth 不同、
或者一边是冒烟 —— 全都拒绝，并说清是哪一件。
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from packages.agent_context.retrieval import Chunk, RetrievalPipeline

from asuka.compare import (
    _labels,
    check_comparable,
    load_reports,
    render_markdown,
)
from asuka.dataset import Dataset, Evidence
from asuka.evaluate import evaluate_retrieval
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter


def _chunk(cid: str, text: str, unit: str, section: str = "overview") -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · {section}",
        attributes={"unit_id": unit, "section": section, "visibility": "public"},
    )


_CHUNKS = (
    _chunk("redis:multi:001", "# MULTI\n\nMarks the start of a transaction. EXEC runs it.", "multi"),
    _chunk("redis:get:000", "# GET\n\nReturns the string value of a key.", "get"),
    _chunk("redis:set:000", "# SET\n\nSets the string value of a key.", "set"),
    _chunk("redis:expire:000", "# EXPIRE\n\nSets a timeout on a key.", "expire"),
)


def _item(task_id: str, question: str, unit: str, out_of_corpus: tuple[str, ...] = ()):
    from asuka.dataset import TaskItem

    return TaskItem(
        task_id=task_id,
        question=question,
        reference_answer="…",
        source_document="redis",
        difficulty="simple",
        evidence=(Evidence(unit, "overview"),),
        out_of_corpus=out_of_corpus,
    )


def _report(
    *,
    top_k: int = 3,
    retriever: str = "bm25",
    with_unfindable: bool = False,
    declare_gap: bool = False,
    with_unfindable_undeclared: bool = False,
):
    """`with_unfindable=True` 时多一道**谁都检不到**的题。

    造法是让问句与语料**词面无重叠**（BM25 的 0 分不返回）——
    而不是把 evidence 指向不存在的片：那样 `ds.resolve()` 会先报错。

    `declare_gap=True` 时给这道题加上语料缺口声明 ——
    用来验"检不到"与"语料本来就不够"这两件事在对照表里**分得开**。
    `with_unfindable_undeclared=True` 再加一道检不到、**没**声明缺口的题。
    """
    items = [
        _item("t1", "MULTI transaction EXEC", "multi"),
        _item("t2", "GET value of a key", "get"),
    ]
    if with_unfindable:
        items.append(
            _item(
                "t9",
                "zzz qqq nonsense gibberish",
                "set",
                ("ZZZ 在语料里完全没有",) if declare_gap else (),
            )
        )
    if with_unfindable_undeclared:
        items.append(_item("t8", "yyy ppp gibberish nonsense", "expire"))
    ds = Dataset(topic="redis", items=tuple(items))
    ds.resolve(_CHUNKS)
    r = BM25Retriever(tuple(_CHUNKS))
    kb = KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
        retriever=r,
        kind=retriever,
    )
    return evaluate_retrieval(kb, ds, top_k=top_k, corpus_chunks=len(_CHUNKS))


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


class TestReportRoundTrip(unittest.TestCase):
    """报告文件必须能被读回，且**与自己的输入自洽**。"""

    def test_round_trip_preserves_numbers(self) -> None:
        rep = _report()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            back = load_reports([p])[0]
        self.assertEqual(back.top_k, rep.top_k)
        self.assertEqual(len(back.items), len(rep.items))
        self.assertAlmostEqual(back.overall.recall, rep.overall.recall, places=4)
        self.assertAlmostEqual(back.overall.mrr, rep.overall.mrr, places=4)

    def test_derived_values_are_recomputed_not_restored(self) -> None:
        """`recall` 是派生量：改了文件里的值，读回来**必须报错**而不是照单全收。"""
        rep = _report()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            # ⚠️ 改成一个**明显不同**的值：改 0.9999 而真值是 1.0 只差 1e-4，
            # 落在舍入容差里，测不出东西（第一版就踩了这个坑）。
            raw["items"][0]["recall"] = 0.0 if raw["items"][0]["recall"] > 0.5 else 1.0
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_reports([p])
        self.assertIn("不自洽", str(ctx.exception))

    def test_tampered_aggregate_is_caught(self) -> None:
        """聚合值虽不能从单题推出来，但可以拿单题**核对**。"""
        rep = _report()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            cur = raw["overall"]["context_recall"]
            raw["overall"]["context_recall"] = 0.0 if cur > 0.5 else 1.0
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_reports([p])
        self.assertIn("聚合值", str(ctx.exception))

    def test_verify_can_be_turned_off_for_archaeology(self) -> None:
        """要读一份旧报告查历史时，得能关掉校验 —— 但那是显式选择。"""
        from asuka.evaluate import RetrievalReport

        rep = _report()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            raw["items"][0]["recall"] = 0.9999
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                len(RetrievalReport.load(p, verify=False).items), 2
            )

    def test_old_format_without_out_of_corpus_is_refused(self) -> None:
        """**缺键 ≠ 空值**。

        旧报告里没有 `out_of_corpus` 这个键，`d.get(..., ())` 会读成空 ——
        而空在读起来就是"这题没有语料缺口"。那不是"少一条提示"，
        是**归因说反了**：`compare` 会把这类题归到"该去查检索"，
        而真相可能是"语料本来就不够"。
        """
        rep = _report()
        with _Tmp() as d:
            p = d / "r.json"
            rep.save(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            for it in raw["items"]:
                del it["out_of_corpus"]
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_reports([p])
        self.assertIn("旧格式", str(ctx.exception))
        self.assertIn("重跑", str(ctx.exception))

    def test_a_comparison_table_is_not_a_report(self) -> None:
        """`compare` 的输出和报告长得很像（都是 JSON、都有 `top_k`），但没有逐题数据。

        按空读的后果不是"少一行"，是两份对照表能互相"对照"出一张**全是 0 的表** ——
        而这张表看起来是结论。
        """
        from asuka.compare import as_dict

        table = as_dict([_report(), _report()])
        with _Tmp() as d:
            p = d / "table.json"
            p.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_reports([p])
        self.assertIn("对照表", str(ctx.exception))


class TestComparability(unittest.TestCase):
    """不可比的理由必须**说清是哪一件**，不能只说"不可比"。"""

    def test_two_identical_runs_are_comparable(self) -> None:
        self.assertEqual(check_comparable([_report(), _report()]), [])

    def test_refuses_different_top_k(self) -> None:
        problems = check_comparable([_report(top_k=3), _report(top_k=5)])
        self.assertTrue(any("top_k" in p for p in problems), problems)

    def test_refuses_non_semantic_smoke(self) -> None:
        """一边是冒烟、一边是真跑 —— 那不是对照，是拿噪声当基线。"""
        smoke = dataclasses.replace(_report(), embedder_semantic=False)
        problems = check_comparable([_report(), smoke])
        self.assertTrue(any("不承载语义" in p for p in problems), problems)

    def test_refuses_different_item_sets(self) -> None:
        a = _report()
        b = dataclasses.replace(a, items=a.items[:1])
        problems = check_comparable([a, b])
        self.assertTrue(any("题目集合" in p for p in problems), problems)

    def test_refuses_different_evidence(self) -> None:
        """同一道题 evidence 不同 = ground truth 变了，那是在比两套标注。"""
        a = _report()
        bad = dataclasses.replace(a.items[0], evidence=("redis:set:000",))
        b = dataclasses.replace(a, items=(bad,) + a.items[1:])
        problems = check_comparable([a, b])
        self.assertTrue(any("evidence 不同" in p for p in problems), problems)

    def test_refuses_fewer_than_two(self) -> None:
        self.assertTrue(check_comparable([_report()]))


class TestRendering(unittest.TestCase):
    def test_refusal_prints_no_numbers(self) -> None:
        """拒绝时**一个对照数字都不能出现** —— 出现了就会被读成结论。"""
        md = render_markdown([_report(top_k=3), _report(top_k=5)])
        self.assertIn("不可比", md)
        self.assertNotIn("context_precision", md)
        self.assertNotIn("| 指标 |", md)

    def test_comparable_table_has_both_retrievers(self) -> None:
        md = render_markdown([_report(), _report()])
        self.assertIn("context_recall", md)
        self.assertIn("recall / 上限", md)

    def test_labels_disambiguate_same_retriever_name(self) -> None:
        """两次 bm25 跑出两列同名，读者分不清哪列是哪次 —— 那对照就白做了。"""
        labels = _labels([_report(), _report()])
        self.assertEqual(len(set(labels)), 2, labels)

    def test_single_retriever_keeps_plain_label(self) -> None:
        self.assertEqual(_labels([_report()]), ["bm25"])

    def test_warns_when_ceiling_is_below_one(self) -> None:
        """上限 < 1 时必须显式告警，否则 recall 会被误读成"检索很差"。"""
        md = render_markdown([_report(top_k=1), _report(top_k=1)])
        self.assertIn("上限", md)

    def test_lists_items_neither_retriever_found(self) -> None:
        """**两边都检不到**的题必须被列出来 —— 那才是"改检索器没用"的那批。

        只写一句"要去查语料"而不给题号，等于承诺了可行动信息却没交付。
        """
        a = _report(with_unfindable=True)
        b = _report(with_unfindable=True)
        self.assertFalse(any(i.hit for i in a.items if i.task_id == "t9"), "t9 应当检不到")
        md = render_markdown([a, b])
        self.assertIn("两边都检不到", md)
        self.assertIn("t9", md)

    def test_corpus_gap_is_separated_from_retrieval_failure(self) -> None:
        """「检不到」与「语料本来就不够」是**两种会叠加但不同源**的问题。

        混在一句话里，读者会拿一种办法去修另一种。
        所以两类题号必须分两行、各自点名。
        """
        a = _report(with_unfindable=True, declare_gap=True, with_unfindable_undeclared=True)
        b = _report(with_unfindable=True, declare_gap=True, with_unfindable_undeclared=True)
        md = render_markdown([a, b])
        self.assertIn("语料覆盖不全", md)
        self.assertIn("没有**语料缺口声明", md)
        tail = md.split("两边都检不到")[1]
        declared_line = next(l for l in tail.splitlines() if "语料覆盖不全" in l)
        undeclared_line = next(l for l in tail.splitlines() if "没有**语料缺口声明" in l)
        self.assertIn("t9", declared_line)
        self.assertNotIn("t8", declared_line)
        self.assertIn("t8", undeclared_line)

    def test_no_gap_split_when_all_unfindable_are_declared(self) -> None:
        """全都声明了缺口时，不该印一行"剩下 0 道" —— 那是噪音。"""
        a = _report(with_unfindable=True, declare_gap=True)
        b = _report(with_unfindable=True, declare_gap=True)
        md = render_markdown([a, b])
        self.assertNotIn("剩下 0 道", md)

    def test_disagreement_split_reaches_json(self) -> None:
        """markdown 里有归因，机器可读的那份也必须有 —— 否则下游少一层信息。"""
        from asuka.compare import as_dict

        a = _report(with_unfindable=True, declare_gap=True, with_unfindable_undeclared=True)
        b = _report(with_unfindable=True, declare_gap=True, with_unfindable_undeclared=True)
        d = as_dict([a, b])["disagreement"]
        self.assertEqual(d["neither"], ["t9", "t8"])
        self.assertEqual(d["neither_with_corpus_gap"], ["t9"])
        self.assertEqual(d["neither_without_corpus_gap"], ["t8"])


if __name__ == "__main__":
    unittest.main()
