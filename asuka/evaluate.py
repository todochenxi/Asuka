"""Evaluation：检索指标（**不需要 LLM**）+ 答案指标（需要 LLM，可注入）。

--------------------------------------------------------------------------
先分清哪些指标**根本不需要模型**

用户要的六个指标里，有一半是**测量**，不是**判断**：

    Retrieval    context_recall / context_precision / MRR / unit hit   ← 纯计算
    Citation     citation 指向的位置对不对                              ← 纯计算
    Latency      计时                                                   ← 纯测量
    Token        数 token                                               ← 纯测量
    Cost         用量 × 单价                                            ← 纯计算
    Correctness  答案对不对                                             ← **需要判断**

前五个现在就能跑，而且**必须现在就跑** —— 因为它们是后两个的**分母**：

    "答案对了 60%" 这个数字，在"检索根本没检到"的情况下毫无信息量。
    只有先知道 context_recall，才能回答"是检索的锅还是生成的锅"。

--------------------------------------------------------------------------
两个反直觉但必须的指标

**`context_precision` 不能只看"有没有命中"**
    检索返回 10 条、其中 1 条相关 —— `hit@10` 是 1.0，看起来完美。
    但那 9 条噪声会直接进 prompt，既烧 token 又干扰生成。
    所以 precision 与 recall 必须成对看，单独的任何一个都能被刷。

**`pass@k` 不能省**（社区结论）
    一个不稳定的 Agent 可能**碰巧**做对一次就在 pass@1 上表现优异。
    k 次里只要有 1 次对就算过 —— 它衡量的是"能不能做到"，
    而 pass@1 衡量的是"能不能稳定做到"。两个都要，差值本身就是信息。

--------------------------------------------------------------------------
为什么 `Correctness` 用**规则式必答要点召回**，不用 LLM-as-Judge 做主判据

社区实测 LLM-as-Judge 有"**偏爱长输出**"的偏见 —— 写得长的答案更容易被判对。
把它当主判据，等于把"啰嗦"变成了得分项。

所以主判据是规则式的：每道题的 `reference_answer` 里声明**必答要点**，
判"答到了几个"。它**确定性、可复现、零成本**，而且**不会因为答案写得长就给高分**。
LLM-as-Judge 留作**辅助信号**（`judge` 可注入），不是主判据。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from packages.agent_context.retrieval import Chunk

from .dataset import Dataset, DatasetError, TaskItem
from .embedding import EmbeddingError
from .kb import KnowledgeBase
from .vectorstore import VectorStoreError

# ---------------------------------------------------------------- 单题得分


@dataclass(frozen=True)
class ItemScore:
    task_id: str
    difficulty: str
    question: str
    #: ground truth（evidence 解析出来的 chunk_id）
    evidence: tuple[str, ...]
    retrieved: tuple[str, ...]
    #: 命中的 evidence（按 retrieved 顺序）
    hits: tuple[str, ...]
    #: 第一个命中出现在第几位（1-based）；没命中 = 0
    first_hit_rank: int
    #: 检到的 chunk 里，有多少来自 evidence 声明的**单元**（比 chunk 粒度宽）
    unit_precision: float
    latency_ms: float = 0.0
    citations: tuple[str, ...] = ()
    #: 从 `TaskItem` 带过来：参考答案里**语料支撑不了**的部分。
    #: 非空 ⇒ 这题的**答案级**低分不该记在检索器头上。检索指标不受影响
    #: （evidence 那几个 chunk 确实存在），所以它在这里只作**注记**。
    out_of_corpus: tuple[str, ...] = ()

    @property
    def recall(self) -> float:
        return len(self.hits) / len(self.evidence) if self.evidence else 0.0

    @property
    def precision(self) -> float:
        return len(self.hits) / len(self.retrieved) if self.retrieved else 0.0

    @property
    def hit(self) -> bool:
        return bool(self.hits)

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_hit_rank if self.first_hit_rank else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "difficulty": self.difficulty,
            "question": self.question,
            "evidence": list(self.evidence),
            "retrieved": list(self.retrieved),
            "hits": list(self.hits),
            "first_hit_rank": self.first_hit_rank,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "unit_precision": round(self.unit_precision, 4),
            "latency_ms": round(self.latency_ms, 2),
            "citations": list(self.citations),
            "out_of_corpus": list(self.out_of_corpus),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, verify: bool = True) -> "ItemScore":
        """`recall` / `precision` / `hit` / `reciprocal_rank` 都是**属性**，不读文件。

        它们由 `evidence` / `retrieved` / `hits` 现算。文件里存的那份只用来**核对**
        —— 对不上说明报告被改过或来自旧定义，见 `RetrievalReport.load`。
        """
        obj = cls(
            task_id=str(d["task_id"]),
            difficulty=str(d.get("difficulty", "")),
            question=str(d.get("question", "")),
            evidence=tuple(d.get("evidence", ())),
            retrieved=tuple(d.get("retrieved", ())),
            hits=tuple(d.get("hits", ())),
            first_hit_rank=int(d.get("first_hit_rank", 0)),
            unit_precision=float(d.get("unit_precision", 0.0)),
            latency_ms=float(d.get("latency_ms", 0.0)),
            citations=tuple(d.get("citations", ())),
            out_of_corpus=tuple(d.get("out_of_corpus", ())),
        )
        # ⚠️ `out_of_corpus` 是**后加的字段**。旧报告里**没有这个键**，
        # `d.get(..., ())` 会读成空 —— 而空在读起来就是"这题没有语料缺口"。
        #
        # 那不是"少了一条提示"，是**归因说反了**：`compare.py` 会把这类题
        # 归到"没有语料缺口声明 ⇒ 该去查检索"，而真相可能是"语料本来就不够"。
        # 所以**缺键就拒绝**，而不是按空读。重跑一次的成本远低于一次错误归因。
        if "out_of_corpus" not in d:
            raise ValueError(
                f"报告里 {d.get('task_id', '?')} 缺 `out_of_corpus` 字段 —— "
                "这是**旧格式**报告（写于加这个字段之前）。"
                "按空读会让下游把『没记录』读成『语料没缺口』，所以直接拒绝：请重跑。"
            )
        if verify:
            drift = [
                f"{name}：文件里 {d[name]}，按输入重算是 {got:.4f}"
                for name, got in (("recall", obj.recall), ("precision", obj.precision))
                if name in d and abs(float(d[name]) - got) > 1e-3
            ]
            if drift:
                raise ValueError(
                    f"报告与自己的输入不自洽（{obj.task_id}）：\n  "
                    + "\n  ".join(drift)
                    + "\n  ⇒ 这份报告要么被改过，要么是旧版指标定义写的，不能拿来比。"
                )
        return obj


# ---------------------------------------------------------------- 聚合


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@dataclass
class GroupScore:
    n: int = 0
    recall: float = 0.0
    precision: float = 0.0
    hit_rate: float = 0.0
    mrr: float = 0.0
    latency_ms: float = 0.0
    #: ⚠️ recall 的**理论上限**。见 `RetrievalReport` 的 docstring。
    recall_ceiling: float = 1.0
    #: evidence 条数 > top_k 的题目数 —— 这些题**不可能** recall=1.0
    items_over_topk: int = 0
    #: 参考答案里有**语料支撑不了**的部分的题目数 —— 这些题的**答案级**分数
    #: 有天花板，且天花板不是检索器造成的。与 `items_over_topk` 同族：
    #: 一个数字的**分母是谁划的**，必须能被读出来。
    items_out_of_corpus: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "context_recall": round(self.recall, 4),
            "context_recall_ceiling": round(self.recall_ceiling, 4),
            "context_precision": round(self.precision, 4),
            "hit_rate": round(self.hit_rate, 4),
            "mrr": round(self.mrr, 4),
            "latency_ms_mean": round(self.latency_ms, 2),
            "items_evidence_over_topk": self.items_over_topk,
            "items_out_of_corpus": self.items_out_of_corpus,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GroupScore":
        """聚合值**只能**读文件 —— 它是"对一组题求均值"的结果，单题数据推不回来。

        （单题能推、聚合不能推，这个区别是刻意的：见 `ItemScore.from_dict`。）
        """
        return cls(
            n=int(d.get("n", 0)),
            recall=float(d.get("context_recall", 0.0)),
            precision=float(d.get("context_precision", 0.0)),
            hit_rate=float(d.get("hit_rate", 0.0)),
            mrr=float(d.get("mrr", 0.0)),
            latency_ms=float(d.get("latency_ms_mean", 0.0)),
            recall_ceiling=float(d.get("context_recall_ceiling", 1.0)),
            items_over_topk=int(d.get("items_evidence_over_topk", 0)),
            items_out_of_corpus=int(d.get("items_out_of_corpus", 0)),
        )


def _group(scores: Sequence[ItemScore], *, top_k: int = 0) -> GroupScore:
    if not scores:
        return GroupScore()
    ceiling = 1.0
    over = 0
    if top_k:
        # 一条题的 recall 上限 = min(|evidence|, top_k) / |evidence|
        caps = [min(len(s.evidence), top_k) / len(s.evidence) for s in scores if s.evidence]
        ceiling = _mean(caps) if caps else 1.0
        over = sum(1 for s in scores if len(s.evidence) > top_k)
    return GroupScore(
        n=len(scores),
        recall=_mean([s.recall for s in scores]),
        precision=_mean([s.precision for s in scores]),
        hit_rate=_mean([1.0 if s.hit else 0.0 for s in scores]),
        mrr=_mean([s.reciprocal_rank for s in scores]),
        latency_ms=_mean([s.latency_ms for s in scores]),
        recall_ceiling=ceiling,
        items_over_topk=over,
        items_out_of_corpus=sum(1 for s in scores if s.out_of_corpus),
    )


@dataclass
class RetrievalReport:
    """检索评测结果。

    ⚠️ **`context_recall` 有理论上限，必须一起看。**

        recall 的上限 = mean( min(|evidence|, top_k) / |evidence| )

    一道题声明了 7 处 evidence 而 `top_k=5`，它的 recall 上限就是 5/7 = 0.71 ——
    **无论检索多好都到不了 1.0**。不把上限说出来，读者会把"上限 0.71、
    实际 0.44"误读成"检索很差"，而真相可能是"检索已接近满分"。

    这就是为什么要一起报 `context_recall_ceiling` 与 `items_evidence_over_topk`：
    一个数字的**分母是谁划的**，必须能被读出来。
    """

    topic: str
    retriever: str
    top_k: int
    items: tuple[ItemScore, ...] = ()
    overall: GroupScore = field(default_factory=GroupScore)
    by_difficulty: dict[str, GroupScore] = field(default_factory=dict)
    corpus_chunks: int = 0
    embedder: str = ""
    #: 该次检索用的 embedder **是否承载语义**。
    #: 词法检索（bm25）不涉及 embedder ⇒ 恒为 True（它没有"语义"这个问题）。
    #: `False` 意味着这是一次**冒烟**，它的分数不能进任何对照表。
    #: ⚠️ 这个字段是后加的：**旧报告里没有**，`from_dict` 按 `True` 读。
    #: 所以"它到底是不是冒烟"这件事，只有**重跑过**的报告才说得准。
    embedder_semantic: bool = True
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "retriever": self.retriever,
            "top_k": self.top_k,
            "corpus_chunks": self.corpus_chunks,
            "embedder": self.embedder,
            "embedder_semantic": self.embedder_semantic,
            "generated_at": self.generated_at,
            "overall": self.overall.as_dict(),
            "by_difficulty": {k: v.as_dict() for k, v in self.by_difficulty.items()},
            "items": [i.as_dict() for i in self.items],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path, *, verify: bool = True) -> "RetrievalReport":
        """从 JSON 读回。`verify=True` 时校验派生量与输入自洽。

        ⚠️ `recall` / `precision` / `mrr` 都是**派生量** —— 它们由
        `evidence` / `retrieved` / `hits` 现算，**不从文件里读**。
        但会**核对**文件里存的值与重算值是否一致：

        不一致说明这份报告要么被人改过，要么是**旧版本指标定义**下写的。
        两种情况都不该被静默接受 —— 拿旧定义的分数和新定义的并排比，
        比出来的差值全是定义的差，不是检索的差。
        """
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(d, verify=verify)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, verify: bool = True) -> "RetrievalReport":
        # ⚠️ 先分清"**这不是一份检索报告**"和"这是一份空跑的报告"。
        # `compare` 的输出和报告长得很像（都是 JSON、都有 top_k），但它**没有逐题数据**。
        # 按空读的后果不是"少一行"，是两份对照表能互相"对照"出一张**全是 0 的表** ——
        # 而这张表看起来是结论。这正是本模块存在的理由，所以这里直接拒绝。
        if "items" not in d or "retriever" not in d:
            raise ValueError(
                "这不是一份检索报告（缺 `items` / `retriever` 键）。"
                "如果你传的是 `asuka.compare` 的输出 —— 那是**对照表**，"
                "不是报告，不能拿来做输入。"
            )
        items = tuple(
            ItemScore.from_dict(i, verify=verify) for i in d.get("items", ())
        )
        top_k = int(d.get("top_k", 0))
        obj = cls(
            topic=str(d.get("topic", "")),
            retriever=str(d.get("retriever", "")),
            top_k=top_k,
            items=items,
            overall=GroupScore.from_dict(d.get("overall", {})),
            by_difficulty={
                k: GroupScore.from_dict(v)
                for k, v in (d.get("by_difficulty") or {}).items()
            },
            corpus_chunks=int(d.get("corpus_chunks", 0)),
            embedder=str(d.get("embedder", "")),
            embedder_semantic=bool(d.get("embedder_semantic", True)),
            generated_at=str(d.get("generated_at", "")),
        )
        if verify:
            obj._verify_aggregates()
        return obj

    def _verify_aggregates(self) -> None:
        """聚合值虽然不能从单题**推**出来，但可以拿单题**核对**。

        对不上说明这份报告是拼出来的（或指标定义变过）。
        """
        if not self.items:
            return
        recomputed = _group(self.items, top_k=self.top_k)
        drift = [
            f"{name}：文件里 {stored:.4f}，按单题重算是 {fresh:.4f}"
            for name, stored, fresh in (
                ("overall.context_recall", self.overall.recall, recomputed.recall),
                ("overall.context_precision", self.overall.precision, recomputed.precision),
                ("overall.hit_rate", self.overall.hit_rate, recomputed.hit_rate),
                ("overall.mrr", self.overall.mrr, recomputed.mrr),
                ("overall.context_recall_ceiling", self.overall.recall_ceiling,
                 recomputed.recall_ceiling),
            )
            if abs(stored - fresh) > 1e-3
        ]
        if self.overall.items_out_of_corpus != recomputed.items_out_of_corpus:
            drift.append(
                f"overall.items_out_of_corpus：文件里 {self.overall.items_out_of_corpus}，"
                f"按单题重算是 {recomputed.items_out_of_corpus}"
            )
        if drift:
            raise ValueError(
                "报告的聚合值与自己的单题数据对不上：\n  "
                + "\n  ".join(drift)
                + "\n  ⇒ 这份报告不可信，别拿它做对照。"
            )


# ---------------------------------------------------------------- 评测


def evaluate_retrieval(
    kb: KnowledgeBase,
    dataset: Dataset,
    *,
    top_k: int = 5,
    corpus_chunks: int = 0,
) -> RetrievalReport:
    """跑一遍检索，算出 recall / precision / MRR / hit_rate。

    ⚠️ `dataset.resolve()` 必须先跑过 —— 没有 ground truth 就没有分母。
    """
    if not dataset.resolved:
        raise ValueError("数据集还没 resolve（evidence 未解析成 chunk_id）")

    # chunk_id → unit_id，用来算"命中的 chunk 是否来自正确的单元"
    unit_of: dict[str, str] = {}
    for task_id, ids in dataset.resolved.items():
        for cid in ids:
            unit_of.setdefault(cid, cid.split(":")[1] if cid.count(":") >= 2 else "")

    scores: list[ItemScore] = []
    for item in dataset.items:
        evidence = dataset.resolved.get(item.task_id, ())
        ev_set = set(evidence)
        ev_units = {e.unit_id for e in item.evidence}

        t0 = time.perf_counter()
        result = kb.search(item.question, limit=top_k)
        latency = (time.perf_counter() - t0) * 1000.0

        retrieved = tuple(c.chunk_id for c in result.kept)
        hits = tuple(cid for cid in retrieved if cid in ev_set)
        first = next((i + 1 for i, cid in enumerate(retrieved) if cid in ev_set), 0)
        from_ev_unit = sum(
            1 for c in result.kept if str(c.attributes.get("unit_id", "")) in ev_units
        )

        scores.append(
            ItemScore(
                task_id=item.task_id,
                difficulty=item.difficulty,
                question=item.question,
                evidence=evidence,
                retrieved=retrieved,
                hits=hits,
                first_hit_rank=first,
                unit_precision=from_ev_unit / len(retrieved) if retrieved else 0.0,
                latency_ms=latency,
                citations=tuple(c.citation for c in result.kept),
                out_of_corpus=tuple(getattr(item, "out_of_corpus", ())),
            )
        )

    by_diff: dict[str, GroupScore] = {}
    for level in ("simple", "medium", "hard"):
        subset = [s for s in scores if s.difficulty == level]
        if subset:
            by_diff[level] = _group(subset, top_k=top_k)

    # 词法检索（bm25）没有 embedder ⇒ "是否承载语义"这个问题对它不成立，记 True。
    emb = getattr(getattr(kb, "retriever", None), "embedder", None)
    return RetrievalReport(
        topic=dataset.topic,
        retriever=kb.kind,
        top_k=top_k,
        items=tuple(scores),
        overall=_group(scores, top_k=top_k),
        by_difficulty=by_diff,
        corpus_chunks=corpus_chunks,
        embedder=emb.info.signature if emb is not None else "",
        embedder_semantic=emb.info.semantic if emb is not None else True,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


# ---------------------------------------------------------------- 报告


def render_markdown(report: RetrievalReport) -> str:
    """人读的评测报告。**先把"检索这一层"说清楚**，再谈答案。"""
    o = report.overall
    lines: list[str] = []
    lines.append(f"# Asuka 检索评测 · {report.topic}")
    lines.append("")
    lines.append(
        f"- 检索器：`{report.retriever}`"
        + (f"（embedder `{report.embedder}`）" if report.embedder else "")
    )
    lines.append(f"- top_k：{report.top_k}")
    lines.append(f"- 语料：{report.corpus_chunks} chunks")
    lines.append(f"- 题目：{o.n} 条")
    lines.append(f"- 生成时间：{report.generated_at}")
    lines.append("")
    lines.append("## 总体")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| context_recall | {o.recall:.4f} |")
    lines.append(f"| **context_recall 上限** | **{o.recall_ceiling:.4f}** |")
    lines.append(f"| context_precision | {o.precision:.4f} |")
    lines.append(f"| hit_rate@{report.top_k} | {o.hit_rate:.4f} |")
    lines.append(f"| MRR | {o.mrr:.4f} |")
    lines.append(f"| 平均检索耗时 | {o.latency_ms:.1f} ms |")
    lines.append("")
    if o.items_over_topk:
        lines.append(
            f"> ⚠️ 有 **{o.items_over_topk}** 条题声明的 evidence 条数超过 `top_k={report.top_k}`，"
            f"这些题的 recall **不可能**到 1.0 —— 上限是 `min(|evidence|, top_k) / |evidence|`。"
            f"所以 `recall={o.recall:.4f}` 要对着上限 `{o.recall_ceiling:.4f}` 读，"
            f"而不是对着 1.0 读。"
        )
        lines.append("")
    ooc = [i for i in report.items if i.out_of_corpus]
    if ooc:
        lines.append(
            f"> ⚠️ 有 **{len(ooc)}** 条题的参考答案要求**语料里没有**的事实"
            f"（`TaskItem.out_of_corpus` 里人工声明）。这些题的**答案级**分数有天花板，"
            f"而天花板**不是检索器造成的** —— 别把它们的低分读成「检索差」。"
            f"检索指标不受影响（evidence 那几个 chunk 确实存在）。"
        )
        lines.append("")
    lines.append("## 按难度")
    lines.append("")
    lines.append("| 难度 | 题数 | recall | 上限 | precision | hit_rate | MRR | 语料不全 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for level, g in report.by_difficulty.items():
        lines.append(
            f"| {level} | {g.n} | {g.recall:.4f} | {g.recall_ceiling:.4f} | "
            f"{g.precision:.4f} | {g.hit_rate:.4f} | {g.mrr:.4f} | "
            f"{g.items_out_of_corpus or '—'} |"
        )
    lines.append("")

    misses = [i for i in report.items if not i.hit]
    if misses:
        lines.append(f"## 完全没检到的题（{len(misses)}）")
        lines.append("")
        for i in misses:
            lines.append(f"- `{i.task_id}` ({i.difficulty}) {i.question}")
            lines.append(f"  - 该检到：{', '.join(i.evidence)}")
            lines.append(f"  - 实际检到：{', '.join(i.retrieved) or '(空)'}")
        lines.append("")

    partial = [i for i in report.items if i.hit and i.recall < 1.0]
    if partial:
        lines.append(f"## 只检到一部分的题（{len(partial)}）")
        lines.append("")
        for i in partial:
            lines.append(
                f"- `{i.task_id}` recall={i.recall:.2f} "
                f"命中 {len(i.hits)}/{len(i.evidence)}"
            )
        lines.append("")

    if ooc:
        lines.append(f"## 语料覆盖不全的题（{len(ooc)}）")
        lines.append("")
        for i in ooc:
            lines.append(f"- `{i.task_id}` ({i.difficulty})")
            for gap in i.out_of_corpus:
                lines.append(f"  - {gap}")
        lines.append("")
        lines.append(
            "> 这一节是**归属表**，不是待办清单：它说的是「即使检索完美，"
            "参考答案也答不全」。要真修，得先决定是**扩语料**还是**改题** —— "
            "那是语料范围的决定，不该由评测脚本替你下。"
        )
        lines.append("")

    lines.append("## 逐题明细")
    lines.append("")
    lines.append("| task | 难度 | recall | precision | 首个命中位 | 耗时ms | 语料 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i in report.items:
        lines.append(
            f"| `{i.task_id}` | {i.difficulty} | {i.recall:.2f} | {i.precision:.2f} | "
            f"{i.first_hit_rank or '—'} | {i.latency_ms:.0f} | "
            f"{'⚠️ 不全' if i.out_of_corpus else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def build_parser() -> Any:
    """CLI 的**声明**。抽成独立函数是为了让"默认值"能被测试断言。

    ⚠️ 特别是 `--allow-non-semantic`：它必须是 `store_true`（默认 **False**）。
    如果哪天有人把它写成默认 True，`test_asuka_evaluate_contract` 会红 ——
    因为那等于**默认允许**用不承载语义的索引去评检索质量，
    分数会变得好看且毫无意义。
    """
    import argparse

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Asuka 检索评测（不需要 LLM）")
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument("--retriever", default="bm25", choices=["bm25", "dense"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--embedder",
        default="auto",
        choices=["auto", "api", "local", "hashing"],
        help="dense 检索用的 embedder；`auto` 需要 ASUKA_EMBED_API_KEY，没有就报错",
    )
    parser.add_argument(
        "--model-path",
        default="",
        help="本地模型权重目录（不填则读 ASUKA_EMBED_MODEL_PATH）",
    )
    parser.add_argument(
        "--allow-non-semantic",
        action="store_true",
        help="⚠️ 只给冒烟用：放行由不承载语义的 embedder 建成的索引。"
        "正常评测不要开 —— 开了分数就不可信了",
    )
    parser.add_argument("--corpus-dir", default=str(root / "corpus"))
    parser.add_argument("--datasets-dir", default=str(root / "datasets"))
    parser.add_argument("--out", default=str(root / "runs"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    # 拒绝要**读起来像拒绝**，不是一段栈回溯。
    # `VectorStoreError`（embedder 对不上）/ `EmbeddingError`（权重缺了）/
    # `DatasetError`（题目的 evidence 解析不出来）都是**用户可修**的输入问题，
    # 不是程序缺陷 —— 所以印一句话 + 退出码 2，不吐 traceback。
    try:
        return _run(args)
    except (EmbeddingError, VectorStoreError, DatasetError) as exc:
        print(f"\n! {exc}", file=sys.stderr)
        return 2


def _run(args: Any) -> int:
    from .corpus import read_chunks
    from .dataset import load_dataset
    from .kb import build_knowledge_base

    corpus_dir = Path(args.corpus_dir)
    chunks_path = corpus_dir / args.topic / "chunks.jsonl"
    if not chunks_path.exists():
        print(f"! 没有语料：{chunks_path}")
        return 2
    chunks = read_chunks(chunks_path)

    ds = load_dataset(Path(args.datasets_dir), args.topic)
    ds.resolve(chunks)

    store = None
    embedder = None
    if args.retriever == "dense":
        from .embedding import build_embedder
        from .vectorstore import QdrantStore

        # 显式造 embedder，而不是让 `QdrantRetriever` 自己 `build_embedder()`。
        # 理由：`auto` 在没有 API key 时会报错，而"本地权重"这条路必须能被选中。
        # 并且这里**不静默** —— `auto` 走到 local 是一次实现替换，得由调用方说出口。
        embedder = build_embedder(args.embedder, model_path=args.model_path)
        store = QdrantStore()
    kb = build_knowledge_base(
        args.topic,
        corpus_dir=corpus_dir,
        kind=args.retriever,
        store=store,
        embedder=embedder,
        allow_non_semantic=args.allow_non_semantic,
    )

    report = evaluate_retrieval(
        kb, ds, top_k=args.top_k, corpus_chunks=len(chunks)
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    (out_dir / "retrieval").mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "retrieval" / f"{args.topic}-{args.retriever}-{stamp}.json"
    md_path = out_dir / "retrieval" / f"{args.topic}-{args.retriever}-{stamp}.md"
    report.save(json_path)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    o = report.overall
    print(
        f"[{args.topic}/{args.retriever}] top_k={args.top_k} n={o.n}  "
        f"recall={o.recall:.4f}  precision={o.precision:.4f}  "
        f"hit_rate={o.hit_rate:.4f}  MRR={o.mrr:.4f}"
    )
    for level, g in report.by_difficulty.items():
        print(
            f"  {level:8s} n={g.n:2d}  recall={g.recall:.4f}  "
            f"precision={g.precision:.4f}  hit={g.hit_rate:.4f}"
        )
    print(f"  → {md_path}")
    print(f"  → {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
