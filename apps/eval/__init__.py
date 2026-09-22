"""`python -m apps.eval` —— 评估入口（M52 / M8）。

    python -m apps.eval run <dataset.json> [--base URL] [--out baseline.json]
    python -m apps.eval compare <dataset.json> --against baseline.json

退出码：

    0   全部通过（或没有回归）
    1   有用例失败 / 有回归         ← CI 该在这里红
    2   环境错误（连不上服务等）

刻意让**失败与回归都是 1**：对 CI 而言它们都是"别发"，
不需要区分；而"连不上服务"是 2 —— 那是要人去修环境，不是代码问题
（与 `apps.cli` 同一套退出码语义）。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from packages.agent_evaluation import Dataset
from packages.agent_evaluation.harness import EvaluationError, run_dataset
from packages.agent_evaluation.regression import compare, regressions, summarize

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ENV = 2

DEFAULT_BASE = "http://127.0.0.1:8011"


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def cmd_run(args: argparse.Namespace) -> int:
    dataset = Dataset.load(args.dataset)
    base = args.base or DEFAULT_BASE
    try:
        verdicts = run_dataset(base, dataset, approver=args.approver)
    except EvaluationError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ENV

    summary = summarize(verdicts)
    report: dict[str, Any] = {
        "dataset": dataset.name,
        "agent_id": dataset.agent_id,
        "base": base,
        "summary": summary,
        "cases": [v.as_dict() for v in verdicts],
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)

    if args.against:
        with open(args.against, encoding="utf-8") as fh:
            prev = json.load(fh)
        before = {c["case_id"]: bool(c["passed"]) for c in prev.get("cases", ())}
        deltas = compare(before, verdicts)
        report["regressions"] = [d.as_dict() for d in regressions(deltas)]
        report["deltas"] = [d.as_dict() for d in deltas]
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)

    print(_dump(report))
    if report.get("regressions"):
        return EXIT_FAILED
    return EXIT_OK if summary["failed"] == 0 else EXIT_FAILED


def cmd_compare(args: argparse.Namespace) -> int:
    """只看回归（不重跑）：拿两份报告比。"""
    with open(args.report, encoding="utf-8") as fh:
        after = json.load(fh)
    with open(args.against, encoding="utf-8") as fh:
        before_report = json.load(fh)
    before = {c["case_id"]: bool(c["passed"])
              for c in before_report.get("cases", ())}
    verdicts = [
        type("V", (), {
            "case_id": c["case_id"],
            "passed": bool(c["passed"]),
            "attribution": c.get("attribution", ""),
        })()
        for c in after.get("cases", ())
    ]
    deltas = compare(before, verdicts)
    print(_dump({"regressions": [d.as_dict() for d in regressions(deltas)],
                 "deltas": [d.as_dict() for d in deltas]}))
    return EXIT_FAILED if regressions(deltas) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.eval",
                                description="AgentOS 评估平台（打真服务）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一个数据集")
    r.add_argument("dataset")
    r.add_argument("--base", default="")
    r.add_argument("--approver", default="evaluator")
    r.add_argument("--out", default="", help="把报告写到文件")
    r.add_argument("--against", default="", help="与这份基线比回归")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="比两份报告（不重跑）")
    c.add_argument("report")
    c.add_argument("--against", required=True)
    c.set_defaults(func=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
