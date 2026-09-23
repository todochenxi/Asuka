"""Trace：一条 Run 的**可审计记录**。

--------------------------------------------------------------------------
报告是**视图**，trace 是**发生了什么**

`RetrievalReport` / `AnswerReport` 都是聚合视图：它们回答"这组题整体怎么样"。
但评测报告回答不了最常被问的那个问题：

    这道题答错了 —— 是**没检到该检的**，还是**检到了没用上**？

答案报告里有 `recall`（检索那一层）和要点召回（答案那一层），
但两者是**分别聚合**的。要对着一道具体的题回答，你需要看这一条 Run 的
**逐步过程**：问了什么 → 检到了哪几片 → 答了什么 → 判成答到哪几条、凭什么。

这就是 trace。它同时是**为 LLM 步骤预留的位置**：现在 `generation` 事件里
装的是 `oracle` / `null`，接入真模型时装的是一样的字段，其余全链路不用改。

--------------------------------------------------------------------------
四条纪律（每条都对应一个"读起来像没事"的失效）

**一、事件种类是闭集，未知值拒绝**
    多一种事件类型是**要显式加进来**的。兜底成"其它"会让新事件静默地不被处理，
    而 trace 看起来是完整的。

**二、`seq` 必须从 0 连续**
    缺号说明事件丢了 —— 而"少了几个事件"读起来像"本来就没发生"。
    一条**半截** trace 比没有 trace 更危险：它看起来是一次完整运行。

**三、首尾必须是 `run.started` / `run.finished`**
    没有 `run.finished` 的 trace 可能是**崩在半路**的。读者不能从"事件到此为止"
    推断出"运行正常结束"。

**四、trace 必须带上**输入的同一性**（语料 / 任务集的 sha256 + 检索器 / embedder 身份）**
    否则"这份 trace 是拿什么跑出来的"无法回答 —— 信息在，但在另一层。
    语料换了、题改了、embedder 换了，同样的分数含义完全不同。

--------------------------------------------------------------------------
两套算法**交叉核对**：`verify_against()`

报告里的聚合值由 `answers._group()` 从 `AnswerScore` 对象算出来；
trace 里的聚合值由 `verify_against()` 从**序列化后的 `scoring` 事件**算出来。
两条独立路径算同一个数 —— 对不上说明其中一条坏了。

⚠️ 注意 `pass@k` 的 `k` 取自 `identity.samples_per_task`，
**和报告那条路径一样是"从外面读进来的"**。所以这个交叉核对
不能证明 `k` 对，只能证明"同一份 k 下两条路径一致"。
`k` 与逐题样本条数的对账在 `AnswerReport._verify_aggregates` 里做。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .answers import pass_at_k
from .context import DEFAULT_CONTEXT_BUDGET, DEFAULT_RESERVED_FOR_OUTPUT
from .textutil import CHARS_PER_TOKEN

# ---------------------------------------------------------------- 事件

#: 事件种类**闭集**。加一种要显式写进这里 —— 见模块 docstring 第一条。
EVENT_KINDS: tuple[str, ...] = (
    "run.started",
    "retrieval",
    "generation",
    "scoring",
    "run.finished",
)

#: 每种事件的**必填字段**。缺了就在 `verify()` 里被点名，而不是读成 `None`。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "run.started": (
        "topic", "retriever", "top_k",
        "context_budget", "reserved_for_output", "chars_per_token",
    ),
    # `kept` 与 `context` **两个都要**，不能只留一个：
    # `kept` 过了 C-9/C-10（检到了），`context` 再过 C-3（模型看见了）。
    # 只留 `kept` ⇒ 被预算丢掉的片会被读成"模型见过它"；
    # 只留 `context` ⇒ 说不出"检索其实检到了，是预算丢的"，归因就归错了头。
    "retrieval": (
        "task_id", "query", "kept", "context", "dropped_budget", "context_tokens", "latency_ms",
    ),
    # `citations` **必填**，值可以是 `null`。缺字段和"答案器没自述"读起来是两件事：
    # 前者是旧格式，后者是一个**结论**。用 `.get("citations", ())` 读会把两者混成一个。
    "generation": ("task_id", "answerer", "chars", "citations"),
    "scoring": ("task_id", "points_total", "hits", "missed", "fabricated"),
    "run.finished": ("status", "n_samples", "elapsed_ms"),
}


class TraceError(RuntimeError):
    """trace 不合法。消息里带**全部**问题。"""


@dataclass(frozen=True)
class Event:
    """一条事件。`seq` 由 `Trace` 分配，不许手填。"""

    kind: str
    seq: int
    ts: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise TraceError(
                f"未知事件种类 {self.kind!r} —— 能用的只有 {list(EVENT_KINDS)}。"
                "新种类要显式加进 EVENT_KINDS：兜底成『其它』会让它静默地不被处理，"
                "而 trace 看起来是完整的。"
            )

    @property
    def task_id(self) -> str:
        return str(self.data.get("task_id", ""))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "seq": self.seq, "ts": self.ts, **dict(self.data)}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "Event":
        row = dict(d)
        kind = str(row.pop("kind"))
        seq = int(row.pop("seq"))
        ts = str(row.pop("ts", ""))
        return Event(kind=kind, seq=seq, ts=ts, data=row)


# ---------------------------------------------------------------- 同一性


def hash_file(path: Path) -> str:
    """文件内容的 sha256 前 16 位。读不到就返回 `""`（并在 trace 里显式记空）。

    ⚠️ **不抛异常**：语料缺失不该让整条 Run 跑不起来 ——
    但"没算出来"必须是个**看得见**的值，不是悄悄跳过。
    """
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()[:16]
    except OSError:
        return ""


@dataclass(frozen=True)
class RunIdentity:
    """这条 Run 是**拿什么**跑出来的。

    分数脱离这些字段就没有意义：语料换了、题改了、embedder 换了，
    同样的 0.6594 指的是完全不同的东西。
    """

    topic: str
    retriever: str
    top_k: int
    samples_per_task: int
    corpus_chunks: int
    answerer: str
    #: ⚠️ 这些是"输入的身份"，不是装饰。缺了就记 `""`，**不省略字段** ——
    #: 省略会让"没算"和"没这个字段"分不开。
    corpus_sha256: str = ""
    dataset_sha256: str = ""
    embedder: str = ""
    answerer_is_calibration: bool = False
    started_at: str = ""
    #: ⚠️ 装配参数也是**输入的同一性**，不是装饰。
    #: "预算丢掉 3 片"这个事实脱离"窗口多大"无法解释；两条窗口不同的 trace
    #: 放一起比引用指标就是在比两件事。所以它进 identity，并由 `verify_against`
    #: 与报告的同一个字段对账。
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT
    chars_per_token: int = CHARS_PER_TOKEN

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "retriever": self.retriever,
            "top_k": self.top_k,
            "samples_per_task": self.samples_per_task,
            "corpus_chunks": self.corpus_chunks,
            "answerer": self.answerer,
            "corpus_sha256": self.corpus_sha256,
            "dataset_sha256": self.dataset_sha256,
            "embedder": self.embedder,
            "answerer_is_calibration": self.answerer_is_calibration,
            "started_at": self.started_at,
            "context_budget": self.context_budget,
            "reserved_for_output": self.reserved_for_output,
            "chars_per_token": self.chars_per_token,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "RunIdentity":
        return RunIdentity(
            topic=str(d.get("topic", "")),
            retriever=str(d.get("retriever", "")),
            top_k=int(d.get("top_k", 0)),
            samples_per_task=int(d.get("samples_per_task", 1)),
            corpus_chunks=int(d.get("corpus_chunks", 0)),
            answerer=str(d.get("answerer", "")),
            corpus_sha256=str(d.get("corpus_sha256", "")),
            dataset_sha256=str(d.get("dataset_sha256", "")),
            embedder=str(d.get("embedder", "")),
            answerer_is_calibration=bool(d.get("answerer_is_calibration", False)),
            started_at=str(d.get("started_at", "")),
            context_budget=int(d.get("context_budget", DEFAULT_CONTEXT_BUDGET)),
            reserved_for_output=int(d.get("reserved_for_output", DEFAULT_RESERVED_FOR_OUTPUT)),
            chars_per_token=int(d.get("chars_per_token", CHARS_PER_TOKEN)),
        )


# ---------------------------------------------------------------- Trace


@dataclass
class Trace:
    """一条 Run 的逐步记录。**只追加**（`emit` 分配 `seq`，没有删除接口）。"""

    identity: RunIdentity
    events: list[Event] = field(default_factory=list)
    _t0: float = 0.0

    # -------------------------------------------------- 写

    @classmethod
    def start(cls, identity: RunIdentity) -> "Trace":
        t = cls(identity=identity, _t0=time.perf_counter())
        t.emit("run.started", **identity.as_dict())
        return t

    def emit(self, kind: str, **data: Any) -> Event:
        ev = Event(
            kind=kind,
            seq=len(self.events),
            ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            data=data,
        )
        self.events.append(ev)
        return ev

    def finish(self, *, status: str = "ok") -> Event:
        return self.emit(
            "run.finished",
            status=status,
            n_samples=sum(1 for e in self.events if e.kind == "scoring"),
            elapsed_ms=(time.perf_counter() - self._t0) * 1000.0,
        )

    # -------------------------------------------------- 读

    def as_dict(self) -> dict[str, Any]:
        return {"_meta": self.identity.as_dict(), "events": [e.as_dict() for e in self.events]}

    def save(self, path: Path) -> None:
        """JSONL：第一行是 `_meta`，之后一行一条事件。

        用 JSONL 不用一个大 JSON，是为了**边跑边落**时也能读 ——
        以及 `tail` 就能看最后发生了什么。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"_meta": self.identity.as_dict()}, ensure_ascii=False) + "\n")
            for e in self.events:
                fh.write(json.dumps(e.as_dict(), ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: Path, *, verify: bool = True) -> "Trace":
        meta: Mapping[str, Any] = {}
        events: list[Event] = []
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    meta = row["_meta"]
                    continue
                events.append(Event.from_dict(row))
        if not meta:
            raise TraceError(
                f"{path} 里没有 `_meta` 行 —— 这不是一份 trace（或者被截断了）。"
                "没有同一性信息的 trace 说不出自己是怎么跑出来的。"
            )
        t = cls(identity=RunIdentity.from_dict(meta), events=events)
        if verify:
            t.verify()
        return t

    # -------------------------------------------------- 校验

    def verify(self) -> None:
        """结构校验。**一次报全部**（同项目既有纪律）。"""
        problems: list[str] = []

        if not self.events:
            problems.append("没有任何事件")
        else:
            if self.events[0].kind != "run.started":
                problems.append(f"第一个事件是 {self.events[0].kind!r}，必须是 'run.started'")
            if self.events[-1].kind != "run.finished":
                problems.append(
                    f"最后一个事件是 {self.events[-1].kind!r}，必须是 'run.finished' —— "
                    "没有它，读者无法区分『正常结束』和『崩在半路』"
                )

        # seq 连续：缺号说明事件丢了，而"少了几个事件"读起来像"本来就没发生"
        gaps = [
            f"第 {i} 条事件的 seq={e.seq}"
            for i, e in enumerate(self.events)
            if e.seq != i
        ]
        if gaps:
            problems.append("seq 不连续（事件丢了）：" + "；".join(gaps[:5]))

        for e in self.events:
            missing = [f for f in REQUIRED_FIELDS[e.kind] if f not in e.data]
            if missing:
                problems.append(f"seq={e.seq} 的 {e.kind!r} 缺字段 {missing}")
            if e.kind in ("retrieval", "generation", "scoring") and not e.task_id:
                problems.append(f"seq={e.seq} 的 {e.kind!r} 没有 task_id")

        if problems:
            raise TraceError(
                f"trace 有 {len(problems)} 处问题：\n  " + "\n  ".join(problems)
            )

    def verify_against(self, report: Any) -> None:
        """拿 trace 里的事件**重算**报告里的聚合值，必须一致。

        这是两套独立算法算同一个数：报告那条路径从 `AnswerScore` 对象算，
        这条路径从**序列化后的 JSON** 算。对不上说明其中一条坏了。

        引用这一路尤其值得单独走一遍：报告里的 `fabricated` 是 `score_citations`
        算出来的，而这里是从 `generation.citations` 和 `retrieval.context`
        **两个事件对减**得到的 —— 不碰 `score_citations`。两条路都写同一段判据的话，
        这个核对就只是在证明"我等于我自己"。

        ⚠️ 对减的右边是 `context`（**喂进 prompt 的**）不是 `kept`（**检到的**）。
        被预算丢掉的片模型没看见，引用了它就是编造 —— 用 `kept` 会把编造读成有依据，
        而这个方向**只会让分数变好看**，没有任何东西会报错。
        """
        scoring = [e for e in self.events if e.kind == "scoring"]
        scorable = [e for e in scoring if int(e.data["points_total"]) > 0]
        if not scoring:
            raise TraceError("trace 里没有 scoring 事件，没什么可核对的")

        def passed(e: Event) -> bool:
            return int(e.data["points_total"]) > 0 and len(e.data["hits"]) == int(
                e.data["points_total"]
            )

        recalls = [
            len(e.data["hits"]) / int(e.data["points_total"]) for e in scorable
        ]
        mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
        pass_at_1 = (
            sum(1 for e in scorable if passed(e)) / len(scorable) if scorable else 0.0
        )
        by_task: dict[str, list[bool]] = {}
        for e in scorable:
            by_task.setdefault(e.task_id, []).append(passed(e))
        k = max(1, self.identity.samples_per_task)
        per_task = [pass_at_k(len(v), sum(v), k) for v in by_task.values()]
        pass_at_k_value = sum(per_task) / len(per_task) if per_task else 0.0

        o = report.overall
        drift = [
            f"{name}：报告里 {stored:.4f}，从 trace 重算是 {fresh:.4f}"
            for name, stored, fresh in (
                ("overall.mean_recall", o.mean_recall, mean_recall),
                ("overall.pass_at_1", o.pass_at_1, pass_at_1),
                ("overall.pass_at_k", o.pass_at_k, pass_at_k_value),
            )
            if abs(stored - fresh) > 1e-6
        ]
        if o.n_scorable != len(scorable):
            drift.append(f"overall.n_scorable：报告里 {o.n_scorable}，trace 里 {len(scorable)}")

        # ---- 装配参数：报告的与 identity 的必须是同一份
        # 两处各写一个数 ⇒ "丢了 3 片"和"窗口 8192"可能不是同一次跑出来的，
        # 而**没有任何东西会报错** —— 报告读起来仍然自洽。
        for name, stored, mine in (
            ("context_budget", report.context_budget, self.identity.context_budget),
            ("reserved_for_output", report.reserved_for_output, self.identity.reserved_for_output),
            ("chars_per_token", report.chars_per_token, self.identity.chars_per_token),
        ):
            if stored != mine:
                drift.append(
                    f"{name}：报告里 {stored}，trace identity 里 {mine} —— "
                    "两条命令的装配参数不同，这份 trace 解释不了这份报告。"
                )

        # ---- 引用：从 (citations, context) 对减，**不经过 score_citations**
        given = self.retrieval_context()
        gens = [e for e in self.events if e.kind == "generation"]
        reported = [e for e in gens if e.data.get("citations") is not None]
        fab_from_trace = sum(
            len({str(x) for x in e.data["citations"]} - set(given.get(e.task_id, ())))
            for e in reported
        )
        if o.fabricated_total != fab_from_trace:
            drift.append(
                f"overall.fabricated_total：报告里 {o.fabricated_total}，"
                f"从 trace 的 citations−context 重算是 {fab_from_trace}"
            )
        if o.n_with_citations != len(reported):
            drift.append(
                f"overall.n_with_citations：报告里 {o.n_with_citations}，trace 里 {len(reported)}"
            )
        if o.n_without_citations != len(gens) - len(reported):
            drift.append(
                f"overall.n_without_citations：报告里 {o.n_without_citations}，"
                f"trace 里 {len(gens) - len(reported)}"
            )

        # 逐题逐样本：`scoring.fabricated`（由 `score_citations` 写）必须等于
        # 紧邻它前面的那条 `generation.citations` 减去同题 `kept` 的**现算**结果。
        # 上面比的是总数，总数对得上而逐条对不上是可能的（一题多报、一题少报）。
        #
        # 按**相邻配对**而不是按题聚合：一题多采样时，不同样本的引用可能不同，
        # 取并集会把"第 1 次编造、第 2 次没编造"抹成一个数。
        for i, e in enumerate(self.events):
            if e.kind != "scoring" or i == 0:
                continue
            prev = self.events[i - 1]
            if prev.kind != "generation":
                continue
            if prev.data.get("citations") is None:
                continue
            want = sorted(
                {str(x) for x in prev.data["citations"]} - set(given.get(e.task_id, ()))
            )
            got = sorted(str(x) for x in e.data.get("fabricated", ()))
            if want != got:
                drift.append(
                    f"seq={e.seq} 的 fabricated 对不上：scoring 事件里 {got}，"
                    f"从 citations−context 现算是 {want}"
                )

        if drift:
            raise TraceError(
                "trace 与报告对不上：\n  "
                + "\n  ".join(drift)
                + "\n  ⇒ 两条路径算同一个数，对不上说明其中一条坏了。"
            )

    # -------------------------------------------------- 查询

    def retrieval_kept(self) -> dict[str, tuple[str, ...]]:
        """`task_id` → 检到的 chunk_id（过了 C-9/C-10，**还没过预算**）。

        ⚠️ 这是"检索检到了什么"，不是"模型看见了什么"。要后者用 `retrieval_context()`。
        这个方法存在是为了和**检索报告**对账（`verify_retrieval_against`）——
        检索报告记的正是装配前的那一份。
        """
        out: dict[str, tuple[str, ...]] = {}
        for e in self.events:
            if e.kind == "retrieval":
                out[e.task_id] = tuple(str(x) for x in e.data.get("kept", ()))
        return out

    def retrieval_context(self) -> dict[str, tuple[str, ...]]:
        """`task_id` → **真正喂进 prompt 的** chunk_id（再过一道 C-3 预算）。

        引用核对必须用这个：被预算丢掉的片模型**没看见**，
        引用了它就是编造 —— 而用 `kept` 会把编造读成有依据。
        """
        out: dict[str, tuple[str, ...]] = {}
        for e in self.events:
            if e.kind == "retrieval":
                out[e.task_id] = tuple(str(x) for x in e.data.get("context", ()))
        return out

    def context_dropped(self) -> dict[str, tuple[tuple[str, int, str], ...]]:
        """`task_id` → 被预算丢掉的 `(chunk_id, tokens, 原因)`（C-4 留痕）。"""
        out: dict[str, tuple[tuple[str, int, str], ...]] = {}
        for e in self.events:
            if e.kind == "retrieval":
                out[e.task_id] = tuple(
                    (str(a), int(b), str(c)) for a, b, c in e.data.get("dropped_budget", ())
                )
        return out

    def verify_retrieval_against(self, report: Any) -> None:
        """trace 里的 `kept` 必须和**检索报告**里的 `retrieved` 逐题一致。

        这两个产物是**两条命令**分别产出的。不核对的话，读者会默认它们配套 ——
        而"配套"这件事从来没有人验过。对不上说明它们不是同一次检索，
        把它们放在一起读就是在把两件事当一件事。
        """
        if self.identity.topic != report.topic:
            raise TraceError(
                f"topic 不一致：trace 里 {self.identity.topic!r}，报告里 {report.topic!r}"
            )
        if self.identity.top_k != report.top_k:
            raise TraceError(
                f"top_k 不一致：trace 里 {self.identity.top_k}，报告里 {report.top_k} —— "
                "不同 top_k 的结果本来就不该一样，先对齐再核对。"
            )
        mine = self.retrieval_kept()
        drift = [
            f"{i.task_id}：trace 里 {list(mine.get(i.task_id, ()))[:3]}…，"
            f"报告里 {list(i.retrieved)[:3]}…"
            for i in report.items
            if mine.get(i.task_id, ()) != tuple(i.retrieved)
        ]
        if drift:
            raise TraceError(
                "trace 与检索报告对不上（不是同一次检索）：\n  "
                + "\n  ".join(drift[:5])
            )

    def for_task(self, task_id: str) -> list[Event]:
        return [e for e in self.events if e.task_id == task_id]

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out


# ---------------------------------------------------------------- 审计视图


def explain(trace: Trace, task_id: str, *, question: str = "") -> str:
    """把这题**发生过什么**摊开 —— 这是 trace 存在的主要理由。

    它要能回答："答错了，是没检到该检的，还是检到了没用上？"
    所以检索、生成、判分三段都要出现，且**判分要说清凭什么**。
    """
    events = trace.for_task(task_id)
    if not events:
        raise TraceError(
            f"trace 里没有 {task_id} 的事件 —— 要么题号写错了，要么这题没被跑到。"
            f"trace 里有：{sorted({e.task_id for e in trace.events if e.task_id})[:5]} …"
        )

    lines: list[str] = [f"# {task_id}", ""]
    if question:
        lines += [f"> {question}", ""]
    lines.append(f"- 语料 `{trace.identity.topic}` · sha256 `{trace.identity.corpus_sha256 or '未记录'}`")
    lines.append(f"- 任务集 sha256 `{trace.identity.dataset_sha256 or '未记录'}`")
    lines.append(
        f"- 检索 `{trace.identity.retriever}`（top_k={trace.identity.top_k}）"
        f" · answerer `{trace.identity.answerer}`"
        + ("（**校准**）" if trace.identity.answerer_is_calibration else "")
    )
    lines.append("")

    for ev in events:
        d = dict(ev.data)
        if ev.kind == "retrieval":
            kept = d.get("kept", [])
            ctx = d.get("context", [])
            dropped = d.get("dropped_budget", [])
            ctx_set = {str(x) for x in ctx}
            lines.append("## 检索")
            lines.append("")
            lines.append(
                f"- 问句：`{d.get('query', '')}`"
            )
            lines.append(
                f"- 检到 {len(kept)} 片（拒绝 {len(d.get('denied', []))} · "
                f"无 citation 丢弃 {len(d.get('dropped_no_citation', []))}）"
                f"，耗时 {float(d.get('latency_ms', 0.0)):.1f} ms"
            )
            for i, cid in enumerate(kept, 1):
                mark = "" if str(cid) in ctx_set else "  ← **被预算丢掉，模型没看见**"
                lines.append(f"  {i}. `{cid}`{mark}")
            lines.append(
                f"- **装配后喂进 prompt** {len(ctx)} 片 / {d.get('context_tokens', 0)} tokens"
                + (f"；预算丢掉 {len(dropped)} 片" if dropped else "")
            )
            if dropped:
                lines.append("")
                lines.append(
                    "  ⚠️ 被预算丢掉的片模型**没看见**：引用了它就是编造；"
                    "而「该引的依据在里面」要记在**预算**头上，不是检索头上。"
                )
                for cid, tokens, why in dropped:
                    lines.append(f"  - `{cid}`（{tokens} tokens）：{why}")
            lines.append("")
        elif ev.kind == "generation":
            lines.append("## 生成")
            lines.append("")
            if d.get("error"):
                lines.append(f"- ⚠️ **报错**：{d['error']}")
            lines.append(
                f"- `{d.get('answerer')}` · {d.get('chars')} 字符 · "
                f"{float(d.get('latency_ms', 0.0)):.1f} ms · "
                f"token {d.get('prompt_tokens', 0)}+{d.get('completion_tokens', 0)} · "
                f"${float(d.get('cost_usd', 0.0)):.4f}"
            )
            cites = d.get("citations")
            if cites is None:
                # 缺字段由 `verify()` 挡掉；走到这里 `None` 只可能是**答案器没自述**。
                lines.append(
                    "- ⚠️ **没有自述引用** —— 引用指标对这条样本**不可测**"
                    "（不是『没引用』）"
                )
            else:
                lines.append(f"- 自述引用 {len(cites)} 条：{list(cites) if cites else '（无）'}")
            lines.append("")
            lines.append("```")
            lines.append(str(d.get("text", "")).strip())
            lines.append("```")
            lines.append("")
        elif ev.kind == "scoring":
            total = int(d.get("points_total", 0))
            hits = d.get("hits", [])
            missed = d.get("missed", [])
            lines.append("## 判分")
            lines.append("")
            if total == 0:
                lines.append("- ⚠️ 这题**没有声明必答要点** ⇒ 不可测（既不算 0 也不算 1）")
            else:
                lines.append(f"- 要点 {len(hits)}/{total}")
                for label, by in d.get("hit_by", []):
                    lines.append(f"  - ✅ {label} —— 凭『{by}』判为答到")
                for label in missed:
                    lines.append(f"  - ❌ 缺：{label}")
            if d.get("out_of_corpus"):
                lines.append("")
                lines.append("- ⚠️ 这题**声明了语料缺口** —— 有些要点语料里根本没有，"
                             "低分不该全记在生成头上：")
                for gap in d["out_of_corpus"]:
                    lines.append(f"  - {gap}")
            lines.append("")
            lines.append("### 引用")
            lines.append("")
            fab = d.get("fabricated", [])
            if fab:
                lines.append(
                    f"- ⚠️ **编造**：引用了这次检索**没给它**的来源 {list(fab)} "
                    "—— 这不是『答得不够好』，是引用来源不存在于它的上下文里。"
                )
            else:
                lines.append("- 没有编造引用")
            if d.get("evidence_not_retrieved"):
                lines.append(
                    f"- 该引但**检索根本没检到**（记检索头上）："
                    f"{list(d['evidence_not_retrieved'])}"
                )
            if d.get("evidence_dropped"):
                lines.append(
                    f"- 该引、**检到了但装不进预算**（记预算头上，不是检索）："
                    f"{list(d['evidence_dropped'])}"
                )
            if d.get("evidence_ignored"):
                lines.append(
                    f"- 该引、**给了它却没引**（记生成头上）：{list(d['evidence_ignored'])}"
                )
            lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def build_parser() -> Any:
    import argparse

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Asuka Trace：跑一条可审计的 Run（检索 → 生成 → 判分）"
    )
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument("--answerer", default="oracle", choices=["oracle", "null", "fabricator"])
    parser.add_argument("--retriever", default="bm25", choices=["bm25", "dense"])
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--embedder", default="auto", choices=["auto", "api", "local", "hashing"])
    parser.add_argument("--model-path", default="")
    parser.add_argument("--allow-non-semantic", action="store_true")
    parser.add_argument(
        "--explain",
        default="",
        help="跑完顺便摊开某一题的过程（题号，如 r-hard-05）",
    )
    parser.add_argument("--corpus-dir", default=str(root / "corpus"))
    parser.add_argument("--datasets-dir", default=str(root / "datasets"))
    parser.add_argument("--out", default=str(root / "runs"))
    # ⚠️ 装配参数**必须能从命令行改**：窗口大小是"这次拿什么模型跑"的属性，
    # 不是常量。硬编码在代码里的话，"预算丢掉几片"这个结论换台机器就不成立。
    parser.add_argument(
        "--context-budget",
        type=int,
        default=DEFAULT_CONTEXT_BUDGET,
        help="喂进 prompt 的上下文窗口上限（token）",
    )
    parser.add_argument(
        "--reserved-for-output",
        type=int,
        default=DEFAULT_RESERVED_FOR_OUTPUT,
        help="为输出预留的 token，不计入上下文预算",
    )
    parser.add_argument(
        "--chars-per-token",
        type=int,
        default=CHARS_PER_TOKEN,
        help="估算 token 用的『每 token 几个字符』（英文技术文档约 4）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    from .dataset import DatasetError
    from .embedding import EmbeddingError
    from .vectorstore import VectorStoreError

    try:
        return _run(args)
    except (EmbeddingError, VectorStoreError, DatasetError, TraceError, ValueError) as exc:
        print(f"\n! {exc}", file=sys.stderr)
        return 2


def _run(args: Any) -> int:
    from .answers import (
        FabricatingAnswerer,
        NullAnswerer,
        OracleAnswerer,
        evaluate_answers,
        render_markdown,
    )
    from .corpus import read_chunks
    from .dataset import dataset_path, load_dataset
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

    answerer = {
        "oracle": OracleAnswerer,
        "null": NullAnswerer,
        "fabricator": FabricatingAnswerer,
    }[args.answerer]()
    emb = getattr(getattr(kb, "retriever", None), "embedder", None)

    identity = RunIdentity(
        topic=args.topic,
        retriever=args.retriever,
        top_k=args.top_k,
        samples_per_task=args.samples,
        corpus_chunks=len(chunks),
        answerer=answerer.name,
        corpus_sha256=hash_file(chunks_path),
        dataset_sha256=hash_file(dataset_path(Path(args.datasets_dir), args.topic)),
        embedder=emb.info.signature if emb is not None else "",
        answerer_is_calibration=answerer.is_calibration,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        context_budget=args.context_budget,
        reserved_for_output=args.reserved_for_output,
        chars_per_token=args.chars_per_token,
    )

    trace = Trace.start(identity)
    report = evaluate_answers(
        kb,
        ds,
        answerer,
        top_k=args.top_k,
        samples_per_task=args.samples,
        corpus_chunks=len(chunks),
        trace=trace,
        context_budget=args.context_budget,
        reserved_for_output=args.reserved_for_output,
        chars_per_token=args.chars_per_token,
    )
    trace.finish(status="ok")

    # 两条独立路径算同一个数 —— 对不上就不落盘。
    trace.verify_against(report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    base = f"{args.topic}-{args.answerer}-{args.retriever}-k{args.samples}-{stamp}"
    trace_path = out_dir / "traces" / f"{base}.jsonl"
    md_path = out_dir / "traces" / f"{base}.md"
    trace.save(trace_path)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    kinds = trace.kinds()
    print(
        f"[{args.topic}/{args.answerer}] trace {len(trace.events)} 条事件 "
        f"{kinds}"
    )
    print(f"  语料 sha256={identity.corpus_sha256}  任务集 sha256={identity.dataset_sha256}")
    print(f"  报告聚合 = trace 重算 ✓（{report.overall.n_scorable} 条可测样本）")
    print(f"  → {trace_path}")

    if args.explain:
        q = next((i.question for i in ds.items if i.task_id == args.explain), "")
        text = explain(trace, args.explain, question=q)
        # 审计视图也是**产物**，落盘 —— 只在终端里闪一下的话，没人能拿它去对质。
        ex_path = out_dir / "traces" / f"{base}-explain-{args.explain}.md"
        ex_path.write_text(text, encoding="utf-8")
        print()
        print(text)
        print(f"  → {ex_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
