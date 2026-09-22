"""`python -m apps.probe` —— 给编排层用的探活（M68）。

    python -m apps.probe live     你还在往前走吗？（看心跳新鲜度）
    python -m apps.probe ready    你现在能干活吗？（连一次库）

退出码只有两个值，且**没有第三个含义**：

    0   通过
    1   不通过 —— 原因打到 stdout（K8s 会把它放进 Events）

刻意让两个命令都把原因打到 **stdout** 而不是 stderr：
`kubectl describe pod` 只展示 exec probe 的标准输出，
写进 stderr 的原因在排障时看不见 —— 而排障正是它唯一的用途。
"""
from __future__ import annotations

import argparse
import os
import sys

from .app import DEFAULT_LIVE_MAX_AGE, check_live, check_ready

EXIT_OK = 0
EXIT_NOT_OK = 1


def cmd_live(args: argparse.Namespace) -> int:
    r = check_live(
        args.path or os.environ.get("AGENTOS_HEARTBEAT_FILE", ""),
        args.max_age,
    )
    print(r.detail)
    return EXIT_OK if r.ok else EXIT_NOT_OK


def cmd_ready(args: argparse.Namespace) -> int:
    from apps._dsn import resolve_dsn

    r = check_ready(resolve_dsn(args.dsn))
    print(r.detail)
    return EXIT_OK if r.ok else EXIT_NOT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apps.probe", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    live = sub.add_parser("live", help="进程还在往前走吗（心跳新鲜度）")
    live.add_argument("--path", default="", help="默认取 $AGENTOS_HEARTBEAT_FILE")
    live.add_argument("--max-age", type=float, default=DEFAULT_LIVE_MAX_AGE)
    live.set_defaults(func=cmd_live)

    ready = sub.add_parser("ready", help="依赖还能连吗（连一次库）")
    ready.add_argument("--dsn", default="", help="默认取 $AGENTOS_PG_DSN")
    ready.set_defaults(func=cmd_ready)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
