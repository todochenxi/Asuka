"""实验环：多配置对照 + **噪声门槛** → 三态结论（M121）。

--------------------------------------------------------------------------
它补的是什么

`compare.py`（并排对照）和 `regression.py`（这次 vs 上次）都只回答
"**同一配置**下两个数谁大"。它们**没有**回答一个更基本的问题：

    这个差，是**系统变了**，还是**这次抽签抽得好**？

本项目反复栽在这一条上（MEMORY 里记着：同一配置两次 bm25-s3 的要点召回
0.2132 vs 0.1889，差 0.0243；而 prompt v1→v2 的差是 0.0289 —— **同量级**）。
于是"改了有没有用"永远停在争论里。

这一层把"运行间噪声"变成一个**可执行的判据**：

    差值 > 噪声门槛   ⇒ 更好了 / 更差了
    差值 ≤ 噪声门槛   ⇒ **分辨不出**

第三个结论是它最值钱的地方：它敢说"这个测量分辨不出这么小的差"，
而不是硬给个方向让人在错的地方继续投入。

--------------------------------------------------------------------------
噪声门槛从哪来（不许拍脑袋）

`--noise <σ>`：**同一配置、重复跑**观测到的波动，由调用方给出并**记进报告**。
它必须是实测的，不是默认常量 —— 一个硬编码的"0.02"会在换语料/换模型时
悄悄失效，而结论照样印得像真的。

    · 怎么量：同配置同采样数跑两遍（或从既有同名基线里取），
      取要点的 mean_recall 之差；`--samples 3` 时本项目实测约 **0.033**。
    · 门槛 `None` ⇒ **只印差值，不下结论**（并点名"没有噪声门槛，拒绝判定"）。
      这比拿一个没依据的门槛下结论诚实。

--------------------------------------------------------------------------
三条硬规矩

R-1 **可比性先过门**：只有"除被测旋钮外其余都相同"的配置才进同一张表。
    走 `compare.check_comparable` 的同一条纪律（不可比的对照表比没有更糟）。
    允许每组之间**只有**一个维度不同（`--vary` 声明是哪个）。
R-2 **三态、且必须有"分辨不出"**：`better / worse / indistinguishable`。
    没有第三态的实验环，等于把所有噪声都当结论。
R-3 **采样数与时序都不许混**：`samples_per_task` 不一致直接拒绝
    （s1 与 s3 的 pass@1 不是一回事，本项目吃过这个亏）。

它**不**输出"最优配置"。它输出一张带噪声门槛的对照表，让人（或外层调度器）
自己决定。理由同 `compare.py`：尺子不许替被测物做决定。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .answers import AnswerReport

#: 三态。**刻意没有**第四态、也没有默认态 —— 每个差值都必须落进一个。
BETTER = "better"
WORSE = "worse"
INDISTINGUISHABLE = "indistinguishable"

#: 参与比较的指标（顺序即渲染顺序）。全部取自 `AnswerGroup`。
#: ⚠️ pass@k 是**布尔派生量**，采样数少时抖动极大 —— 但它照样要列，
#: 只是它的差通常落进"分辨不出"。
_METRICS: tuple[tuple[str, str], ...] = (
    ("mean_recall", "要点召回"),
    ("pass_at_1", "pass@1"),
    ("pass_at_k", "pass@K（全部要点都答对）"),
    ("evidence_recall", "依据召回"),
    ("evidence_used_rate", "依据用上率"),
)


class ExperimentError(Exception):
    """实验定义本身不成立（可比性过不去 / 没有基线 / 噪声门槛没给却要判定）。"""


@dataclass(frozen=True)
class Arm:
    """实验里的一"臂"：一个配置 + 它的报告。"""

    label: str
    report: AnswerReport

    def metric(self, key: str) -> float | None:
        value = getattr(self.report.overall, key, None)
        return None if value is None else float(value)


@dataclass(frozen=True)
class Verdict:
    """一个指标上、某一臂 vs 基线 的结论。"""

    metric: str
    label: str
    baseline_value: float | None
    value: float | None
    delta: float | None
    conclusion: str  # better / worse / indistinguishable / unmeasurable

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "arm": self.label,
            "baseline": self.baseline_value,
            "value": self.value,
            "delta": self.delta,
            "conclusion": self.conclusion,
        }


# ---------------------------------------------------------------------------
# 可比性
# ---------------------------------------------------------------------------

#: 两臂之间**允许不同**的字段之外，其余必须逐项相同。
#: 注意 `prompt_id` 也在内：换 prompt 是**被测系统变了**，
#: 但它是本轮实验常测的旋钮之一，所以由 `vary` 决定它能不能不同。
_COMPARABLE_FIELDS: tuple[str, ...] = (
    "topic",
    "answerer",
    "retriever",
    "top_k",
    "samples_per_task",
    "corpus_chunks",
    "calibration",
    "context_budget",
    "reserved_for_output",
    "chars_per_token",
    "prompt_id",
)


def check_comparable(arms: Sequence[Arm], *, vary: str) -> list[str]:
    """返回不一致清单（空 = 可并排）。`vary` 是**唯一**允许不同的字段。"""
    if len(arms) < 2:
        raise ExperimentError("至少要两臂才能做实验")
    if vary not in _COMPARABLE_FIELDS:
        raise ExperimentError(
            f"--vary {vary!r} 不是可变的可比字段；可选项：{sorted(_COMPARABLE_FIELDS)}"
        )
    problems: list[str] = []
    for field in _COMPARABLE_FIELDS:
        if field == vary:
            continue
        values = {str(getattr(a.report, field)) for a in arms}
        if len(values) > 1:
            shown = ", ".join(f"{a.label}={getattr(a.report, field)}" for a in arms)
            problems.append(f"{field} 不一致：{shown}")
    # 逐题 identity：题集不同 → 不是同一个实验
    qsets = {tuple(i.task_id for i in a.report.items) for a in arms}
    if len(qsets) > 1:
        problems.append("题集不同（task_id 序列不一致）—— 不是同一个实验")
    return problems


# ---------------------------------------------------------------------------
# 三态判定
# ---------------------------------------------------------------------------


def classify(delta: float, noise: float) -> str:
    """R-2：差值与噪声门槛比 —— 严格大才算数（等于门槛算"分辨不出"）。"""
    if noise < 0:
        raise ExperimentError("噪声门槛不能为负")
    if abs(delta) > noise:
        return BETTER if delta > 0 else WORSE
    return INDISTINGUISHABLE


def verdicts(
    arms: Sequence[Arm], baseline_label: str, *, noise: float | None, vary: str
) -> tuple[Verdict, ...]:
    """对每个指标、每一臂算出结论。`noise is None` ⇒ 只印差值（unmeasurable）。"""
    by_label = {a.label: a for a in arms}
    if baseline_label not in by_label:
        raise ExperimentError(f"基线 {baseline_label!r} 不在实验臂里：{sorted(by_label)}")
    base = by_label[baseline_label]

    out: list[Verdict] = []
    for key, _cn in _METRICS:
        b = base.metric(key)
        for arm in arms:
            if arm.label == baseline_label:
                continue
            v = arm.metric(key)
            if b is None or v is None:
                # 缺数不许当成 0（同 answers._fmt_opt 的规矩）
                out.append(Verdict(key, arm.label, b, v, None, "unmeasurable"))
                continue
            delta = v - b
            # 没有噪声门槛 ⇒ 只印差值，**不下结论**（R-2 的另一半：
            # 拿一个没依据的门槛下结论，比不下结论更糟）。
            conclusion = "unmeasurable" if noise is None else classify(delta, noise)
            out.append(Verdict(key, arm.label, b, v, delta, conclusion))
    return tuple(out)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _fmt_delta(delta: float | None) -> str:
    return "—" if delta is None else f"{delta:+.4f}"


_CONCLUSION_CN = {
    BETTER: "更好",
    WORSE: "更差",
    INDISTINGUISHABLE: "**分辨不出**",
    "unmeasurable": "无法判定（缺噪声门槛 / 缺数）",
}


def render_markdown(
    arms: Sequence[Arm], vs: Sequence[Verdict], *, baseline_label: str, noise: float | None
) -> str:
    lines: list[str] = ["# Asuka 实验对照", ""]
    base = next(a for a in arms if a.label == baseline_label)
    lines.append(f"- 基线：`{baseline_label}`（{base.report.topic} / {base.report.retriever} / "
                 f"top_k={base.report.top_k} / s{base.report.samples_per_task}）")
    lines.append(f"- 噪声门槛：{'**未提供 —— 只印差值，不下结论**' if noise is None else f'{noise:.4f}（实测，同配置重复跑）'}")
    lines.append("- 目标：**只印差值 + 三态结论，绝不输出「最优配置」**")
    lines.append("")
    lines.append("> 差值 ≤ 噪声门槛 ⇒ **分辨不出**。这不是「没差别」，是「这个测量分辨不出这么小的差」。")
    lines.append("")

    by_metric: dict[str, list[Verdict]] = {}
    for v in vs:
        by_metric.setdefault(v.metric, []).append(v)

    for key, cn in _METRICS:
        rows = by_metric.get(key, [])
        if not rows:
            continue
        lines.append(f"## {cn}")
        lines.append("")
        lines.append("| 臂 | 值 | 差值 | 结论 |")
        lines.append("|---|---|---|---|")
        for v in rows:
            lines.append(
                f"| {v.label} | {_fmt(v.value)} | {_fmt_delta(v.delta)} | "
                f"{_CONCLUSION_CN.get(v.conclusion, v.conclusion)} |"
            )
        lines.append("")
    return "\n".join(lines)


def as_dict(
    arms: Sequence[Arm], vs: Sequence[Verdict], *, baseline_label: str, noise: float | None
) -> dict[str, Any]:
    return {
        "baseline": baseline_label,
        "noise": noise,
        "arms": [
            {"label": a.label, "retriever": a.report.retriever, "top_k": a.report.top_k,
             "prompt_id": a.report.prompt_id, "samples_per_task": a.report.samples_per_task}
            for a in arms
        ],
        "verdicts": [v.as_dict() for v in vs],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_arm(label: str, path: Path) -> Arm:
    return Arm(label=label, report=AnswerReport.load(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Asuka 实验环：多配置对照 + 噪声门槛 → 三态结论"
    )
    parser.add_argument(
        "arms",
        nargs="+",
        help="`label=报告.json`，第一个是基线（如 base=runs/answers/a.json)",
    )
    parser.add_argument("--vary", required=True, help="允许不同的那**一个**维度（如 retriever / top_k / prompt_id）")
    parser.add_argument("--noise", type=float, default=None, help="噪声门槛（实测）；不给则只印差值")
    parser.add_argument("--out", default="", help="markdown 输出路径")
    parser.add_argument("--json", default="", help="json 输出路径")
    return parser


def _parse_arms(raw: Sequence[str]) -> list[Arm]:
    arms: list[Arm] = []
    for item in raw:
        label, sep, path = item.partition("=")
        if not sep or not label or not path:
            raise ExperimentError(f"臂的写法应是 `label=path.json`，得到 {item!r}")
        p = Path(path)
        if not p.exists():
            raise ExperimentError(f"报告不存在：{p}")
        arms.append(load_arm(label, p))
    return arms


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)
    try:
        arms = _parse_arms(args.arms)
        if args.noise is not None and args.noise < 0:
            raise ExperimentError("--noise 不能为负")
        problems = check_comparable(arms, vary=args.vary)
        if problems:
            print("! 这几臂不可并排（除 %s 外应逐项相同）：" % args.vary)
            for p in problems:
                print(f"    - {p}")
            return 2
        baseline_label = arms[0].label
        vs = verdicts(arms, baseline_label, noise=args.noise, vary=args.vary)
    except ExperimentError as exc:
        print(f"! {exc}")
        return 2

    markdown = render_markdown(arms, vs, baseline_label=baseline_label, noise=args.noise)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"→ {args.out}")
    else:
        print(markdown)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(as_dict(arms, vs, baseline_label=baseline_label, noise=args.noise),
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


__all__ = [
    "BETTER",
    "INDISTINGUISHABLE",
    "WORSE",
    "Arm",
    "ExperimentError",
    "Verdict",
    "as_dict",
    "check_comparable",
    "classify",
    "load_arm",
    "main",
    "render_markdown",
    "verdicts",
]


if __name__ == "__main__":
    raise SystemExit(main())
