"""答案级指标：**规则式必答要点召回** + `pass@k` + Latency / Token / Cost 测量位。

--------------------------------------------------------------------------
为什么主判据不用 LLM-as-Judge

社区实测 LLM-as-Judge 有"**偏爱长输出**"的偏见 —— 写得长的答案更容易被判对。
把它当主判据，等于把"啰嗦"变成了得分项。

所以主判据是**规则式**的：每道题在 `TaskItem.required_points` 里声明**必答要点**，
判"答到了几条"。它确定性、可复现、零成本、**不会因为答案长就给高分**。

⚠️ 它仍有已知的、**故意的**缺口：把整篇文档抄进答案，要点召回会很高。
所以报告里同时给出 `chars_per_hit_point`（每答到一个要点花了多少字符）——
**一个数字混两种含义，读者无法归因**，所以分开报，不塞进同一个分数。

LLM-as-Judge 留作**辅助信号**（`judge` 可注入），不是主判据。

--------------------------------------------------------------------------
`pass@k` 与 `pass@1` 必须成对看

一个不稳定的 Agent 可能**碰巧**做对一次就在 pass@1 上表现优异。
`pass@k` 衡量"能不能做到"，`pass@1` 衡量"能不能稳定做到"，
**差值本身就是信息**（差大 = 不稳定）。

通过的定义是**全部要点都答到**（`recall == 1.0`）。
不用"recall ≥ 阈值"是因为阈值是个自由旋钮，而旋钮会被调到来凑结论。
梯度信息由 `mean_recall` 提供 —— 两个数各司其职。

--------------------------------------------------------------------------
`OracleAnswerer` / `NullAnswerer`：这是**校准**，不是成绩

没有 LLM API key 时，答案级指标仍然要能被验证 —— 否则它就是一段没人跑过的代码。
两个假答案器给判据划出上下界：

    oracle  直接返回参考答案   ⇒ 要点召回**必须**是 1.0。不是 1.0 说明**声明写错了**。
    null    返回空串           ⇒ 要点召回**必须**是 0.0。不是 0 说明**判据在送分**。

`oracle` 不过是一条**很强**的自检：它证明"每条要点的说法确实能在参考答案里找到"
（`Dataset.validate` 也查这个，但那是校验期；这里是判据期，用的是同一条匹配规则）。
它**不能**证明"要点覆盖了参考答案的全部含义"—— 那需要裁判模型，见 M12。

所以报告会把 `calibration=True` 印在最上面：**别把校准分数读成模型成绩。**

--------------------------------------------------------------------------
引用（Citation）：答案**自述**用了哪些来源，然后被**核对**

要点召回只回答"答到了几条"，不回答"它是**凭什么**答的"。
一个靠参数记忆答对的模型，和一个真读了语料答对的模型，在要点召回上**一模一样**。
对企业知识库来说这两者完全不同 —— 前者意味着**知识库根本没起作用**。

所以 `Answer.citations` 是答案器必须自述的一项：它用了哪几个 `chunk_id`。
`None`（缺省）表示**没自述**，和 `()`（明确说"没引用任何来源"）是两件事：
前者**不可测**，后者可测且召回为 0。这是 `points_total == 0` 那条纪律的同一形状。

⚠️ 自述**不是**信任 —— 它立刻被拿去和**三个逐级收窄**的集合核对：

    retrieved   检索管线留下的（过了 C-9 权限 + C-10 citation）
    available   **真正喂进 prompt 的**（再过一道 C-3 token 预算）⊆ retrieved
    evidence    这题声明该引的依据（`dataset.resolved`）

于是有四条互不替代的结论：

    fabricated = cited − available     引了**没给它**的东西 ⇒ **编造**。硬判据，点名。
    grounded   = 1 − |fabricated| / |cited|          引用有没有依据（< 1 就是有编的）
    evidence_recall = |cited ∩ evidence| / |evidence| 该引的依据引到没有（端到端）

⚠️ `evidence_recall` **单独看会误判**：它同时受检索、预算、生成三件事影响。
所以缺的依据被拆成**三段**，各自归因：

    evidence_not_retrieved = evidence − retrieved              **检索根本没检到** ⇒ 换检索器
    evidence_dropped       = (evidence ∩ retrieved) − available **检到了但装不下** ⇒ 加预算/降 top_k
    evidence_ignored       = (evidence ∩ available) − cited     **给了它却没引** ⇒ 改 prompt

⚠️ 前两段**必须分开**。合成一句"检索没检到"的话，人会去改一个没坏的东西 ——
而"检到了但被预算丢掉"是这套评测里**最容易被误读**的一种失败：
报告上的分数低，看起来像检索差，实际是配置（窗口 / top_k）不合适。

`evidence_used_rate = |evidence_cited| / |evidence_cited ⊎ evidence_ignored|` 是
**纯生成侧**的数，检索漏没漏、预算丢没丢都和它无关。归因就靠这三个数分开。

为什么**不**报 `citation_precision`（引的东西里有多少条属于 ground truth）：
ground truth 是**最少必要依据**，不是**唯一允许引的依据**。引了别的真实上下文
不算错，用 precision 罚它等于**奖励"少引"** —— 那会把一个好行为变成扣分项。

为什么**不**再分"编造的 id 是语料里根本没有，还是语料里有但没检到"：
前者是凭空编，后者说明它在**用记忆**，诊断价值不同。但要分就得把整份语料的 id
传进来，而"传不进来时静默降级"会让这一栏读起来像"没有这类问题"。
**一个会静默降级的判据，比没有更危险** —— 所以只报"没给它"这一个硬结论。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from packages.agent_context.items import ContextItem
from packages.agent_context.retrieval import Chunk

from .context import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_RESERVED_FOR_OUTPUT,
    assemble,
)
from .dataset import Dataset, TaskItem
from .kb import KnowledgeBase
from .textutil import CHARS_PER_TOKEN

# ---------------------------------------------------------------- 答案


@dataclass(frozen=True)
class Answer:
    """一次生成的产物 **+ 它的代价**。

    代价字段和文本放在一起，是为了让"这个答案值不值"能被回答 ——
    只报正确率、不报成本，等于默认成本为零。
    """

    text: str
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    #: 生成失败时写这里。**不许**用空文本冒充"模型答了但答错"——
    #: 那是两件事：一个要修管线，一个要改 prompt。
    error: str = ""
    #: 答案器**自述**用了哪几个 `chunk_id`。
    #:
    #: `None` = **没自述**（不可测）／`()` = 明确说"没引用任何来源"（可测，召回 0）。
    #: 这两者混起来，"没测"会被读成"答得没依据"，而报告里没有任何东西会提醒你。
    citations: tuple[str, ...] | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    # ⚠️ 这里**故意没有** `as_dict()` / `from_dict()`。
    # 曾经有，而且是**零调用者** —— 报告存的是 `AnswerScore`，trace 存的是显式字段，
    # 没有任何一条路径序列化 `Answer` 本身。一对没人读的序列化方法不是 API，
    # 是**会悄悄漂移的注释**：它写错了没有任何东西会红。
    # （变红验证抓到过：把它改坏，21 条变异里唯一没红的就是它。）


class Answerer(Protocol):
    """生成答案的东西。**真模型和假答案器共用这个接口**。

    `is_calibration` 是**必须自述**的（不是可选属性）：
    报告读者要能一眼分清"这是模型成绩"还是"这是判据校准"。
    一个不说的 answerer 会被 `evaluate_answers` **拒绝**，
    而不是被当成"真模型" —— 沉默的差读起来像没有差。

    同样必须自述的是 `Answer.citations`（用了哪几个 chunk_id）。
    没自述（`None`）不会让运行失败，但这一题**不进引用指标的分母**，
    并在报告里被点名 —— 引用指标最怕的就是"没测"读成"没依据"。

    ⚠️ `contexts` 是**装配之后**的 `ContextItem`（过了 C-1/C-3/C-4），
    **不是** `RetrievalResult.kept`。两者会不一样：预算装不下的片被丢掉了，
    模型**没看见**它们。所以：

        i.key        chunk_id
        i.text       正文
        i.reference  citation（C-10）—— 模型该引的就是它
        i.attributes["score"]  检索得分

    给 answerer 传 `kept` 而给引用判据传装配后的 id 的话，
    两者会对不上，而**报告里没有任何东西会提醒你**。
    """

    name: str
    is_calibration: bool

    def answer(self, item: TaskItem, contexts: Sequence[ContextItem]) -> Answer: ...


@dataclass
class OracleAnswerer:
    """**校准用**：直接返回参考答案，并引用**它拿到的全部上下文**。

    要点召回必须 1.0（参考答案必然命中自己的要点）。

    引用这一路要看清它**引什么**：引 `contexts` 而不是引 ground truth。
    引 ground truth 会把"检索漏了没有"这个变量混进来，
    于是 `grounded_rate` 会随检索质量浮动 —— 而 oracle 是**判据**的上界，
    不该受被测系统影响。引 `contexts` 保证两件事：

        grounded_rate 恒 1.0（它只引它拿到的）
        evidence_ignored 恒空（给它的依据它一条不漏地引了）⇒ evidence_used_rate 恒 1.0

    后者才是它作为**生成侧上界**的意义。检索漏掉的依据落在
    `evidence_not_retrieved`（根本没检到）或 `evidence_dropped`（检到了但装不进预算）
    —— 那是检索和**预算**的责任，报告分开说。
    """

    name: str = "oracle"
    is_calibration: bool = True

    def answer(self, item: TaskItem, contexts: Sequence[ContextItem]) -> Answer:
        t0 = time.perf_counter()
        text = item.reference_answer
        return Answer(
            text=text,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            citations=tuple(i.key for i in contexts),
        )


@dataclass
class NullAnswerer:
    """**校准用**：返回空串，并**明确自述**"没引用任何来源"。

    `citations=()` 而不是 `None`：它确实"答了"（一个空答案），
    只是没引用。写成 `None` 会让它整题退出引用分母，
    下界就从"引用召回 0"退化成"引用不可测" —— 校准会**静默失效**。
    """

    name: str = "null"
    is_calibration: bool = True

    def answer(self, item: TaskItem, contexts: Sequence[ContextItem]) -> Answer:
        return Answer(text="", citations=())


@dataclass
class FabricatingAnswerer:
    """**校准用**：答得像模像样，但引用是**编的**。

    它存在的唯一理由是**证明编造探测器会响**：`oracle`（全对）和 `null`（全空）
    都碰不到"引了没给它的东西"这条路径，所以光有那两个，
    `fabricated` 那一栏可能是**一段永远为空的代码**，而报告看起来一切正常。

    它引用的是语料里不存在的 id —— `grounded_rate` 必须 0.0，
    `fabricated` 必须非空，且必须在报告里被点名。
    """

    name: str = "fabricator"
    is_calibration: bool = True
    ghost: tuple[str, ...] = ("ghost-chunk-0001", "ghost-chunk-0002")

    def answer(self, item: TaskItem, contexts: Sequence[ContextItem]) -> Answer:
        return Answer(text=item.reference_answer, citations=self.ghost)


# ---------------------------------------------------------------- 判据


@dataclass(frozen=True)
class CitationVerdict:
    """一次生成的**引用核对结果**。判据本身在 `score_citations` 里。

    ⚠️ `cited is None` 时下面**所有**"引了什么"的字段都是空的 ——
    它们空是因为**没测**，不是因为"没有"。读之前先看 `cited`。
    """

    #: 答案器自述的引用。`None` = **没自述**（不是"没引用"）。
    cited: tuple[str, ...] | None = None
    #: 引了但**没给它**的 ⇒ 编造。这是硬判据，报告必须点名。
    fabricated: tuple[str, ...] = ()
    #: 引了、给了、而且**确实属于该题 ground truth** 的依据。
    evidence_cited: tuple[str, ...] = ()
    #: 该引但**检索根本没检到**的依据 —— 归因给**检索器**。
    evidence_not_retrieved: tuple[str, ...] = ()
    #: 该引、**检索检到了、但被 token 预算丢掉了** —— 归因给**预算/配置**。
    #:
    #: ⚠️ 和上面那个**必须分开**：一个是"换检索器"，一个是"加预算或降 top_k"。
    #: 混成一句"检索没检到"的话，人会去改一个没坏的东西。
    evidence_dropped: tuple[str, ...] = ()
    #: 该引、**给了它却没引**的依据 —— 归因给生成。`cited is None` 时无意义。
    evidence_ignored: tuple[str, ...] = ()

    @property
    def reported(self) -> bool:
        """这一题有没有自述引用。**没自述 ⇒ 引用指标全部不可测。**"""
        return self.cited is not None

    @property
    def evidence_total(self) -> int:
        """该题声明了几条依据。四段互斥，加起来就是全部。"""
        return (
            len(self.evidence_cited)
            + len(self.evidence_not_retrieved)
            + len(self.evidence_dropped)
            + len(self.evidence_ignored)
        )

    @property
    def grounded_rate(self) -> float | None:
        """引用的东西里有多少是**它确实拿到过**的。`None` = 不可测。

        低于 1.0 ⇒ 有编造。**这是判据不是分数** —— 一个 0.98 和一个 0.0
        在"有没有编造"这件事上是同一类，区别只在于编了几条。
        """
        if self.cited is None or not self.cited:
            return None
        return (len(self.cited) - len(self.fabricated)) / len(self.cited)

    @property
    def evidence_recall(self) -> float | None:
        """端到端：该引的依据引到没有。⚠️ **同时受检索 / 预算 / 生成影响**。

        想归因就配 `evidence_not_retrieved` / `evidence_dropped` /
        `evidence_ignored` 三行一起读。
        """
        if self.cited is None:
            return None
        total = (
            len(self.evidence_cited)
            + len(self.evidence_ignored)
            + len(self.evidence_not_retrieved)
            + len(self.evidence_dropped)
        )
        if total <= 0:
            return None
        return len(self.evidence_cited) / total

    @property
    def evidence_used_rate(self) -> float | None:
        """**纯生成侧**：给了它的依据，它引了几成。检索漏没漏与它无关。

        分母是"给了它、而且该引"的那些 —— 检索没给的**不进分母**，
        否则检索越差这个数越高，方向就反了。
        """
        if self.cited is None:
            return None
        denom = len(self.evidence_cited) + len(self.evidence_ignored)
        if denom <= 0:
            return None
        return len(self.evidence_cited) / denom

    def as_dict(self) -> dict[str, Any]:
        return {
            "cited": None if self.cited is None else list(self.cited),
            "fabricated": list(self.fabricated),
            "evidence_cited": list(self.evidence_cited),
            "evidence_not_retrieved": list(self.evidence_not_retrieved),
            "evidence_dropped": list(self.evidence_dropped),
            "evidence_ignored": list(self.evidence_ignored),
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "CitationVerdict":
        raw = d.get("cited")

        def tup(key: str) -> tuple[str, ...]:
            return tuple(str(x) for x in d.get(key, ()))

        return CitationVerdict(
            cited=None if raw is None else tuple(str(x) for x in raw),
            fabricated=tup("fabricated"),
            evidence_cited=tup("evidence_cited"),
            evidence_not_retrieved=tup("evidence_not_retrieved"),
            evidence_dropped=tup("evidence_dropped"),
            evidence_ignored=tup("evidence_ignored"),
        )


def score_citations(
    cited: Sequence[str] | None,
    *,
    available: Sequence[str],
    retrieved: Sequence[str],
    evidence: Sequence[str],
) -> CitationVerdict:
    """把答案器自述的引用和 `retrieved` / `available` / `evidence` 对账。

    三个集合是**逐级收窄**的：

        retrieved   检索管线留下的（过了 C-9 权限 + C-10 citation）
        available   **真正喂进 prompt 的**（再过一道 C-3 token 预算）⊆ retrieved
        evidence    这题声明该引的依据

    ⚠️ `available` 与 `retrieved` **必须分开传**。合成一个的话，
    "检到了但装不下"会被归到"检索没检到"头上，而这两件事的修法
    （换检索器 vs 加预算）完全不同 —— 报告会把人指向错误的地方。

    判据住在这里一处 —— 报告和 trace 两条路径都调它，不各写一份。
    """
    avail = set(available)
    got = set(retrieved)
    ev = set(evidence)

    def uniq(xs: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(x) for x in xs))

    if cited is None:
        # 没自述：能算的只有"检索/预算漏了哪些依据"（那是它们的事），
        # "引了什么"这一整类**不可测**。
        return CitationVerdict(
            cited=None,
            evidence_not_retrieved=tuple(x for x in uniq(evidence) if x not in got),
            evidence_dropped=tuple(x for x in uniq(evidence) if x in got and x not in avail),
        )

    cited_u = uniq(cited)
    return CitationVerdict(
        cited=cited_u,
        fabricated=tuple(x for x in cited_u if x not in avail),
        # ⚠️ 必须**同时**在 `available` 里。只看 `x in ev` 的话，
        # 一个靠参数记忆背出正确答案的模型会在"依据召回"上拿满分 ——
        # 而那正是这个指标要抓的东西。
        evidence_cited=tuple(x for x in cited_u if x in avail and x in ev),
        evidence_not_retrieved=tuple(x for x in uniq(evidence) if x not in got),
        evidence_dropped=tuple(x for x in uniq(evidence) if x in got and x not in avail),
        evidence_ignored=tuple(
            x for x in uniq(evidence) if x in avail and x not in set(cited_u)
        ),
    )


@dataclass(frozen=True)
class AnswerScore:
    """一次生成的判分。**一条样本一行**（多次采样就是多行）。"""

    task_id: str
    difficulty: str
    question: str
    points_total: int
    hits: tuple[str, ...] = ()
    missed: tuple[str, ...] = ()
    #: `(要点 label, 命中的那个说法)` —— 分数要**可复核**：
    #: 报告能回答"这题为什么算过了"，而不只是给一个数。
    hit_by: tuple[tuple[str, str], ...] = ()
    answer_chars: int = 0
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""
    out_of_corpus: tuple[str, ...] = ()
    #: 引用核对结果。**不是**一个分数，是一组事实 + 归因。
    citation: CitationVerdict = field(default_factory=CitationVerdict)
    #: **检索**花了多久。与 `latency_ms`（生成）分开 —— 合起来报的话，
    #: dense 的 673 ms 会被生成延迟掩盖，"换检索器值不值"这个问题就看不见了。
    retrieval_ms: float = 0.0
    #: 真正喂进 prompt 的上下文有多少 token（C-3 之后）。
    context_tokens: int = 0
    #: 喂进去几片（`len(contexts)`）。
    context_size: int = 0
    #: `(chunk_id, tokens, 原因)` —— 被 **token 预算**丢掉的片。
    #:
    #: ⚠️ 与 `out_of_corpus` 是两件事：后者是"语料里根本没有"，
    #: 这里的是"检到了但装不进窗口"。C-4：**静默截断是 bug**，每条都要留原因。
    context_dropped: tuple[tuple[str, int, str], ...] = ()

    @property
    def recall(self) -> float | None:
        """要点召回。`None` = **不可测**（这题没声明要点）。

        ⚠️ 不可测**不是** 1.0。把两者混起来，覆盖率会看起来比实际高，
        而报告里没有任何东西会提醒你。
        """
        if self.points_total <= 0:
            return None
        return len(self.hits) / self.points_total

    @property
    def passed(self) -> bool:
        """通过 = **全部**要点都答到。没声明要点 ⇒ 不算通过（没测，不是做到了）。"""
        return self.points_total > 0 and len(self.hits) == self.points_total

    # ---- 引用：三个数各答一个问题，别混成一个

    @property
    def fabricated(self) -> tuple[str, ...]:
        """引了**没给它**的来源。非空 ⇒ 编造。**这是判据，不是分数。**"""
        return self.citation.fabricated

    @property
    def grounded_rate(self) -> float | None:
        return self.citation.grounded_rate

    @property
    def evidence_recall(self) -> float | None:
        return self.citation.evidence_recall

    @property
    def evidence_used_rate(self) -> float | None:
        return self.citation.evidence_used_rate

    @property
    def context_dropped_tokens(self) -> int:
        return sum(t for _, t, _ in self.context_dropped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "difficulty": self.difficulty,
            "question": self.question,
            "points_total": self.points_total,
            "hits": list(self.hits),
            "missed": list(self.missed),
            "hit_by": [list(x) for x in self.hit_by],
            "answer_chars": self.answer_chars,
            "latency_ms": self.latency_ms,
            "retrieval_ms": self.retrieval_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "out_of_corpus": list(self.out_of_corpus),
            "citation": self.citation.as_dict(),
            "context_tokens": self.context_tokens,
            "context_size": self.context_size,
            "context_dropped": [list(x) for x in self.context_dropped],
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "AnswerScore":
        # 同 `out_of_corpus`：**缺键就拒绝**，不许把缺键读成空。
        # 旧报告没有 `citation`，按空读会得到"这题没引用任何来源" ——
        # 那是**一个结论**，而真相是"这份报告根本没测引用"。
        if "citation" not in d:
            raise ValueError(
                f"报告里 {d.get('task_id', '?')} 缺 `citation` 字段 —— 这是**旧格式**报告"
                "（引用指标是后加的）。按空读会得到『答案器没引用任何来源』这个**假结论**，"
                "而真相是『这份报告没测引用』。请用当前代码重跑，不要手工补这个字段。"
            )
        # 同族：`context_tokens` 缺了 ⇒ 这份报告没有"喂进去多少 token"这个事实。
        # 按 0 读会得到"上下文不占窗口"，而真相是"没测"。
        if "context_tokens" not in d:
            raise ValueError(
                f"报告里 {d.get('task_id', '?')} 缺 `context_tokens` 字段 —— 这是**旧格式**"
                "报告（上下文预算与装配是后加的）。按 0 读会得到『上下文不占窗口』"
                "这个**假结论**。请用当前代码重跑。"
            )
        return AnswerScore(
            task_id=str(d["task_id"]),
            difficulty=str(d.get("difficulty", "")),
            question=str(d.get("question", "")),
            points_total=int(d.get("points_total", 0)),
            hits=tuple(str(x) for x in d.get("hits", ())),
            missed=tuple(str(x) for x in d.get("missed", ())),
            hit_by=tuple((str(a), str(b)) for a, b in d.get("hit_by", ())),
            answer_chars=int(d.get("answer_chars", 0)),
            latency_ms=float(d.get("latency_ms", 0.0)),
            retrieval_ms=float(d.get("retrieval_ms", 0.0)),
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            error=str(d.get("error", "")),
            out_of_corpus=tuple(str(x) for x in d.get("out_of_corpus", ())),
            citation=CitationVerdict.from_dict(d["citation"]),
            context_tokens=int(d["context_tokens"]),
            context_size=int(d.get("context_size", 0)),
            context_dropped=tuple(
                (str(a), int(b), str(c)) for a, b, c in d.get("context_dropped", ())
            ),
        )


def score_answer(item: TaskItem, text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    """按 `item.required_points` 判分。返回 `(命中, 没答到, 凭什么算命中)`。

    判据本身住在 `RequiredPoint.matched_by` —— 校验期和判据期**用同一条规则**。
    两处各写一份，改了一处就会出现"校验说声明没问题、判分说答不到"这种鬼故事。
    """
    hits: list[str] = []
    missed: list[str] = []
    hit_by: list[tuple[str, str]] = []
    for p in item.required_points:
        by = p.matched_by(text)
        if by:
            hits.append(p.label)
            hit_by.append((p.label, by))
        else:
            missed.append(p.label)
    return tuple(hits), tuple(missed), tuple(hit_by)

def pass_at_k(n: int, c: int, k: int) -> float:
    """`n` 次采样里 `c` 次成功，`k` 次里至少一次成功的概率（无偏估计）。

        1 - C(n-c, k) / C(n, k)

    用乘积形式算，避免大组合数溢出。`n < k` 时退化成 `pass@n` ——
    样本不够就别硬算，但**要说清退化了**（报告里会印）。
    """
    if n <= 0 or k <= 0 or c <= 0:
        return 0.0
    k = min(k, n)
    if n - c < k:
        return 1.0
    p = 1.0
    for i in range(k):
        p *= (n - c - i) / (n - i)
    return 1.0 - p


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _fmt_opt(v: float | None, *, digits: int = 4) -> str:
    """`None` = **不可测**，印成 `—` 而不是 `0.0000`。

    ⚠️ 印 0 会把"没测"说成"测了，结果是 0" —— 这正是这个项目反复挡的那种谎。
    """
    return "—" if v is None else f"{v:.{digits}f}"


# ---------------------------------------------------------------- 聚合


@dataclass(frozen=True)
class AnswerGroup:
    n_samples: int = 0
    n_tasks: int = 0
    samples_per_task: int = 1
    #: 有要点声明的样本数 —— **答案级指标的分母就是这个**，不是 `n_samples`。
    n_scorable: int = 0
    tasks_unscorable: int = 0
    mean_recall: float = 0.0
    pass_at_1: float = 0.0
    pass_at_k: float = 0.0
    #: **生成**耗时（均值）。检索耗时是下面那个 —— 两个分开报，见 `AnswerScore`。
    latency_ms: float = 0.0
    #: **检索**耗时（均值）。
    retrieval_ms: float = 0.0
    mean_tokens: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    #: 每答到一个要点花了多少字符。**抄文档式的答案会在这里露馅。**
    chars_per_hit_point: float = 0.0
    errors: int = 0

    # ---- 上下文（喂进去的那一份）
    #: 喂进 prompt 的上下文 token（均值）。
    context_tokens: float = 0.0
    total_context_tokens: int = 0
    #: 喂进去几片（均值）。
    context_size: float = 0.0
    #: 被 **token 预算**丢掉的片数（总）。⚠️ C-4：丢了几片只是总数，
    #: **丢了哪几片、为什么**在逐题明细里点名。
    context_dropped_total: int = 0
    samples_with_context_drops: int = 0

    @property
    def total_ms(self) -> float:
        """检索 + 生成。⚠️ 只用它报"总耗时"的话，两个分量会被加成一个数。"""
        return self.retrieval_ms + self.latency_ms

    # ---- 引用（分母 = **自述了引用的**样本，不是全部样本）
    #: 自述了引用的样本数。**引用指标的每一个分母都是它** ——
    #: 没自述的样本放进来会把"没测"算成"没引"。
    n_with_citations: int = 0
    #: 没自述引用的样本数。**不为 0 就必须在报告里点名。**
    n_without_citations: int = 0
    #: 编造引用的**总条数**（不是样本数）。
    fabricated_total: int = 0
    #: 有编造引用的**样本数** —— 这个才是"多少题犯了"。
    samples_with_fabrication: int = 0
    grounded_rate: float | None = None
    evidence_recall: float | None = None
    evidence_used_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_samples": self.n_samples,
            "n_tasks": self.n_tasks,
            "samples_per_task": self.samples_per_task,
            "n_scorable": self.n_scorable,
            "tasks_unscorable": self.tasks_unscorable,
            "mean_recall": self.mean_recall,
            "pass_at_1": self.pass_at_1,
            "pass_at_k": self.pass_at_k,
            "latency_ms": self.latency_ms,
            "retrieval_ms": self.retrieval_ms,
            "mean_tokens": self.mean_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "chars_per_hit_point": self.chars_per_hit_point,
            "errors": self.errors,
            "context_tokens": self.context_tokens,
            "total_context_tokens": self.total_context_tokens,
            "context_size": self.context_size,
            "context_dropped_total": self.context_dropped_total,
            "samples_with_context_drops": self.samples_with_context_drops,
            "n_with_citations": self.n_with_citations,
            "n_without_citations": self.n_without_citations,
            "fabricated_total": self.fabricated_total,
            "samples_with_fabrication": self.samples_with_fabrication,
            "grounded_rate": self.grounded_rate,
            "evidence_recall": self.evidence_recall,
            "evidence_used_rate": self.evidence_used_rate,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "AnswerGroup":
        def opt_float(key: str) -> float | None:
            v = d.get(key)
            return None if v is None else float(v)

        return AnswerGroup(
            n_samples=int(d.get("n_samples", 0)),
            n_tasks=int(d.get("n_tasks", 0)),
            samples_per_task=int(d.get("samples_per_task", 1)),
            n_scorable=int(d.get("n_scorable", 0)),
            tasks_unscorable=int(d.get("tasks_unscorable", 0)),
            mean_recall=float(d.get("mean_recall", 0.0)),
            pass_at_1=float(d.get("pass_at_1", 0.0)),
            pass_at_k=float(d.get("pass_at_k", 0.0)),
            latency_ms=float(d.get("latency_ms", 0.0)),
            retrieval_ms=float(d.get("retrieval_ms", 0.0)),
            mean_tokens=float(d.get("mean_tokens", 0.0)),
            total_tokens=int(d.get("total_tokens", 0)),
            total_cost_usd=float(d.get("total_cost_usd", 0.0)),
            chars_per_hit_point=float(d.get("chars_per_hit_point", 0.0)),
            errors=int(d.get("errors", 0)),
            context_tokens=float(d.get("context_tokens", 0.0)),
            total_context_tokens=int(d.get("total_context_tokens", 0)),
            context_size=float(d.get("context_size", 0.0)),
            context_dropped_total=int(d.get("context_dropped_total", 0)),
            samples_with_context_drops=int(d.get("samples_with_context_drops", 0)),
            n_with_citations=int(d.get("n_with_citations", 0)),
            n_without_citations=int(d.get("n_without_citations", 0)),
            fabricated_total=int(d.get("fabricated_total", 0)),
            samples_with_fabrication=int(d.get("samples_with_fabrication", 0)),
            grounded_rate=opt_float("grounded_rate"),
            evidence_recall=opt_float("evidence_recall"),
            evidence_used_rate=opt_float("evidence_used_rate"),
        )


def _group(scores: Sequence[AnswerScore], *, samples_per_task: int) -> AnswerGroup:
    """聚合。**不可测的样本不进任何分母**，但会被单独计数。"""
    scorable = [s for s in scores if s.points_total > 0]
    by_task: dict[str, list[bool]] = {}
    for s in scorable:
        by_task.setdefault(s.task_id, []).append(s.passed)

    k = max(1, samples_per_task)
    per_task = [pass_at_k(len(v), sum(v), k) for v in by_task.values()]
    hits_total = sum(len(s.hits) for s in scorable)
    chars_total = sum(s.answer_chars for s in scorable)

    # ---- 引用：分母只有**自述了引用的**样本。
    # 把没自述的算进来，"没测"就变成了"没引"—— 一个没人会发现的假结论。
    #
    # ⚠️ 三个比例一律**逐样本求均值**（macro），和本组里的 `mean_recall` /
    # `pass_at_1` / `latency_ms` 一个口径。这里曾经用 Σ/Σ（micro），
    # 于是"依据召回 0.2955"和检索报告里**同一个量**的 `context_recall 0.4424`
    # 对不上 —— 同一个东西两个定义，读者只会以为自己看错了。
    reported = [s for s in scores if s.citation.reported]
    fabricated_total = sum(len(s.citation.fabricated) for s in reported)
    grounded_each = [
        g for g in (s.citation.grounded_rate for s in reported) if g is not None
    ]
    recall_each = [
        len(s.citation.evidence_cited) / s.citation.evidence_total
        for s in reported
        if s.citation.evidence_total > 0
    ]
    used_each = [
        u for u in (s.citation.evidence_used_rate for s in reported) if u is not None
    ]

    return AnswerGroup(
        n_samples=len(scores),
        n_tasks=len({s.task_id for s in scores}),
        samples_per_task=samples_per_task,
        n_scorable=len(scorable),
        tasks_unscorable=len({s.task_id for s in scores if s.points_total <= 0}),
        mean_recall=_mean([s.recall for s in scorable if s.recall is not None]),
        pass_at_1=(sum(1 for s in scorable if s.passed) / len(scorable)) if scorable else 0.0,
        pass_at_k=_mean(per_task),
        latency_ms=_mean([s.latency_ms for s in scores]),
        retrieval_ms=_mean([s.retrieval_ms for s in scores]),
        mean_tokens=_mean([s.prompt_tokens + s.completion_tokens for s in scores]),
        total_tokens=sum(s.prompt_tokens + s.completion_tokens for s in scores),
        total_cost_usd=sum(s.cost_usd for s in scores),
        chars_per_hit_point=(chars_total / hits_total) if hits_total else 0.0,
        errors=sum(1 for s in scores if s.error),
        context_tokens=_mean([s.context_tokens for s in scores]),
        total_context_tokens=sum(s.context_tokens for s in scores),
        context_size=_mean([s.context_size for s in scores]),
        context_dropped_total=sum(len(s.context_dropped) for s in scores),
        samples_with_context_drops=sum(1 for s in scores if s.context_dropped),
        n_with_citations=len(reported),
        n_without_citations=len(scores) - len(reported),
        fabricated_total=fabricated_total,
        samples_with_fabrication=sum(1 for s in reported if s.citation.fabricated),
        grounded_rate=_mean(grounded_each) if grounded_each else None,
        evidence_recall=_mean(recall_each) if recall_each else None,
        evidence_used_rate=_mean(used_each) if used_each else None,
    )


# ---------------------------------------------------------------- 报告


@dataclass(frozen=True)
class AnswerReport:
    topic: str
    answerer: str
    retriever: str
    top_k: int
    samples_per_task: int
    items: tuple[AnswerScore, ...] = ()
    overall: AnswerGroup = field(default_factory=AnswerGroup)
    by_difficulty: dict[str, AnswerGroup] = field(default_factory=dict)
    corpus_chunks: int = 0
    generated_at: str = ""
    #: ⚠️ `True` ⇒ 这些分数是**判据的上/下限校准**，不是模型成绩。
    calibration: bool = False
    #: 上下文窗口（token）。⚠️ 这是**输入**不是事实 —— 换个模型就换个数。
    #: 但它必须进报告："丢了 3 片"这个事实脱离"预算多少"无法解释。
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT
    #: 每 token 几个字符。**语料的属性**，进报告见 `asuka.context` 的模块 docstring。
    chars_per_token: int = CHARS_PER_TOKEN
    #: 提示词身份（`版本-内容指纹`）。⚠️ **提示词是被测系统的一部分**：
    #: 它决定模型会不会自述引用、也决定答案的详略与覆盖面。
    #: 不记它 ⇒ 换个 prompt 再跑，`regression` 会把"提示词的效果"
    #: **静默读成"系统退步/进步"**（同 `context_budget` 当初的洞）。
    #: ⚠️ 无提示词的答案器（oracle/null）是 `""`，而"老报告没记录"也是 `""` ——
    #: 这两者**区分不开**，所以对比门对"两边不一致"一律拒绝，宁可说"无法确认"。
    prompt_id: str = ""

    @property
    def context_available(self) -> int:
        """预算里**真正能放上下文**的部分 = total − 留给输出的。"""
        return self.context_budget - self.reserved_for_output

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "answerer": self.answerer,
            "retriever": self.retriever,
            "top_k": self.top_k,
            "samples_per_task": self.samples_per_task,
            "corpus_chunks": self.corpus_chunks,
            "generated_at": self.generated_at,
            "calibration": self.calibration,
            "context_budget": self.context_budget,
            "reserved_for_output": self.reserved_for_output,
            "chars_per_token": self.chars_per_token,
            "prompt_id": self.prompt_id,
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
    def load(cls, path: Path, *, verify: bool = True) -> "AnswerReport":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")), verify=verify)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, verify: bool = True) -> "AnswerReport":
        obj = cls(
            topic=str(d.get("topic", "")),
            answerer=str(d.get("answerer", "")),
            retriever=str(d.get("retriever", "")),
            top_k=int(d.get("top_k", 0)),
            samples_per_task=int(d.get("samples_per_task", 1)),
            items=tuple(AnswerScore.from_dict(i) for i in d.get("items", ())),
            overall=AnswerGroup.from_dict(d.get("overall", {})),
            by_difficulty={
                k: AnswerGroup.from_dict(v) for k, v in (d.get("by_difficulty") or {}).items()
            },
            corpus_chunks=int(d.get("corpus_chunks", 0)),
            generated_at=str(d.get("generated_at", "")),
            calibration=bool(d.get("calibration", False)),
            context_budget=int(d.get("context_budget", DEFAULT_CONTEXT_BUDGET)),
            reserved_for_output=int(d.get("reserved_for_output", DEFAULT_RESERVED_FOR_OUTPUT)),
            chars_per_token=int(d.get("chars_per_token", CHARS_PER_TOKEN)),
            prompt_id=str(d.get("prompt_id", "")),
        )
        if verify:
            obj._verify_aggregates()
        return obj

    def _verify_aggregates(self) -> None:
        """聚合值拿单题**核对** —— 对不上说明报告是拼出来的，或判据定义变过。"""
        if not self.items:
            return
        fresh = _group(self.items, samples_per_task=self.samples_per_task)
        drift: list[str] = []
        for name, stored, again in (
            ("overall.mean_recall", self.overall.mean_recall, fresh.mean_recall),
            ("overall.pass_at_1", self.overall.pass_at_1, fresh.pass_at_1),
            ("overall.pass_at_k", self.overall.pass_at_k, fresh.pass_at_k),
            ("overall.chars_per_hit_point", self.overall.chars_per_hit_point, fresh.chars_per_hit_point),
            ("overall.retrieval_ms", self.overall.retrieval_ms, fresh.retrieval_ms),
            ("overall.context_tokens", self.overall.context_tokens, fresh.context_tokens),
            ("overall.context_size", self.overall.context_size, fresh.context_size),
            ("overall.grounded_rate", self.overall.grounded_rate, fresh.grounded_rate),
            ("overall.evidence_recall", self.overall.evidence_recall, fresh.evidence_recall),
            ("overall.evidence_used_rate", self.overall.evidence_used_rate, fresh.evidence_used_rate),
        ):
            # `None`（不可测）也是**一个值**：存着 `None`、重算成 0.6 同样是漂移。
            # 只用 `abs(stored - again)` 比会把 `None` 漏过去（TypeError 被吞或直接崩）。
            if (stored is None) != (again is None):
                drift.append(f"{name}：文件里 {stored}，按单题重算是 {again}")
            elif stored is not None and again is not None and abs(stored - again) > 1e-3:
                drift.append(f"{name}：文件里 {stored:.4f}，按单题重算是 {again:.4f}")
        for name, stored_i, again_i in (
            ("overall.n_scorable", self.overall.n_scorable, fresh.n_scorable),
            ("overall.tasks_unscorable", self.overall.tasks_unscorable, fresh.tasks_unscorable),
            ("overall.total_tokens", self.overall.total_tokens, fresh.total_tokens),
            ("overall.errors", self.overall.errors, fresh.errors),
            ("overall.total_context_tokens", self.overall.total_context_tokens, fresh.total_context_tokens),
            ("overall.context_dropped_total", self.overall.context_dropped_total, fresh.context_dropped_total),
            (
                "overall.samples_with_context_drops",
                self.overall.samples_with_context_drops,
                fresh.samples_with_context_drops,
            ),
            ("overall.n_with_citations", self.overall.n_with_citations, fresh.n_with_citations),
            ("overall.n_without_citations", self.overall.n_without_citations, fresh.n_without_citations),
            ("overall.fabricated_total", self.overall.fabricated_total, fresh.fabricated_total),
            (
                "overall.samples_with_fabrication",
                self.overall.samples_with_fabrication,
                fresh.samples_with_fabrication,
            ),
        ):
            if stored_i != again_i:
                drift.append(f"{name}：文件里 {stored_i}，按单题重算是 {again_i}")

        # `samples_per_task` **本身是聚合的输入**（它决定 pass@k 的 k），
        # 所以它也必须和 items 对得上。少了这一步，改掉文件里的 `samples_per_task`
        # 会让"用新 k 重算"和"存着的 pass@k"一起漂移 —— 而两边一致，校验就**静默通过**。
        counts: dict[str, int] = {}
        for s in self.items:
            counts[s.task_id] = counts.get(s.task_id, 0) + 1
        odd = {t: n for t, n in counts.items() if n != self.samples_per_task}
        if odd:
            drift.append(
                f"samples_per_task={self.samples_per_task}，"
                f"但这些题的样本条数对不上：{odd}"
            )

        if drift:
            raise ValueError(
                "答案报告的聚合值与自己的单条样本对不上：\n  "
                + "\n  ".join(drift)
                + "\n  ⇒ 这份报告不可信，别拿它做结论。"
            )


# ---------------------------------------------------------------- 评测


def evaluate_answers(
    kb: KnowledgeBase,
    dataset: Dataset,
    answerer: Answerer,
    *,
    top_k: int = 5,
    samples_per_task: int = 1,
    corpus_chunks: int = 0,
    trace: Any | None = None,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserved_for_output: int = DEFAULT_RESERVED_FOR_OUTPUT,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> AnswerReport:
    """检索 → **装配** → 生成 → 判分。每题采样 `samples_per_task` 次。

    ⚠️ 检索这一层**复用 `kb.search`**，与检索评测同一条路径 ——
    否则"检索检到了但答案没答对"这个归因就不成立（两次跑的上下文不是同一批）。

    ⚠️ 检索之后还有一步**装配**（`asuka.context.assemble`，C-3/C-4）：
    `top_k` 是条数不是 token 数，10 片长文档能撑爆窗口。
    所以喂给 answerer 的是**受预算约束**的那一份，不是 `result.kept` 本身。
    **引用判据的 `available` 也必须用装配后的 id** —— 被预算丢掉的片模型没看见，
    引用了它就是编造。这一处如果忘了改，"没给它的"会被当成"给过它"，
    而报告里没有任何东西会提醒你。

    `trace` 非空时把每一步写进去（`asuka.trace.Trace`）。**只写不读** ——
    报告的聚合值仍然由下面这段算，trace 是另一条独立路径（见 `Trace.verify_against`）。
    """
    if not dataset.resolved:
        raise ValueError("数据集还没 resolve（evidence 未解析成 chunk_id）")

    # `is_calibration` 必须被**显式**自述。缺失就当"真模型"是最坏的选择：
    # 校准分数会被读成模型成绩，而报告里没有任何东西会提醒你。
    calibration = getattr(answerer, "is_calibration", None)
    if not isinstance(calibration, bool):
        raise ValueError(
            f"answerer {getattr(answerer, 'name', answerer)!r} 没有自述 `is_calibration`。"
            "报告读者必须能分清『模型成绩』和『判据校准』—— "
            "不说的 answerer 会被拒绝，而不是被默认当成真模型。"
        )

    if samples_per_task < 1:
        raise ValueError(f"samples_per_task 必须 >= 1，收到 {samples_per_task}")

    scores: list[AnswerScore] = []
    for item in dataset.items:
        t0 = time.perf_counter()
        result = kb.search(item.question, limit=top_k)
        retrieval_ms = (time.perf_counter() - t0) * 1000.0

        # 装配：C-1 转换 + C-3 预算 + C-4 留痕。`kept` 是按**相关性**排的。
        assembled = assemble(
            kb.pipeline,
            result,
            budget=context_budget,
            reserved_for_output=reserved_for_output,
            chars_per_token=chars_per_token,
        )
        contexts = assembled.items
        # ⚠️ 两个集合**都要**传下去，不能合成一个：
        # `retrieved` = 检索管线留下的；`available` = 真正喂进 prompt 的。
        # 合成一个的话，"检到了但装不下"会被归到"检索没检到"头上。
        retrieved = [c.chunk_id for c in result.kept]
        available = list(assembled.chunk_ids)
        # 该题声明该引的依据。
        evidence = dataset.resolved.get(item.task_id, ())

        if trace is not None:
            # ⚠️ 只记 chunk_id，**不复制正文** —— 正文在语料里，按 id 查得到。
            # 复制会让 trace 膨胀，而且产生第二个真相源（改了语料两边不一致）。
            trace.emit(
                "retrieval",
                task_id=item.task_id,
                query=item.question,
                limit=top_k,
                # `kept` = 检索管线留下的（过了 C-9 权限 + C-10 citation）。
                kept=[c.chunk_id for c in result.kept],
                citations=[c.citation for c in result.kept],
                denied=[c.chunk_id for c in result.denied],
                dropped_no_citation=[c.chunk_id for c in result.dropped_no_citation],
                # `context` = **真正喂进 prompt 的**（再过一道 C-3 预算）。
                # 两个字段名字不同、含义不同：混成一个的话，"检到了"会被读成"模型看见了"。
                context=list(available),
                dropped_budget=[list(x) for x in assembled.dropped_reasons],
                context_tokens=assembled.total_tokens,
                latency_ms=retrieval_ms,
            )

        for _ in range(samples_per_task):
            ans = answerer.answer(item, contexts)
            hits, missed, hit_by = score_answer(item, ans.text)
            verdict = score_citations(
                ans.citations, available=available, retrieved=retrieved, evidence=evidence
            )

            if trace is not None:
                # 答案全文**要存**：审计最常问的就是"它到底答了什么"。
                # 24 题 × 几百字符 = 几十 KB，不值得为省这点空间丢掉可审计性。
                trace.emit(
                    "generation",
                    task_id=item.task_id,
                    answerer=getattr(answerer, "name", "?"),
                    text=ans.text,
                    chars=len(ans.text),
                    latency_ms=ans.latency_ms,
                    prompt_tokens=ans.prompt_tokens,
                    completion_tokens=ans.completion_tokens,
                    cost_usd=ans.cost_usd,
                    error=ans.error,
                    # `None` 要显式落成 `null`：缺字段和"没自述"读起来是两件事。
                    citations=None if ans.citations is None else list(ans.citations),
                )
                trace.emit(
                    "scoring",
                    task_id=item.task_id,
                    points_total=len(item.required_points),
                    hits=list(hits),
                    missed=list(missed),
                    hit_by=[list(x) for x in hit_by],
                    out_of_corpus=list(item.out_of_corpus),
                    fabricated=list(verdict.fabricated),
                    evidence_ignored=list(verdict.evidence_ignored),
                    evidence_not_retrieved=list(verdict.evidence_not_retrieved),
                    evidence_dropped=list(verdict.evidence_dropped),
                )

            scores.append(
                AnswerScore(
                    task_id=item.task_id,
                    difficulty=item.difficulty,
                    question=item.question,
                    points_total=len(item.required_points),
                    hits=hits,
                    missed=missed,
                    hit_by=hit_by,
                    answer_chars=len(ans.text),
                    latency_ms=ans.latency_ms,
                    retrieval_ms=retrieval_ms,
                    prompt_tokens=ans.prompt_tokens,
                    completion_tokens=ans.completion_tokens,
                    cost_usd=ans.cost_usd,
                    error=ans.error,
                    out_of_corpus=tuple(item.out_of_corpus),
                    citation=verdict,
                    context_tokens=assembled.total_tokens,
                    context_size=len(contexts),
                    context_dropped=assembled.dropped_reasons,
                )
            )

    by_diff: dict[str, AnswerGroup] = {}
    for level in ("simple", "medium", "hard"):
        subset = [s for s in scores if s.difficulty == level]
        if subset:
            by_diff[level] = _group(subset, samples_per_task=samples_per_task)

    return AnswerReport(
        topic=dataset.topic,
        answerer=getattr(answerer, "name", "?"),
        retriever=kb.kind,
        top_k=top_k,
        samples_per_task=samples_per_task,
        items=tuple(scores),
        overall=_group(scores, samples_per_task=samples_per_task),
        by_difficulty=by_diff,
        corpus_chunks=corpus_chunks,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        calibration=calibration,
        context_budget=context_budget,
        reserved_for_output=reserved_for_output,
        chars_per_token=chars_per_token,
        # 提示词身份由答案器**自述**（`getattr` 取，没有就是 `""`）。
        # 不用 `isinstance` 窄化：协议本来就靠结构满足，写死类型会把第三方答案器挡在外面。
        prompt_id=str(getattr(answerer, "prompt_id", "") or ""),
    )


# ---------------------------------------------------------------- 报告渲染


def render_markdown(report: AnswerReport) -> str:
    """人读的答案级报告。

    ⚠️ 校准跑**必须在最上面**说清楚 —— 否则 `oracle` 的 1.0 会被读成"模型满分"。
    """
    o = report.overall
    lines: list[str] = []
    lines.append(f"# Asuka 答案级评测 · {report.topic}")
    lines.append("")
    lines.append(f"- answerer：`{report.answerer}`")
    lines.append(f"- 检索器：`{report.retriever}`（top_k={report.top_k}）")
    lines.append(f"- 采样：每题 {report.samples_per_task} 次")
    lines.append(f"- 语料：{report.corpus_chunks} chunks")
    lines.append(
        f"- 上下文窗口：{report.context_budget} tokens"
        f"（留 {report.reserved_for_output} 给输出 ⇒ 可放 {report.context_available}）"
        f" · 每 token {report.chars_per_token} 字符"
    )
    lines.append(f"- 生成时间：{report.generated_at}")
    lines.append("")

    if report.calibration:
        lines.append("> ⚠️ **这是校准跑，不是模型成绩。**")
        lines.append(
            "> `answerer` 是**假答案器**（不调用模型），用来给规则判据划上下界："
            "`oracle` 必须 1.0，`null` 必须 0.0。"
        )
        lines.append(
            "> 它的用处是证明**判据本身没坏** —— 真模型成绩要等接入 LLM 之后才有。"
        )
        lines.append("")

    lines.append("## 总体")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 必答要点召回（mean） | {o.mean_recall:.4f} |")
    lines.append(f"| pass@1（稳定做到） | {o.pass_at_1:.4f} |")
    if report.samples_per_task > 1:
        # `samples == 1` 时两行是同一个数 —— 印两遍同名指标只会让人以为看错了。
        lines.append(f"| pass@{report.samples_per_task}（能做到） | {o.pass_at_k:.4f} |")
    # ⚠️ 检索与生成**分开报**。合成一个"平均耗时"的话，
    # dense 的 673 ms 会被生成延迟稀释掉，"换检索器值不值"就看不见了。
    lines.append(f"| 检索耗时（mean） | {o.retrieval_ms:.1f} ms |")
    lines.append(f"| 生成耗时（mean） | {o.latency_ms:.1f} ms |")
    lines.append(f"| 合计耗时（mean） | {o.total_ms:.1f} ms |")
    lines.append(f"| 上下文 token（mean） | {o.context_tokens:.1f} |")
    lines.append(f"| 上下文片数（mean） | {o.context_size:.1f} |")
    lines.append(f"| 生成 token（mean，prompt+completion） | {o.mean_tokens:.1f} |")
    lines.append(f"| 总 token | {o.total_tokens} |")
    lines.append(f"| 每答到一个要点的字符数 | {o.chars_per_hit_point:.1f} |")
    lines.append("")

    # ---- 成本：**"没测"不等于"免费"**
    if report.calibration:
        lines.append(
            "| 总成本 | —（**校准跑**：不调用模型，成本这个量在这里不存在） |"
        )
    elif o.total_cost_usd > 0:
        lines.append(f"| 总成本 | ${o.total_cost_usd:.4f} |")
    else:
        lines.append("| 总成本 | —（**不可测**） |")
        lines.append("")
        lines.append(
            "⚠️ 这是**非校准**跑，但全部样本的 `cost_usd` 都是 0。"
            "**『没测』不等于『免费』** —— 要么 answerer 没填成本，要么单价没接上。"
            "别把这一行读成『这次没花钱』。"
        )
    lines.append("")

    if o.tasks_unscorable:
        lines.append(
            f"⚠️ 有 **{o.tasks_unscorable}** 道题**没有声明必答要点**，"
            f"它们**不在**任何答案级分母里（分母是 {o.n_scorable} 条样本 / "
            f"{o.n_tasks - o.tasks_unscorable} 道题）。"
            "『没测』不等于『答对了』—— 所以这里既不算 0 也不算 1。"
        )
        lines.append("")

    if o.errors:
        lines.append(f"⚠️ 有 **{o.errors}** 次生成**报错**（不是答错）。见逐题明细的 `err` 列。")
        lines.append("")

    lines.append(
        f"> `pass@k` 的通过定义是**全部要点都答到**（recall = 1.0），"
        f"不用阈值 —— 阈值是个自由旋钮，会被调到来凑结论。"
        f"梯度信息由第一行的 `mean_recall` 提供。"
    )
    lines.append("")
    lines.append(
        "> ⚠️ `chars_per_hit_point` 是**已知缺口**的度量：判据是要点召回，"
        "所以把整篇文档抄进答案也会得高分。这个数越大越可疑 —— "
        "但它只是**信号**，不是判据（要真判『答得冗不冗』需要裁判模型）。"
    )
    lines.append("")

    # ---- 上下文：装了什么、丢了什么、为什么（C-4）
    lines.append("## 上下文（Context）")
    lines.append("")
    lines.append(
        f"- 窗口 {report.context_budget} tokens，可放 **{report.context_available}**"
        f"（留给输出 {report.reserved_for_output}）"
    )
    lines.append(
        f"- 实际占用 **{o.context_tokens:.1f}** tokens（均值）· "
        f"{o.context_size:.1f} 片 · 共 {o.total_context_tokens} tokens"
    )
    lines.append(
        f"- 被预算丢掉 **{o.context_dropped_total}** 片，涉及 "
        f"**{o.samples_with_context_drops}** 条样本"
    )
    lines.append("")
    lines.append(
        "> ⚠️ `top_k` 是**条数**，不是窗口占用。检索之后必须有这一步装配："
        "10 片长文档能撑爆 8k 窗口，而失败发生在**模型那一侧**"
        "（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧 —— 评测跑得好好的，线上全崩。"
    )
    lines.append("")
    lines.append(
        "> ⚠️ 取舍顺序按**相关性**（检索名次写进 `ContextItem.priority`）。"
        "不写的话内核的 `allocate` 会退化成按 `chunk_id` **字母序**丢 —— "
        "丢掉第一名、留下最后一名，而且是**静默**的：分数照出，只是低了一点。"
    )
    lines.append("")

    dropped_rows = [(s.task_id, s.context_dropped) for s in report.items if s.context_dropped]
    if dropped_rows:
        lines.append(f"### 被预算丢掉的片（{len(dropped_rows)} 题）")
        lines.append("")
        for tid, drops in dropped_rows:
            lines.append(f"- `{tid}`")
            for cid, toks, reason in drops:
                lines.append(f"  - `{cid}`（{toks} tokens）—— {reason}")
        lines.append("")
        lines.append(
            "> 这是 **C-4**：静默截断是 bug，每条被丢的都要留下原因。"
            "⚠️ 内核给的 reason 措辞是 `token budget exhausted (已用/可用)` —— "
            "它说的是**这一条塞不进剩下的空间**，不一定等于『预算刚好用完』。"
            "对不上的时候以括号里的两个数为准。"
        )
        lines.append("")

    # ---- 引用：三个数各答一个问题
    lines.append("## 引用（Citation）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 引用有依据率 grounded | {_fmt_opt(o.grounded_rate)} |")
    lines.append(f"| 依据召回（端到端） | {_fmt_opt(o.evidence_recall)} |")
    lines.append(f"| 依据用上率（纯生成侧） | {_fmt_opt(o.evidence_used_rate)} |")
    lines.append(
        f"| 编造引用 | {o.fabricated_total} 条 / {o.samples_with_fabrication} 题 |"
    )
    lines.append("")
    lines.append(
        "> 三个数**各答一个问题，不能互相替代**：`grounded` = 引的东西**给它了吗**"
        "（< 1 就是编造）；`依据召回` = 该引的依据引到没有（⚠️ **同时受检索和生成影响**，"
        "单独看会误判）；`依据用上率` = **给了它的**依据它引了几成（纯生成侧，"
        "检索漏没漏与它无关 —— 检索没给的**不进这个分母**，否则检索越差它越高，方向就反了）。"
    )
    lines.append("")
    lines.append(
        "> 三个数一律**逐样本求均值**，与检索报告的 `context_recall` 同口径 —— "
        "`oracle` 跑出来的 `依据召回` 就等于那份报告的 `context_recall`"
        "（它把拿到的依据一条不漏地引了）。**同一个量只有一处定义。**"
    )
    lines.append("")
    lines.append(
        "> 为什么**不**报 `citation_precision`（引的东西里有多少条属于 ground truth）："
        "ground truth 是**最少必要依据**，不是**唯一允许引的依据**。"
        "引了别的真实上下文不算错，用 precision 罚它等于**奖励『少引』**。"
    )
    lines.append("")

    if o.n_without_citations:
        lines.append(
            f"⚠️ 有 **{o.n_without_citations}** 条样本**没有自述引用**，"
            f"它们**不在**任何引用分母里（分母是 {o.n_with_citations} 条）。"
            "『没自述』不等于『没引用』—— 所以这里既不算 0 也不算 1。"
        )
        lines.append("")

    fabricated_rows = [
        (s.task_id, s.citation.fabricated)
        for s in report.items
        if s.citation.fabricated
    ]
    if fabricated_rows:
        lines.append(
            f"⚠️ **有 {len(fabricated_rows)} 题引用了没给它的来源** —— "
            "这是**编造**，不是『答得不够好』。它引用的是这次检索**没给它**的材料："
        )
        lines.append("")
        for tid, ids in fabricated_rows:
            lines.append(f"- `{tid}` 引了但没有的来源：{list(ids)}")
        lines.append("")

    # ---- 缺的依据归谁：**这是归因，不是待办**
    not_retrieved = sum(len(s.citation.evidence_not_retrieved) for s in report.items)
    dropped_ev = sum(len(s.citation.evidence_dropped) for s in report.items)
    ignored = sum(len(s.citation.evidence_ignored) for s in report.items)
    lines.append("### 缺的依据归谁")
    lines.append("")
    lines.append("| 归因 | 条数 | 该动什么 |")
    lines.append("|---|---|---|")
    lines.append(f"| 检索**根本没检到** | {not_retrieved} | 换检索器 / 扩语料 |")
    lines.append(f"| 检到了但**装不进预算** | {dropped_ev} | 加窗口 / 降 top_k |")
    lines.append(f"| 给了它却**没引** | {ignored} | 改 prompt / 换模型 |")
    lines.append("")
    lines.append(
        "> ⚠️ 这是**归因**，不是三个待办。`依据召回` 低时先看这三行再下结论 —— "
        "不然『检索没检到』会被读成『模型没用依据』，改错地方。"
    )
    lines.append(
        "> ⚠️ 第二行和第一行**必须分开**：『检到了但装不下』是这套评测里最容易误读的失败 —— "
        "分数低看起来像检索差，实际是**配置**（窗口 / top_k）不合适。"
    )
    lines.append("")

    if report.by_difficulty:
        lines.append("## 分难度")
        lines.append("")
        lines.append("| 难度 | 样本 | recall | pass@1 | pass@k | 平均 token |")
        lines.append("|---|---|---|---|---|---|")
        for level, g in report.by_difficulty.items():
            lines.append(
                f"| {level} | {g.n_samples} | {g.mean_recall:.4f} | "
                f"{g.pass_at_1:.4f} | {g.pass_at_k:.4f} | {g.mean_tokens:.1f} |"
            )
        lines.append("")

    # ---- 全丢的题：最有行动价值的一段（缺哪个要点都写出来）
    by_task: dict[str, list[AnswerScore]] = {}
    for s in report.items:
        by_task.setdefault(s.task_id, []).append(s)

    all_missed = [
        (tid, rows)
        for tid, rows in by_task.items()
        if rows[0].points_total > 0 and all(not r.hits for r in rows)
    ]
    if all_missed:
        lines.append(f"## 一个要点都没答到的题（{len(all_missed)}）")
        lines.append("")
        for tid, rows in all_missed:
            lines.append(f"- `{tid}` ({rows[0].difficulty}) {rows[0].question[:70]}")
            for label in rows[0].missed:
                lines.append(f"  - 缺：{label}")
            if rows[0].out_of_corpus:
                lines.append(
                    "  - ⚠️ 这题还**声明了语料缺口** —— 有些要点语料里根本没有，"
                    "低分不该全记在生成头上。"
                )
        lines.append("")

    # ---- 不可测的题：点名，别让它们静默消失
    unscorable = [tid for tid, rows in by_task.items() if rows[0].points_total == 0]
    if unscorable:
        lines.append(f"## 不可测的题（{len(unscorable)}）—— 没有声明必答要点")
        lines.append("")
        lines.append(f"{unscorable}")
        lines.append("")
        lines.append(
            "> 这些题在答案级指标里**既不算 0 也不算 1**。"
            "它们的存在会让覆盖率看起来比实际高，所以必须点名。"
        )
        lines.append("")

    # ---- 逐题明细
    lines.append("## 逐题明细")
    lines.append("")
    lines.append(
        "| task | 难度 | 通过 / 采样 | recall | 字符 | ctx | token | 检ms | 生ms | 引用 | 依据 | err |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for tid, rows in by_task.items():
        n = len(rows)
        passed = sum(1 for r in rows if r.passed)
        recalls = [r.recall for r in rows if r.recall is not None]
        rec = f"{_mean(recalls):.2f}" if recalls else "—"
        chars = int(_mean([r.answer_chars for r in rows]))
        toks = int(_mean([r.prompt_tokens + r.completion_tokens for r in rows]))
        ctx = int(_mean([r.context_tokens for r in rows]))
        rms = _mean([r.retrieval_ms for r in rows])
        gms = _mean([r.latency_ms for r in rows])
        err = "!" if any(r.error for r in rows) else "—"
        if rows[0].citation.reported:
            # `引用` = 自述引了几条；`依据` = 该引的依据里引到了几条（端到端）。
            cited = int(_mean([len(r.citation.cited or ()) for r in rows]))
            ev_c = sum(len(r.citation.evidence_cited) for r in rows)
            ev_t = sum(r.citation.evidence_total for r in rows)
            cit = f"{cited} 条"
            evd = f"{ev_c}/{ev_t}" if ev_t else "—"
        else:
            cit = evd = "—"
        lines.append(
            f"| `{tid}` | {rows[0].difficulty} | {passed}/{n} | {rec} | "
            f"{chars} | {ctx} | {toks} | {rms:.0f} | {gms:.0f} | {cit} | {evd} | {err} |"
        )
    lines.append("")
    lines.append(
        "> `ctx` = 喂进去的上下文 token（受预算约束）。`检ms` / `生ms` **分开** —— "
        "合成一个数的话，检索器的差距会被生成耗时稀释掉。"
    )
    lines.append(
        "> `引用` / `依据` 两列印 `—` = **这题没自述引用**（不可测），"
        "不是『引用了 0 条』。"
    )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def build_parser() -> Any:
    """CLI 的**声明**。抽出来是为了让默认值能被断言（同 `evaluate.build_parser`）。"""
    import argparse

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Asuka 答案级评测（规则式必答要点召回）")
    parser.add_argument("topic", nargs="?", default="redis")
    parser.add_argument(
        "--answerer",
        default="oracle",
        choices=["oracle", "null", "fabricator"],
        help="三个都是**校准 / 自检**答案器（判据的上下界 + 编造探测器）。"
        "真模型作答已移到 AgentOS（Runtime 的 ModelGateway）；Asuka 不再直接调模型",
    )
    parser.add_argument("--retriever", default="bm25", choices=["bm25", "dense"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--context-budget",
        type=int,
        default=DEFAULT_CONTEXT_BUDGET,
        help="上下文窗口（token）。⚠️ 这是**输入**不是事实 —— 换个模型就换个数；"
        "它进报告，因为『丢了几片』脱离预算无法解释",
    )
    parser.add_argument(
        "--reserved-for-output",
        type=int,
        default=DEFAULT_RESERVED_FOR_OUTPUT,
        help="给模型输出留的位置。不留的话会出现『上下文刚好塞满、模型一个字都吐不出来』",
    )
    parser.add_argument(
        "--chars-per-token",
        type=int,
        default=CHARS_PER_TOKEN,
        help="每 token 几个字符。**语料的属性**，进报告 —— 4（英文）与 3（中文保守）差 33%",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="每题采样次数 k —— pass@k 的 k。`oracle`/`null` 是确定性的，采样只是让流程可验",
    )
    parser.add_argument(
        "--embedder",
        default="auto",
        choices=["auto", "api", "local", "hashing"],
        help="dense 检索用的 embedder；`auto` 需要 ASUKA_EMBED_API_KEY，没有就报错",
    )
    parser.add_argument("--model-path", default="")
    parser.add_argument(
        "--allow-non-semantic",
        action="store_true",
        help="⚠️ 只给冒烟用：放行由不承载语义的 embedder 建成的索引",
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

    # 同 `evaluate`：输入问题印一句话 + 退出码 2，不吐 traceback。
    from .dataset import DatasetError
    from .embedding import EmbeddingError
    from .vectorstore import VectorStoreError

    try:
        return _run(args)
    except (EmbeddingError, VectorStoreError, DatasetError, ValueError) as exc:
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

    answerer: Answerer = {
        "oracle": OracleAnswerer,
        "null": NullAnswerer,
        "fabricator": FabricatingAnswerer,
    }[args.answerer]()

    report = evaluate_answers(
        kb,
        ds,
        answerer,
        top_k=args.top_k,
        samples_per_task=args.samples,
        corpus_chunks=len(chunks),
        context_budget=args.context_budget,
        reserved_for_output=args.reserved_for_output,
        chars_per_token=args.chars_per_token,
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    (out_dir / "answers").mkdir(parents=True, exist_ok=True)
    base = f"{args.topic}-{args.answerer}-{args.retriever}-k{args.samples}-{stamp}"
    json_path = out_dir / "answers" / f"{base}.json"
    md_path = out_dir / "answers" / f"{base}.md"
    report.save(json_path)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    o = report.overall
    tag = "（校准）" if report.calibration else ""
    print(
        f"[{args.topic}/{args.answerer}{tag}] 检索 {args.retriever} top_k={args.top_k} "
        f"采样 {args.samples}  样本 {o.n_samples}"
    )
    print(
        f"  要点召回={o.mean_recall:.4f}  pass@1={o.pass_at_1:.4f}  "
        + (f"pass@{args.samples}={o.pass_at_k:.4f}  " if args.samples > 1 else "")
        + f"每要点字符={o.chars_per_hit_point:.1f}"
    )
    print(
        f"  耗时：检索 {o.retrieval_ms:.1f} ms + 生成 {o.latency_ms:.1f} ms "
        f"= {o.total_ms:.1f} ms"
    )
    print(
        f"  上下文：{o.context_tokens:.0f} / {report.context_available} tokens"
        f"（{o.context_size:.1f} 片 · 每 token {report.chars_per_token} 字符）"
        + (
            f"  ⚠️ 预算丢掉 {o.context_dropped_total} 片 / {o.samples_with_context_drops} 题"
            if o.context_dropped_total
            else ""
        )
    )
    if o.tasks_unscorable:
        print(f"  ⚠️ {o.tasks_unscorable} 道题没有必答要点声明 —— 不在分母里")
    if o.fabricated_total:
        print(
            f"  ⚠️ **编造引用** {o.fabricated_total} 条，涉及 {o.samples_with_fabrication} 题"
            " —— 引用了这次检索没给它的来源"
        )
    if o.n_without_citations:
        print(f"  ⚠️ {o.n_without_citations} 条样本没有自述引用 —— 不在引用分母里")
    print(
        f"  引用：grounded={_fmt_opt(o.grounded_rate)}  "
        f"依据召回={_fmt_opt(o.evidence_recall)}  "
        f"依据用上率={_fmt_opt(o.evidence_used_rate)}"
    )
    print(f"  → {md_path}")
    print(f"  → {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
