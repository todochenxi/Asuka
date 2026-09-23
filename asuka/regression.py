"""回归对比：**这次 vs 上次** —— 判"新退步"，不判"绝对通过率"。

--------------------------------------------------------------------------
为什么"和上一次比"比"绝对通过率"更有用

一条用例这次通过了，说明不了什么 —— 它可能一直都通过。
真正要报警的是**它上次通过、这次不通过了**。

判定规则**不是这里写的**，是 `packages.agent_evaluation.regression` 写的：

    regressed   上次通过 → 这次不通过     ← 唯一必须报警的一类
    improved    上次不通过 → 这次通过
    unchanged   都一样（含"两次都不通过"，那是**已知问题**，不是新退步）
    new         基线里没有这道题

⚠️ **"两次都不通过"归 `unchanged` 是刻意的**：一条一直失败的用例是 **backlog**，
不是回归。把它报成回归，回归信号会淹没在噪声里 ——
那正是"每轮 3 条红、其实 0 个新问题"这种疲惫感的来源。

所以本模块**不重写**这套规则，直接调它；只在外面加 Asuka 需要的两层：
**可比性这道门**，和**题级判定口径**。

--------------------------------------------------------------------------
一、题级口径：**通过 = 这道题的全部采样都通过**

`AnswerScore.passed` 是**一条样本**的判定；而回归对比是**一道题 vs 一道题**。
`samples_per_task > 1` 时两者不是一回事，所以口径必须写死、并印在报告上：

    每题通过 ⟺ 这道题 pass@1 == 1.0（全部采样都过）

为什么**不**用"至少一次通过"：那个口径下 `3/3 → 1/3` 会被读成 `unchanged` ——
一道正在塌的题被记成"没事"。

反过来，本口径下**一次采样翻转就会算成回归**，所以报告里**必须**同时印
前后的比例（`2/3 → 3/3` 与 `0/3 → 3/3` 是两回事），让读者自己判这是抖动还是塌了。
**判定给结论，比例给分辨力** —— 缺了后者，判定就是个会被误读的布尔值。

⚠️ 为什么**不**逐样本对：两次跑的第 2 次采样**不是同一个东西** ——
采样没有种子，`samples_per_task=3` 的两次运行之间没有可对齐的样本身份。
拿位置当身份，等于编一个不存在的对应关系。

--------------------------------------------------------------------------
二、不可测的题**不进对比**，而且必须点名

`points_total == 0` ⇒ `AnswerScore.passed` 恒为 `False`。直接拿去比的话，
一道**没测**的题会以"两次都不通过"的形状落进 `unchanged` ——
读起来是"已知问题"，真相是"压根没测"。那是本项目反复挡的那种谎。

⇒ 不可测的题被**排除**在对比之外，并在报告里单独点名。

--------------------------------------------------------------------------
三、可比性：配置变了，就不是"这次 vs 上次"

和 `asuka.compare` 同一条纪律 —— **不可比的对照表比没有对照表更糟**，
因为它看起来是结论。任何一项不一致都拒绝，并说清是**哪一项**：

    topic / retriever / answerer / top_k / samples_per_task
    context_budget / reserved_for_output / chars_per_token / corpus_chunks
    calibration（校准跑 vs 真跑）
    逐题：`question` 文本、`points_total` 条数

⚠️ `corpus_chunks` 只是个**计数**，不是指纹：语料重新切分后条数可能恰好不变，
而内容全变了。所以"它相等"**不等于**"语料同源"。
这一条是本模块**已知的**缺口 —— 写在报告里，不假装它被守住了。

--------------------------------------------------------------------------
四、`None`（不可测）不许印成 0

和 `answers._fmt_opt` 同一条规矩：不可测印 `—`。
漂移表里 `— → 0.5941` 读起来是"从 0 涨上来了"，而真相是"上次没测"。
两边都不可测时**连方向都不该判** —— 所以那一格是 `—`，不是"持平"。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from packages.agent_evaluation import Verdict
from packages.agent_evaluation.regression import Delta, compare, regressions

from .answers import AnswerReport, AnswerScore

# ---------------------------------------------------------------- 口径

#: 判定口径。**只有一种，不是旋钮** —— 旋钮会被调到来凑结论。
PASS_DEFINITION = "每题通过 = 这道题的全部采样都通过（pass@1 == 1.0）"

#: 归因词表。⚠️ 与 `packages.agent_evaluation` 的 `ATTR_*` **不是同一套**：
#: 那些说的是"Run 的轨迹卡在哪一步"，这里说的是"这道题的答案怎么了"。
#: 两套词共用 `Verdict.attribution` 这个字段，但**不能混读**。
ATTR_FLAKY = "flaky"                    # 采样之间有的过有的不过
ATTR_WRONG = "wrong_answer"             # 每次都答不到要点
ATTR_ERROR = "generation_error"         # 生成报错（不是答错）

#: 归因 → (人读的原因, 该动什么)。**这是归因，不是三个待办。**
_ATTR: dict[str, tuple[str, str]] = {
    ATTR_ERROR: ("生成**报错**（不是答错）", "先看 `err` —— 这是管线问题"),
    ATTR_FLAKY: ("采样之间**不稳定**（有的过有的不过）", "查温度 / 重试；别用 pass@1 读它"),
    ATTR_WRONG: ("**每次都**答不到要点", "改 prompt / 换模型 / 查喂进去的上下文够不够"),
}

#: 配置同一性。`(字段, 标签, 为什么它不一致就不可比)`。
#: 表驱动是为了**加字段时只改一行** —— 手写 if 链会静默漏掉新字段。
_IDENTITY: tuple[tuple[str, str, str], ...] = (
    ("topic", "语料 / 题目集", "不同的语料不可比"),
    ("retriever", "检索器", "换了检索器 ⇒ 变的是被测系统，不是它退步了"),
    ("answerer", "答案器", "换了答案器 ⇒ 同上；『模型 A 换模型 B』不是回归"),
    ("top_k", "top_k", "改它等于改检索范围，两次看到的上下文不是同一批"),
    ("samples_per_task", "每题采样数", "判定口径依赖它（全部采样通过），换了口径就换了结论"),
    ("context_budget", "上下文窗口", "窗口变了 ⇒ 喂进 prompt 的那一份变了"),
    ("reserved_for_output", "留给输出的位置", "它和窗口一起决定能放多少上下文"),
    ("chars_per_token", "每 token 字符数", "它是**语料的属性**，变了说明语料或假设变了"),
    ("corpus_chunks", "语料片数", "语料变了 ⇒ 检索的宇宙变了（⚠️ 只是个计数，见模块 docstring）"),
    ("calibration", "校准 / 真跑", "拿判据校准和模型成绩比，比出来的差是指标的差"),
)

#: `(字段, 标签, 方向, 小数位)`。方向只是**附注** ——
#: 指标该往哪边走是领域知识，"变了多少"是事实。混在一起会把附注读成判据。
_METRICS: tuple[tuple[str, str, str, int], ...] = (
    ("mean_recall", "要点召回（mean）", "↑好", 4),
    ("pass_at_1", "pass@1", "↑好", 4),
    ("pass_at_k", "pass@k", "↑好", 4),
    ("evidence_recall", "依据召回（端到端）", "↑好", 4),
    ("evidence_used_rate", "依据用上率（纯生成侧）", "↑好", 4),
    ("grounded_rate", "引用有依据率", "↑好", 4),
    ("fabricated_total", "编造引用（条）", "↓好", 0),
    ("retrieval_ms", "检索耗时（mean ms）", "↓好", 1),
    ("latency_ms", "生成耗时（mean ms）", "↓好", 1),
    ("total_tokens", "总 token", "↓好", 0),
    ("context_tokens", "上下文 token（mean）", "↓好", 1),
    ("chars_per_hit_point", "每答到一个要点的字符数", "↓好", 1),
)


# ---------------------------------------------------------------- 题级判定


def _by_task(scores: Sequence[AnswerScore]) -> dict[str, list[AnswerScore]]:
    out: dict[str, list[AnswerScore]] = {}
    for s in scores:
        out.setdefault(s.task_id, []).append(s)
    return out


def _why(rows: Sequence[AnswerScore]) -> str:
    """这道题**为什么**不算通过。优先级：报错 > 不稳定 > 答不到。

    报错排第一，因为它是**管线**问题（该去看 `err`），
    而不是"答得不够好"（该去改 prompt）—— 两者该动的地方完全不同。
    """
    if any(r.error for r in rows):
        return ATTR_ERROR
    if any(r.passed for r in rows):
        return ATTR_FLAKY
    return ATTR_WRONG


def task_verdicts(report: AnswerReport) -> list[Verdict]:
    """每题一条 `Verdict` —— **不可测的题不进这里**（见模块 docstring 二）。

    一次跑里同一道题会有 `samples_per_task` 条样本，这里把它们**收成一条**。
    收法是本模块的核心决定，写在 `PASS_DEFINITION` 里。
    """
    out: list[Verdict] = []
    for tid, rows in _by_task(report.items).items():
        scorable = [r for r in rows if r.points_total > 0]
        if not scorable:
            continue
        passed = all(r.passed for r in scorable)
        out.append(Verdict(tid, passed, "" if passed else _why(scorable)))
    return out


def baseline(report: AnswerReport) -> dict[str, bool]:
    """`case_id → passed`，**从 `task_verdicts` 推出来**。

    ⚠️ 不许各算一份：那样"上次过没过"和"这次过没过"会用两套口径，
    而 `compare()` 是把这两个映射对减的 —— 口径不同时，对减出来的差
    **全是口径的差**，报告里没有任何东西会提醒你。
    """
    return {v.case_id: v.passed for v in task_verdicts(report)}


def deltas(before: AnswerReport, after: AnswerReport) -> list[Delta]:
    """这次 vs 上次的逐题分类。规则住在 `agent_evaluation.regression`。"""
    return compare(baseline(before), task_verdicts(after))


def unmeasurable(report: AnswerReport) -> list[str]:
    """**没测**的题（`points_total == 0`）—— 它们不在对比里，必须点名。"""
    return sorted(
        tid for tid, rows in _by_task(report.items).items()
        if all(r.points_total <= 0 for r in rows)
    )


def only_in_before(before: AnswerReport, after: AnswerReport) -> list[str]:
    """上次有、这次没有的题。

    ⚠️ `compare()` **只遍历这次的结果**，所以这些题**不会**产生 `Delta` ——
    它们会静默消失。不是回归，但"题库少了 3 道"是必须被看见的。
    """
    return sorted(set(_by_task(before.items)) - set(_by_task(after.items)))


def count_kinds(ds: Sequence[Delta]) -> dict[str, int]:
    counts = {"regressed": 0, "improved": 0, "unchanged": 0, "new": 0}
    for d in ds:
        counts[d.kind] = counts.get(d.kind, 0) + 1
    return counts


# ---------------------------------------------------------------- 可比性


def _first_by_task(items: Sequence[AnswerScore]) -> dict[str, AnswerScore]:
    out: dict[str, AnswerScore] = {}
    for s in items:
        out.setdefault(s.task_id, s)
    return out


def check_comparable(before: AnswerReport, after: AnswerReport) -> list[str]:
    """返回**不可比的原因**（空列表 = 可比）。**一次报全部**，不只报第一个。

    只报第一个的话，读者改完一项再跑一次、又冒出一项 ——
    那会把人训练成"多跑几次"，而不是"看一次报告"。
    """
    problems: list[str] = []

    for field, label, why in _IDENTITY:
        a, b = getattr(before, field), getattr(after, field)
        if a != b:
            problems.append(
                f"{label}（`{field}`）不一致：之前 {a!r}，之后 {b!r} —— {why}"
            )

    # 逐题：题号相同但**题本身变了** —— 那是在比两道不同的题。
    # ⚠️ 这是 `compare()` 看不见的一类：它只比 `case_id` 和 `passed`，
    # 同一个题号下换了问句，它会照样给出"regressed"，而那毫无意义。
    b_items = _first_by_task(before.items)
    a_items = _first_by_task(after.items)
    rewritten: list[str] = []
    points_changed: list[str] = []
    for tid in sorted(set(b_items) & set(a_items)):
        if b_items[tid].question != a_items[tid].question:
            rewritten.append(tid)
        if b_items[tid].points_total != a_items[tid].points_total:
            points_changed.append(tid)
    if rewritten:
        problems.append(
            f"这些题的**问句**被改过：{rewritten} —— 同一个题号下是两道不同的题，"
            "『上次过没过』对不上"
        )
    if points_changed:
        problems.append(
            f"这些题的**必答要点条数**变了：{points_changed} —— "
            "『通过』的含义跟着变了（原来是全中这一组，现在是全中另一组）"
        )
    return problems


def looks_reversed(before: AnswerReport, after: AnswerReport) -> bool:
    """`after` 的生成时间早于 `before` ⇒ 很可能两个参数传反了。

    比的是**前 19 个字符**（本地时间 `YYYY-MM-DDTHH:MM:SS`）。
    ⚠️ 跨时区比会错 —— 但两次跑在同一台机器上，所以够用。
    宁可说清这个前提，也不要为了"更严谨"去解析时区（`%z` 在两次跑里
    本来就一样，多解析一层只会多一层能崩的地方）。
    """
    a, b = before.generated_at[:19], after.generated_at[:19]
    return bool(a) and bool(b) and b < a


# ---------------------------------------------------------------- 渲染


@dataclass(frozen=True)
class TaskView:
    """一道题在**一次跑**里的样子（渲染要用的那几个数）。"""

    passed: int
    total: int
    missed: tuple[str, ...] = ()
    errored: bool = False

    @property
    def ratio(self) -> str:
        return f"{self.passed}/{self.total}"


def _view(report: AnswerReport, tid: str) -> TaskView:
    rows = [r for r in report.items if r.task_id == tid]
    return TaskView(
        passed=sum(1 for r in rows if r.passed),
        total=len(rows),
        missed=tuple(dict.fromkeys(lbl for r in rows for lbl in r.missed)),
        errored=any(r.error for r in rows),
    )


def _num(v: Any, digits: int) -> str:
    """`None` = **不可测**，印 `—`。

    ⚠️ 印 0 会把"没测"说成"测了，结果是 0" ——
    漂移表里 `— → 0.5941` 读起来是"从 0 涨上来了"。
    """
    if v is None:
        return "—"
    if digits == 0:
        return str(int(v))
    return f"{float(v):.{digits}f}"


def render_markdown(
    before: AnswerReport,
    after: AnswerReport,
    *,
    before_path: str = "",
    after_path: str = "",
) -> str:
    """人读的回归报告。不可比时**一个指标数字都不印**（和 `compare` 同规矩）。"""
    lines: list[str] = ["# Asuka 回归对比", ""]
    lines.append(f"- **之前**：{('`' + before_path + '`') if before_path else '—'}"
                 f"（生成于 {before.generated_at or '?'}）")
    lines.append(f"- **之后**：{('`' + after_path + '`') if after_path else '—'}"
                 f"（生成于 {after.generated_at or '?'}）")
    lines.append(
        f"- 配置：answerer `{after.answerer}` · 检索器 `{after.retriever}`"
        f" · top_k={after.top_k} · 每题 {after.samples_per_task} 次采样"
        f" · 窗口 {after.context_budget}（可放 {after.context_available}）"
    )
    lines.append(f"- **判定口径**：{PASS_DEFINITION}")
    lines.append("")

    problems = check_comparable(before, after)
    if problems:
        lines.append("## ⛔ 不可比 —— 拒绝出对比")
        lines.append("")
        for p in problems:
            lines.append(f"- {p}")
        lines.append("")
        lines.append(
            "> 不给任何对比数字。**一个不可比的对照表比没有对照表更糟** —— "
            "它看起来是结论。"
        )
        return "\n".join(lines) + "\n"

    if looks_reversed(before, after):
        lines.append(
            "> ⚠️ **这份『之后』的报告生成时间早于『之前』的** —— "
            "你是不是把两个参数传反了？下面所有 `regressed` / `improved` 的方向都会反过来。"
        )
        lines.append("")

    ds = deltas(before, after)
    counts = count_kinds(ds)
    gone = only_in_before(before, after)
    blind = unmeasurable(after)

    lines.append("## 总览")
    lines.append("")
    lines.append("| 类别 | 题数 |")
    lines.append("|---|---|")
    lines.append(f"| **regressed**（上次过 → 这次不过） | **{counts['regressed']}** |")
    lines.append(f"| improved（上次不过 → 这次过） | {counts['improved']} |")
    lines.append(f"| unchanged（含**两次都不过**的 backlog） | {counts['unchanged']} |")
    lines.append(f"| new（基线里没有这题） | {counts['new']} |")
    lines.append(f"| 只在基线里（这次没跑） | {len(gone)} |")
    lines.append(f"| 不可测（没声明必答要点） | {len(blind)} |")
    lines.append("")
    lines.append(
        f"> **`regressed` 是唯一需要报警的一类。** `unchanged` 里混着两种东西 ——"
        f"**两次都过**和**两次都不过**（那是 backlog）。"
        f"把它们分开看的是下面各节，不是这一行数字。"
    )
    lines.append("")

    # ---- 回归：唯一需要报警的一类，放最前面
    reg = regressions(ds)
    lines.append(f"## ⚠️ 回归（{len(reg)}）")
    lines.append("")
    if not reg:
        lines.append("**没有回归。** 上次通过、这次不通过的题：一道都没有。")
        lines.append("")
        lines.append(
            "> 这句话**不等于**『一切都好』：下面那些 `unchanged` 里可能有一堆"
            "**一直不通过**的题（backlog），`new` 里可能有还没跑熟的题。"
        )
    else:
        lines.append("| task | 之前 | 之后 | 原因 | 缺的要点 | 该动什么 |")
        lines.append("|---|---|---|---|---|---|")
        for d in reg:
            b = _view(before, d.case_id)
            a = _view(after, d.case_id)
            why, fix = _ATTR.get(d.attribution, (d.attribution or "?", "—"))
            miss = "、".join(a.missed[:3]) or "—"
            lines.append(
                f"| `{d.case_id}` | {b.ratio} | {a.ratio} | {why} | {miss} | {fix} |"
            )
        lines.append("")
        lines.append(
            "> ⚠️ 看**前后比例**再下结论：`2/3 → 3/3` 和 `0/3 → 3/3` 都被算成"
            "`regressed`（口径是『全部采样都过』），但一个是抖动、一个是塌了。"
        )
    lines.append("")

    # ---- 改善
    imp = [d for d in ds if d.kind == "improved"]
    lines.append(f"## 改善（{len(imp)}）")
    lines.append("")
    if imp:
        for d in imp:
            lines.append(f"- `{d.case_id}`：{_view(before, d.case_id).ratio} → {_view(after, d.case_id).ratio}")
    else:
        lines.append("没有。")
    lines.append("")

    # ---- backlog：两次都不过。**必须与回归分开说**
    stuck = [d for d in ds if d.kind == "unchanged" and not d.before]
    lines.append(f"## 已知问题（{len(stuck)}）—— **两次都不通过，是 backlog，不是这次退步**")
    lines.append("")
    if stuck:
        for d in stuck:
            a = _view(after, d.case_id)
            why, _ = _ATTR.get(d.attribution, (d.attribution or "?", ""))
            lines.append(f"- `{d.case_id}`（{a.ratio}）—— {why}")
        lines.append("")
        lines.append(
            "> 这一节**刻意不叫『回归』**：一条一直失败的用例是 backlog。"
            "把它报成回归，回归信号会淹没在噪声里 —— "
            "那正是『每轮 3 条红、其实 0 个新问题』这种疲惫感的来源。"
        )
    else:
        lines.append("没有。")
    lines.append("")

    # ---- new
    new = [d for d in ds if d.kind == "new"]
    if new:
        lines.append(f"## 新增的题（{len(new)}）—— 基线里没有，**不算退步**")
        lines.append("")
        for d in new:
            a = _view(after, d.case_id)
            lines.append(f"- `{d.case_id}`（{a.ratio}）")
        lines.append("")

    # ---- 只在基线里：`compare()` 看不见的那一批
    if gone:
        lines.append(f"## 只在基线里的题（{len(gone)}）—— 这次没跑")
        lines.append("")
        lines.append(f"{gone}")
        lines.append("")
        lines.append(
            "> 它们**不产生** `regressed` / `improved`（`compare()` 只遍历这次的结果），"
            "所以必须单独点名 —— 否则『题库少了几道』会静默消失。"
        )
        lines.append("")

    # ---- 不可测：不进任何分母
    if blind:
        lines.append(f"## 不可测的题（{len(blind)}）—— 没声明必答要点")
        lines.append("")
        lines.append(f"{blind}")
        lines.append("")
        lines.append(
            "> 它们**不在对比里**。直接拿去比的话，一道**没测**的题会以"
            "『两次都不通过』的形状落进 `unchanged` —— 读起来是『已知问题』，"
            "真相是『压根没测』。"
        )
        lines.append("")

    # ---- 指标漂移：判定之外的另一个维度
    lines.append("## 指标漂移")
    lines.append("")
    lines.append("| 指标 | 方向 | 之前 | 之后 |")
    lines.append("|---|---|---|---|")
    b_o, a_o = before.overall, after.overall
    for field, label, direction, digits in _METRICS:
        if field == "pass_at_k" and before.samples_per_task <= 1:
            # `samples == 1` 时它和 pass@1 是同一个数 —— 印两行同名指标只会让人以为看错了。
            continue
        lines.append(
            f"| {label} | {direction} | "
            f"{_num(getattr(b_o, field), digits)} | {_num(getattr(a_o, field), digits)} |"
        )
    lines.append("")
    if b_o.total_cost_usd or a_o.total_cost_usd:
        lines.append(
            f"| 总成本 USD | ↓好 | ${b_o.total_cost_usd:.4f} | ${a_o.total_cost_usd:.4f} |"
        )
        lines.append("")
    elif not after.calibration:
        lines.append(
            "⚠️ 总成本**两边都没测**（都是 0）—— **『没测』不等于『免费』**，"
            "所以这一行不印，而不是印两个 0 让你读成『没花钱』。"
        )
        lines.append("")
    lines.append(
        "> ⚠️ **判定（过/不过）与度量（分数多少）是两个维度。** 上面这张表里的数字"
        "**不参与** `regressed` / `improved` 的判定 —— 一道题可以两次都通过，"
        "而它的 `依据召回` 掉了。那种『分数低了一点』正是最容易被放过去的一类。"
    )
    lines.append(
        "> ⚠️ 方向列只是**附注**：指标该往哪边走是领域知识，『变了多少』是事实。"
        "把两者混成一列，附注会被读成判据。"
    )
    lines.append("")

    return "\n".join(lines) + "\n"


def as_dict(before: AnswerReport, after: AnswerReport) -> dict[str, Any]:
    """机器可读的那一份。**归因必须和 markdown 里一致** ——
    下游只拿 JSON 的时候，少一层归因就等于少一个该动的地方。"""
    problems = check_comparable(before, after)
    out: dict[str, Any] = {
        "comparable": not problems,
        "problems": problems,
        "definition": PASS_DEFINITION,
        "reversed_suspect": looks_reversed(before, after) if not problems else False,
        "before": {"path": "", "generated_at": before.generated_at,
                   "answerer": before.answerer, "retriever": before.retriever},
        "after": {"path": "", "generated_at": after.generated_at,
                  "answerer": after.answerer, "retriever": after.retriever},
    }
    if problems:
        return out

    ds = deltas(before, after)
    out["counts"] = count_kinds(ds)
    out["deltas"] = [d.as_dict() for d in ds]
    out["regressions"] = [d.case_id for d in regressions(ds)]
    out["only_in_before"] = only_in_before(before, after)
    out["unmeasurable"] = unmeasurable(after)
    out["metrics"] = {
        field: {
            "before": getattr(before.overall, field),
            "after": getattr(after.overall, field),
        }
        for field, _label, _dir, _d in _METRICS
    }
    return out


# ---------------------------------------------------------------- CLI


def _write_outputs(
    args: Any, before: AnswerReport, after: AnswerReport, md: str, *, stream: Any
) -> None:
    """把 markdown / JSON 落盘。**拒绝时也写**。

    ⚠️ 这一点和 `asuka.compare` **不同**（那边拒绝时只在 stdout 吐一段）。
    理由：`--out` 是**显式给的** flag，静默忽略它属于"承诺了却没交付"；
    而且"这次为什么不能比"本身就是该留档的结论。
    拒绝时 `→ 路径` 印到 **stderr** —— stdout 保持"一个数字都不给"。
    """
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"  → {args.out}", file=stream)
    if args.json:
        payload = as_dict(before, after)
        payload["before"]["path"] = args.before
        payload["after"]["path"] = args.after
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  → {args.json}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="回归对比：这次 vs 上次（先验可比性）"
    )
    parser.add_argument("before", help="**上次**的答案报告 JSON")
    parser.add_argument("after", help="**这次**的答案报告 JSON")
    parser.add_argument("--out", default="", help="把 markdown 写到这个文件")
    parser.add_argument("--json", default="", help="把机器可读结果写到这个文件")
    parser.add_argument(
        "--allow-non-comparable",
        action="store_true",
        help="⚠️ 不可比时也出报告（退出码仍是 2）—— 只给排查用，别拿它做结论",
    )
    args = parser.parse_args(argv)

    try:
        before = AnswerReport.load(Path(args.before))
        after = AnswerReport.load(Path(args.after))
    except (ValueError, KeyError, OSError) as exc:
        # 读不回来是**输入问题**，不是程序缺陷 —— 印一句话，不吐 traceback。
        # （旧格式报告缺 `citation` / `context_tokens`，`load()` 会在这里拒绝，
        #   它的消息已经说清了"按空读会得到什么假结论"。）
        print(f"\n! 报告读不回来：{exc}", file=sys.stderr)
        return 3

    md = render_markdown(
        before, after, before_path=Path(args.before).name, after_path=Path(args.after).name
    )
    problems = check_comparable(before, after)

    if problems and not args.allow_non_comparable:
        print(md, file=sys.stderr)
        _write_outputs(args, before, after, md, stream=sys.stderr)
        return 2

    print(md)
    _write_outputs(args, before, after, md, stream=sys.stdout)
    return 2 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
