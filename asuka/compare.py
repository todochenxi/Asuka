"""把多份检索报告并排对照 —— 但**先验可比性**，不可比就拒绝。

--------------------------------------------------------------------------
为什么单份报告的数字不能直接横向比

**一、`top_k` 不同 ⇒ recall 的**上限**不同**

    recall 上限 = mean( min(|evidence|, top_k) / |evidence| )

拿 `top_k=5` 的 dense 和 `top_k=10` 的 bm25 并排，读者会以为 dense 更差 ——
实际可能只是**它的上限更低**。上限不同时，"谁分高"这个问题没有意义。

**二、题目集合不同 ⇒ 分母不同**

均值是"**对这组题**求的"。换一组题就不可比，哪怕题目数量一样。

**三、同一道题的 evidence 不同 ⇒ ground truth 变了**

那不是在比两个检索器，是在比两套标注。

⇒ 所以这里**先断言**这三件事，不一致就拒绝并说清是哪一件。
这是"宁可拒绝，不许编造"在对照场景的落地：**一个不可比的对照表比没有对照表更糟**，
因为它看起来是结论。

--------------------------------------------------------------------------
上限归一化：`recall / ceiling`

上限不同的报告之间，唯一还能谈的量是 `recall / 上限`（1.0 = 已到上限）。
但它**只在上限之内可比**，跨上限比较仍然是错的 —— 所以本模块在
`top_k` 不一致时**直接拒绝**，不会给你一个"归一化后可比"的假象。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .evaluate import RetrievalReport

#: 比对的指标：(字段名, 中文标签, 越大越好)
_METRICS: tuple[tuple[str, str], ...] = (
    ("recall", "context_recall（上限内）"),
    ("recall_ceiling", "context_recall 上限"),
    ("precision", "context_precision"),
    ("hit_rate", "hit_rate（至少命中一处）"),
    ("mrr", "MRR"),
    ("latency_ms", "平均耗时 ms"),
)


def load_reports(paths: Sequence[Path]) -> list[RetrievalReport]:
    return [RetrievalReport.load(Path(p)) for p in paths]


def check_comparable(reports: Sequence[RetrievalReport]) -> list[str]:
    """返回**不可比的原因**（空列表 = 可比）。一次报全部。"""
    problems: list[str] = []
    if len(reports) < 2:
        problems.append("少于两份报告，没什么可对照的")
        return problems

    topics = {r.topic for r in reports}
    if len(topics) > 1:
        problems.append(f"topic 不一致：{sorted(topics)} —— 不同的语料不可比")

    # 一边是冒烟、一边是真跑 —— 那不是对照，是拿噪声当基线。
    smokes = [r.retriever for r in reports if not r.embedder_semantic]
    if smokes:
        problems.append(
            f"这些 run 的 embedder **不承载语义**（冒烟）：{smokes} —— "
            "它的分数是噪声，不能进对照表。重建索引时用真 embedder。"
        )

    top_ks = {r.top_k for r in reports}
    if len(top_ks) > 1:
        problems.append(
            f"top_k 不一致：{sorted(top_ks)} —— "
            "recall 的**上限**依赖 top_k（min(|evidence|, top_k)/|evidence|），"
            "上限不同的分数并排会被误读成能力差"
        )

    # 逐题比：题目集合 与 每题的 evidence 都必须逐字一致
    first = reports[0]
    base = {i.task_id: tuple(i.evidence) for i in first.items}
    for r in reports[1:]:
        other = {i.task_id: tuple(i.evidence) for i in r.items}
        only_a = sorted(set(base) - set(other))
        only_b = sorted(set(other) - set(base))
        if only_a or only_b:
            problems.append(
                f"{first.retriever} 与 {r.retriever} 题目集合不同："
                f"只有前者有 {only_a[:5]}，只有后者有 {only_b[:5]}"
            )
            continue
        mismatched = sorted(k for k in base if base[k] != other[k])
        if mismatched:
            problems.append(
                f"{first.retriever} 与 {r.retriever} 在 {len(mismatched)} 道题上 "
                f"evidence 不同（如 {mismatched[:3]}）—— ground truth 变了"
            )
    return problems


def _labels(reports: Sequence[RetrievalReport]) -> list[str]:
    """列名必须**唯一**。

    两次 bm25（改前 / 改后）跑出两列同名，读者分不清哪列是哪次 ——
    而"改了什么导致分数变了"恰恰是对照的全部意义。

    唯一性由**序号**保证，时间戳只是附注：同一秒内的两次运行，
    时间戳也会撞（实测撞过）。
    """
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.retriever] = counts.get(r.retriever, 0) + 1

    used: dict[str, int] = {}
    labels: list[str] = []
    for r in reports:
        if counts[r.retriever] == 1:
            labels.append(r.retriever)
            continue
        used[r.retriever] = used.get(r.retriever, 0) + 1
        stamp = r.generated_at[11:19] if len(r.generated_at) >= 19 else ""
        suffix = f" @{stamp}" if stamp else ""
        labels.append(f"{r.retriever} #{used[r.retriever]}{suffix}")
    return labels


def _row(reports: Sequence[RetrievalReport], key: str) -> list[float]:
    return [float(getattr(r.overall, key)) for r in reports]


def _disagreement(
    a: RetrievalReport, b: RetrievalReport
) -> tuple[list[str], list[str], list[str]]:
    """逐题三分类：只有 A 检到 / 只有 B 检到 / 两边都检不到。"""
    by_b = {i.task_id: i for i in b.items}
    only_a: list[str] = []
    only_b: list[str] = []
    neither: list[str] = []
    for ia in a.items:
        ib = by_b.get(ia.task_id)
        if ib is None:
            continue
        if ia.hit and not ib.hit:
            only_a.append(ia.task_id)
        elif ib.hit and not ia.hit:
            only_b.append(ib.task_id)
        elif not ia.hit and not ib.hit:
            neither.append(ia.task_id)
    return only_a, only_b, neither


def render_markdown(reports: Sequence[RetrievalReport]) -> str:
    problems = check_comparable(reports)
    lines: list[str] = ["# 检索器对照", ""]

    if problems:
        lines.append("## ⛔ 不可比 —— 拒绝出对照表")
        lines.append("")
        for p in problems:
            lines.append(f"- {p}")
        lines.append("")
        lines.append(
            "> 不给出对照数字。**一个不可比的对照表比没有对照表更糟** —— "
            "它看起来是结论。"
        )
        return "\n".join(lines) + "\n"

    # 先说清**比的是哪两次**：光看 retriever 名字，读者不知道 embedder 是什么。
    lines.append("## 参与对照的 run")
    lines.append("")
    lines.append("| # | retriever | embedder | 生成时间 |")
    lines.append("|---|---|---|---|")
    for n, r in enumerate(reports, 1):
        lines.append(
            f"| {n} | `{r.retriever}` | `{r.embedder or '—（词法检索）'}` | {r.generated_at} |"
        )
    lines.append("")

    labels = _labels(reports)
    head = "| 指标 | " + " | ".join(f"`{x}`" for x in labels) + " |"
    sep = "|---|" + "---|" * len(reports)
    lines += [head, sep]

    for key, label in _METRICS:
        vals = _row(reports, key)
        cells = " | ".join(f"{v:.4f}" if key != "latency_ms" else f"{v:.1f}" for v in vals)
        lines.append(f"| {label} | {cells} |")

    # 上限归一化 —— 只在真正可比时才有意义（上面的检查已经保证了）
    norm = [
        (r.overall.recall / r.overall.recall_ceiling)
        if r.overall.recall_ceiling
        else 0.0
        for r in reports
    ]
    lines.append("| recall / 上限 | " + " | ".join(f"{v:.4f}" for v in norm) + " |")
    lines.append("")

    ceilings = {round(r.overall.recall_ceiling, 4) for r in reports}
    lines.append(
        f"`top_k = {reports[0].top_k}`，共 {len(reports[0].items)} 道题，"
        f"语料 {reports[0].corpus_chunks} chunks。"
    )
    if len(ceilings) == 1:
        c = next(iter(ceilings))
        if c < 0.999:
            lines.append("")
            lines.append(
                f"⚠️ recall 上限 = **{c:.4f}**（有题的 evidence 条数 > top_k）。"
                f"所以 recall 永远到不了 1.0，请对着上限读，或看上一行的归一化值。"
            )
    lines.append("")

    # 分难度
    levels = [lv for lv in ("simple", "medium", "hard") if all(lv in r.by_difficulty for r in reports)]
    if levels:
        lines.append("## 分难度")
        lines.append("")
        lines.append("| 难度 | " + " | ".join(f"`{x}` recall / hit" for x in labels) + " |")
        lines.append("|---|" + "---|" * len(reports))
        for lv in levels:
            cells = " | ".join(
                f"{r.by_difficulty[lv].recall:.4f} / {r.by_difficulty[lv].hit_rate:.4f}"
                for r in reports
            )
            lines.append(f"| {lv} | {cells} |")
        lines.append("")

    # 逐题分歧 —— 最有行动价值的部分
    if len(reports) == 2:
        a, b = reports
        only_a, only_b, neither = _disagreement(a, b)

        lines.append("## 逐题分歧")
        lines.append("")
        lines.append(f"- 只有 `{labels[0]}` 检到（{len(only_a)}）：{only_a or '—'}")
        lines.append(f"- 只有 `{labels[1]}` 检到（{len(only_b)}）：{only_b or '—'}")
        lines.append(f"- **两边都检不到**（{len(neither)}）：{neither or '—'}")

        # 把「两边都检不到」与「语料本来就不够」接上。
        # 这是两种**会叠加但不同源**的问题：前者是检索/标注，后者是语料范围。
        # 分开说，读者才不会拿一种办法去修另一种。
        gaps = {i.task_id for i in a.items if i.out_of_corpus}
        declared = [t for t in neither if t in gaps]
        undeclared = [t for t in neither if t not in gaps]
        if neither:
            lines.append("")
            if declared:
                lines.append(
                    f"  - 其中 {len(declared)} 道**同时**在「语料覆盖不全」名单里"
                    f"（{declared}）—— 就算检索修好了，答案级分数仍有天花板。"
                )
            if undeclared:
                lines.append(
                    f"  - 剩下 {len(undeclared)} 道**没有**语料缺口声明"
                    f"（{undeclared}）—— 这才是该去查检索或标注的。"
                )
        lines.append("")
        lines.append(
            "> 前两行是**换检索器能解决的**。第三行不是 —— 换谁都没用，"
            "要去查**语料里到底有没有**这条答案（可能该补文档，也可能该改标注）。"
        )
        lines.append("")

    return "\n".join(lines) + "\n"


def as_dict(reports: Sequence[RetrievalReport]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "comparable": not check_comparable(reports),
        "problems": check_comparable(reports),
        "top_k": reports[0].top_k if reports else 0,
        "runs": [
            {
                "retriever": r.retriever,
                "embedder": r.embedder,
                "generated_at": r.generated_at,
                "overall": r.overall.as_dict(),
                "recall_over_ceiling": round(
                    r.overall.recall / r.overall.recall_ceiling, 4
                )
                if r.overall.recall_ceiling
                else 0.0,
            }
            for r in reports
        ],
    }
    # markdown 里印了逐题分歧，JSON 里也必须有 —— 否则机器可读的那份少一层归因。
    if len(reports) == 2:
        only_a, only_b, neither = _disagreement(reports[0], reports[1])
        gaps = {i.task_id for i in reports[0].items if i.out_of_corpus}
        out["disagreement"] = {
            "only_first": only_a,
            "only_second": only_b,
            "neither": neither,
            "neither_with_corpus_gap": [t for t in neither if t in gaps],
            "neither_without_corpus_gap": [t for t in neither if t not in gaps],
        }
    return out


# ---------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="并排对照多份检索报告（先验可比性）"
    )
    parser.add_argument("reports", nargs="+", help="报告 JSON 路径，至少两份")
    parser.add_argument("--out", default="", help="把 markdown 写到这个文件")
    parser.add_argument("--json", default="", help="把机器可读结果写到这个文件")
    args = parser.parse_args(argv)

    try:
        reports = load_reports(args.reports)
    except (ValueError, KeyError, OSError) as exc:
        # 读不回来是**输入问题**，不是程序缺陷 —— 印一句话，不吐 traceback。
        print(f"\n! 报告读不回来：{exc}", file=sys.stderr)
        return 3

    md = render_markdown(reports)

    problems = check_comparable(reports)
    if problems:
        print(md, file=sys.stderr)
        return 2

    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"  → {args.out}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(as_dict(reports), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
