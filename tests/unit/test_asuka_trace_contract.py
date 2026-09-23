"""Trace 的契约。

--------------------------------------------------------------------------
为什么 trace 不是"顺手多存一份日志"

评测报告回答不了最常被问的那个问题：**这道题答错了，是没检到该检的，
还是检到了没用上？** 报告里的 `recall` 和要点召回是**分别聚合**的，
要对着一道具体的题回答，必须看这一条 Run 的**逐步过程**。

所以这份测试守的是"trace 真的能当审计用"，不是"它跑通了"：

  · 事件种类是**闭集**，未知值拒绝（兜底成"其它"会让新事件静默不被处理）
  · `seq` 连续、首尾齐全 —— **半截 trace 比没有 trace 更危险**，它看起来是完整运行
  · trace 必须带上**输入的同一性**（语料/任务集 sha256）—— 否则说不出自己怎么跑出来的
  · `verify_against` 用**另一条路径**重算报告的聚合值，必须一致
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packages.agent_context.retrieval import Chunk, RetrievalPipeline

from asuka.answers import (
    Answer,
    AnswerReport,
    NullAnswerer,
    OracleAnswerer,
    evaluate_answers,
)
from asuka.dataset import Dataset, Evidence, RequiredPoint, TaskItem
from asuka.kb import BM25Retriever, KnowledgeBase, PublicCorpusFilter
from asuka.trace import (
    EVENT_KINDS,
    REQUIRED_FIELDS,
    Event,
    RunIdentity,
    Trace,
    TraceError,
    explain,
    hash_file,
)


def _chunk(cid: str, text: str, unit: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        document_id=f"redis:{unit}",
        text=text,
        citation=f"Redis · {unit.upper()} · overview",
        attributes={"unit_id": unit, "section": "overview", "visibility": "public"},
    )


_CHUNKS = (
    _chunk("redis:expire:000", "# EXPIRE\n\nSets a timeout on a key.", "expire"),
    _chunk("redis:ttl:000", "# TTL\n\nReturns the remaining time to live.", "ttl"),
)


def _item(task_id: str = "t-1", **over) -> TaskItem:
    base = dict(
        task_id=task_id,
        question="What does EXPIRE do?",
        reference_answer="EXPIRE sets a timeout on a key, so the key is deleted.",
        source_document="redis:expire",
        difficulty="simple",
        evidence=(Evidence("expire", "overview"),),
        required_points=(
            RequiredPoint("设超时", ("sets a timeout",)),
            RequiredPoint("到期删除", ("deleted",)),
        ),
    )
    base.update(over)
    return TaskItem(**base)  # type: ignore[arg-type]


def _kb() -> KnowledgeBase:
    r = BM25Retriever(tuple(_CHUNKS))
    return KnowledgeBase(
        topic="redis",
        pipeline=RetrievalPipeline(retriever=r, permission=PublicCorpusFilter()),
        retriever=r,
        kind="bm25",
    )


def _dataset(*items: TaskItem) -> Dataset:
    ds = Dataset(topic="redis", items=items or (_item(),))
    ds.resolve(_CHUNKS)
    return ds


def _identity(**over) -> RunIdentity:
    base = dict(
        topic="redis",
        retriever="bm25",
        top_k=3,
        samples_per_task=1,
        corpus_chunks=2,
        answerer="oracle",
        corpus_sha256="deadbeefdeadbeef",
        dataset_sha256="cafebabecafebabe",
        answerer_is_calibration=True,
    )
    base.update(over)
    return RunIdentity(**base)  # type: ignore[arg-type]


def _run(*items: TaskItem, answerer=None, trace=None, samples: int = 1):
    """跑一次，返回 `(trace, report)`。"""
    t = trace or Trace.start(_identity(samples_per_task=samples))
    rep = evaluate_answers(
        _kb(),
        _dataset(*items),
        answerer or OracleAnswerer(),
        top_k=3,
        samples_per_task=samples,
        trace=t,
    )
    t.finish()
    return t, rep


class _Tmp:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *a):
        self._d.cleanup()


# ---------------------------------------------------------------- 闭集


class TestEventKindsAreClosed(unittest.TestCase):
    def test_unknown_kind_is_refused_and_lists_the_valid_ones(self) -> None:
        """兜底成"其它"会让新事件静默地不被处理，而 trace 看起来是完整的。"""
        with self.assertRaises(TraceError) as ctx:
            Event(kind="whatever", seq=0, ts="", data={})
        msg = str(ctx.exception)
        self.assertIn("whatever", msg)
        self.assertIn("run.started", msg)

    def test_every_kind_declares_its_required_fields(self) -> None:
        """声明与能力之间的差必须由系统说出来。

        新加一种事件却忘了在 `REQUIRED_FIELDS` 里声明字段 ⇒ 它永远不会被字段校验 ——
        而"没校验"读起来和"校验通过"一模一样。
        """
        self.assertEqual(set(EVENT_KINDS), set(REQUIRED_FIELDS))

    def test_seq_is_assigned_by_the_trace_not_by_the_caller(self) -> None:
        t, _ = _run()
        self.assertEqual([e.seq for e in t.events], list(range(len(t.events))))


# ---------------------------------------------------------------- 结构


class TestTraceStructure(unittest.TestCase):
    def test_first_and_last_events_are_pinned(self) -> None:
        t, _ = _run()
        self.assertEqual(t.events[0].kind, "run.started")
        self.assertEqual(t.events[-1].kind, "run.finished")
        self.assertEqual(t.events[-1].data["status"], "ok")

    def test_a_gap_in_seq_is_caught(self) -> None:
        """缺号说明事件丢了 —— 而"少了几个事件"读起来像"本来就没发生"。"""
        t, _ = _run()
        del t.events[3]
        with self.assertRaises(TraceError) as ctx:
            t.verify()
        self.assertIn("seq 不连续", str(ctx.exception))

    def test_missing_run_finished_is_caught(self) -> None:
        """半截 trace 比没有 trace 更危险 —— 它看起来是一次完整运行。"""
        t, _ = _run()
        t.events.pop()
        with self.assertRaises(TraceError) as ctx:
            t.verify()
        self.assertIn("崩在半路", str(ctx.exception))

    def test_missing_required_field_is_named(self) -> None:
        t, _ = _run()
        t.events.append(Event(kind="retrieval", seq=len(t.events), ts="", data={"task_id": "t-1"}))
        with self.assertRaises(TraceError) as ctx:
            t.verify()
        self.assertIn("缺字段", str(ctx.exception))

    def test_reports_all_problems_at_once(self) -> None:
        """同一条纪律：拒绝"整体做不到"时查整份，不查"下一个"。

        这里一条坏事件同时踩了**四类**检查（首尾事件 / seq 连续 / 必填字段 / task_id）。
        修一处跑一次的话要跑四轮，而每轮都像"又发现一个新问题"。
        """
        t = Trace(identity=_identity())
        t.events.append(Event(kind="retrieval", seq=9, ts="", data={}))
        with self.assertRaises(TraceError) as ctx:
            t.verify()
        msg = str(ctx.exception)
        self.assertIn("5 处问题", msg)
        for category in ("必须是 'run.started'", "必须是 'run.finished'", "seq 不连续", "缺字段"):
            with self.subTest(category=category):
                self.assertIn(category, msg)

    def test_an_empty_trace_is_refused(self) -> None:
        with self.assertRaises(TraceError) as ctx:
            Trace(identity=_identity()).verify()
        self.assertIn("没有任何事件", str(ctx.exception))


# ---------------------------------------------------------------- 读写


class TestTraceIO(unittest.TestCase):
    def test_round_trip_preserves_identity_and_events(self) -> None:
        t, _ = _run()
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            back = Trace.load(p)
        self.assertEqual(back.identity, t.identity)
        self.assertEqual([e.as_dict() for e in back.events], [e.as_dict() for e in t.events])

    def test_it_is_jsonl_so_tail_works(self) -> None:
        """一行一条事件是为了能 `tail` 看最后发生了什么。"""
        t, _ = _run()
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), len(t.events) + 1)   # +1 是 _meta
        self.assertIn("_meta", json.loads(lines[0]))

    def test_a_file_without_meta_is_refused(self) -> None:
        """没有同一性信息的 trace 说不出自己是怎么跑出来的。"""
        t, _ = _run()
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            keep = p.read_text(encoding="utf-8").splitlines()[1:]
            p.write_text("\n".join(keep) + "\n", encoding="utf-8")
            with self.assertRaises(TraceError) as ctx:
                Trace.load(p)
        self.assertIn("_meta", str(ctx.exception))

    def test_verify_can_be_turned_off_for_archaeology(self) -> None:
        """要读一条崩在半路的 trace 查原因时，得能关掉结构校验 —— 但那是显式选择。"""
        t, _ = _run()
        t.events.pop()
        with _Tmp() as d:
            p = d / "t.jsonl"
            t.save(p)
            self.assertEqual(len(Trace.load(p, verify=False).events), len(t.events))

    def test_hash_file_is_empty_for_a_missing_file_not_an_exception(self) -> None:
        """语料缺失不该让整条 Run 跑不起来 —— 但"没算出来"必须是个**看得见**的值。"""
        self.assertEqual(hash_file(Path("no/such/file.jsonl")), "")

    def test_hash_file_is_content_addressed(self) -> None:
        with _Tmp() as d:
            a, b = d / "a.txt", d / "b.txt"
            a.write_text("hello", encoding="utf-8")
            b.write_text("hello", encoding="utf-8")
            self.assertEqual(hash_file(a), hash_file(b))
            b.write_text("hello!", encoding="utf-8")
            self.assertNotEqual(hash_file(a), hash_file(b))


# ---------------------------------------------------------------- 交叉核对


class TestCrossCheck(unittest.TestCase):
    def test_agrees_with_the_report_on_a_real_run(self) -> None:
        """两条独立路径算同一个数：报告从 `AnswerScore` 对象算，
        这条从**序列化后的事件**算。"""
        t, rep = _run(samples=3)
        t.verify_against(rep)   # 不抛就算过
        self.assertGreater(rep.overall.n_scorable, 0)

    def test_catches_a_tampered_scoring_event(self) -> None:
        t, rep = _run()
        for e in t.events:
            if e.kind == "scoring" and e.data["points_total"]:
                e.data["hits"] = []       # 假装一个要点都没答到
                break
        with self.assertRaises(TraceError) as ctx:
            t.verify_against(rep)
        self.assertIn("对不上", str(ctx.exception))

    def test_refuses_when_there_is_nothing_to_cross_check(self) -> None:
        t = Trace.start(_identity())
        t.finish()
        with self.assertRaises(TraceError) as ctx:
            t.verify_against(_run()[1])
        self.assertIn("没有 scoring 事件", str(ctx.exception))

    def test_a_zero_recall_run_still_cross_checks(self) -> None:
        """`null` 的下界也要能被核对 —— 全是 0 时最容易"看起来对"。"""
        t, rep = _run(answerer=NullAnswerer())
        t.verify_against(rep)
        self.assertEqual(rep.overall.mean_recall, 0.0)


class TestRetrievalCrossCheck(unittest.TestCase):
    """trace 与**检索报告**是两个产物、两条命令 —— "它们配套吗"必须被验过。"""

    def _retrieval_report(self, top_k: int = 3):
        from asuka.evaluate import evaluate_retrieval

        return evaluate_retrieval(_kb(), _dataset(), top_k=top_k, corpus_chunks=2)

    def test_agrees_with_a_matching_retrieval_report(self) -> None:
        t, _ = _run()
        t.verify_retrieval_against(self._retrieval_report(top_k=3))   # 不抛就算过

    def test_refuses_a_different_top_k(self) -> None:
        """不同 top_k 的结果本来就不该一样 —— 先对齐再核对，别比出一个假的差。"""
        t, _ = _run()
        with self.assertRaises(TraceError) as ctx:
            t.verify_retrieval_against(self._retrieval_report(top_k=1))
        self.assertIn("top_k 不一致", str(ctx.exception))

    def test_catches_a_drifted_retrieval(self) -> None:
        """对不上说明它们不是同一次检索 —— 把它们放一起读就是在把两件事当一件事。"""
        t, _ = _run()
        for e in t.events:
            if e.kind == "retrieval":
                e.data["kept"] = ["redis:ttl:000"]
                break
        with self.assertRaises(TraceError) as ctx:
            t.verify_retrieval_against(self._retrieval_report(top_k=3))
        self.assertIn("不是同一次检索", str(ctx.exception))


# ---------------------------------------------------------------- 发射


class TestPipelineEmits(unittest.TestCase):
    def test_one_retrieval_and_one_pair_per_sample(self) -> None:
        """检索每题只跑一次（上下文是同一批），生成/判分每采样一次。

        检索跑两次会让"检索检到了但答案没答对"这个归因不成立 ——
        两次的上下文不是同一批。
        """
        t, _ = _run(samples=3)
        k = t.kinds()
        self.assertEqual(k["retrieval"], 1)
        self.assertEqual(k["generation"], 3)
        self.assertEqual(k["scoring"], 3)

    def test_retrieval_records_ids_not_text(self) -> None:
        """只记 chunk_id，**不复制正文** —— 正文在语料里，复制会产生第二个真相源。"""
        t, _ = _run()
        ev = next(e for e in t.events if e.kind == "retrieval")
        self.assertEqual(ev.data["kept"], ["redis:expire:000"])
        self.assertNotIn("text", ev.data)

    def test_generation_records_the_answer_text(self) -> None:
        """答案全文**要存** —— 审计最常问的就是"它到底答了什么"。"""
        t, _ = _run()
        ev = next(e for e in t.events if e.kind == "generation")
        self.assertIn("sets a timeout", ev.data["text"])
        self.assertEqual(ev.data["chars"], len(ev.data["text"]))

    def test_no_trace_means_no_side_effects(self) -> None:
        """`trace` 是可选参数 —— 不传时行为必须与之前**完全一致**。"""
        rep = evaluate_answers(_kb(), _dataset(), OracleAnswerer(), top_k=3)
        self.assertAlmostEqual(rep.overall.mean_recall, 1.0, places=6)


# ---------------------------------------------------------------- 审计视图


class TestExplain(unittest.TestCase):
    def test_shows_retrieval_generation_and_scoring(self) -> None:
        """三段缺一不可 —— 少了任何一段就回答不了"错在哪一步"。"""
        t, _ = _run()
        md = explain(t, "t-1", question="What does EXPIRE do?")
        self.assertIn("## 检索", md)
        self.assertIn("redis:expire:000", md)
        self.assertIn("## 生成", md)
        self.assertIn("## 判分", md)

    def test_says_why_a_point_was_judged_hit(self) -> None:
        """分数要可复核：能回答"这题为什么算过了"，不只是给一个数。"""
        t, _ = _run()
        md = explain(t, "t-1")
        self.assertIn("凭『sets a timeout』判为答到", md)

    def test_lists_the_missing_points(self) -> None:
        """只说"这题错了"没法去改 —— 必须说清缺哪个要点。"""
        t, _ = _run(answerer=NullAnswerer())
        md = explain(t, "t-1")
        self.assertIn("❌ 缺：设超时", md)

    def test_warns_about_the_corpus_gap(self) -> None:
        item = _item(out_of_corpus=("ZRANK 在语料里完全没有",))
        t, _ = _run(item)
        md = explain(t, "t-1")
        self.assertIn("声明了语料缺口", md)
        self.assertIn("ZRANK 在语料里完全没有", md)

    def test_says_unscorable_when_no_points_declared(self) -> None:
        t, _ = _run(_item(required_points=()))
        md = explain(t, "t-1")
        self.assertIn("不可测", md)

    def test_unknown_task_names_what_is_available(self) -> None:
        """报错要**指出可选项** —— 只说"不对"会让人去翻文件。"""
        t, _ = _run()
        with self.assertRaises(TraceError) as ctx:
            explain(t, "t-999")
        self.assertIn("t-999", str(ctx.exception))
        self.assertIn("t-1", str(ctx.exception))

    def test_identity_is_printed_so_the_trace_can_be_located(self) -> None:
        """语料 / 任务集的 sha256 必须印出来 —— 否则这份 trace 说不出自己怎么跑出来的。"""
        t, _ = _run()
        md = explain(t, "t-1")
        self.assertIn("deadbeefdeadbeef", md)
        self.assertIn("cafebabecafebabe", md)
        self.assertIn("**校准**", md)


# ---------------------------------------------------------------- CLI


class TestTraceCli(unittest.TestCase):
    def test_defaults(self) -> None:
        from asuka.trace import build_parser

        a = build_parser().parse_args([])
        self.assertEqual(a.answerer, "oracle")
        self.assertEqual(a.retriever, "bm25")
        self.assertEqual(a.top_k, 10)
        self.assertEqual(a.samples, 1)
        self.assertEqual(a.explain, "")

    def test_allow_non_semantic_defaults_to_false(self) -> None:
        from asuka.trace import build_parser

        self.assertFalse(build_parser().parse_args([]).allow_non_semantic)


if __name__ == "__main__":
    unittest.main()
