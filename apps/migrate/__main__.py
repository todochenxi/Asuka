"""`python -m apps.migrate` —— 生产迁移入口（M67）。

    python -m apps.migrate check          # 线上跑到哪一版了？有没有待应用？
    python -m apps.migrate apply          # 把待应用的补上

退出码是有意义的，编排层靠它分支：

    0   UP_TO_DATE    没有待应用（check）/ 应用成功（apply）
    2   CONFIG        没给 DSN / 没装驱动 / 迁移目录不存在
    3   PENDING       仅 check：有待应用的迁移 —— 不是错，是"落后了"
    4   DRIFT         已上线的迁移被改过 —— 这是真错，不许继续
    5   FAILED        应用时 PG 报错，点名哪一条
    6   USAGE         用法错误

刻意**不**提供 `--force`：绕过 drift 检查就是篡改账本，
那条路一旦开了，"线上到底跑的是哪一版"就又没有答案了。
"""
from __future__ import annotations

import argparse
import sys

from .app import (
    MigrationError,
    apply_pending,
    connect,
    discover,
    ensure_bookkeeping,
    plan,
    read_applied,
    release_lock,
    report,
    with_lock,
)

EXIT_UP_TO_DATE = 0
EXIT_CONFIG = 2
EXIT_PENDING = 3
EXIT_DRIFT = 4
EXIT_FAILED = 5
EXIT_USAGE = 6

#: 配置错误 vs 应用失败：前者不该重试（改配置再来），后者值得看一眼库。
_CONFIG_CODES = frozenset({"NO_DSN", "NO_PG_DRIVER", "NO_MIGRATIONS_DIR", "NO_MIGRATIONS",
                           "CONNECT_FAILED"})


def _dsn(explicit: str) -> str:
    # 走 `apps._dsn.resolve_dsn` 而不是自己翻环境变量：
    # 清单模式下 DSN 只在 TOML 里（M71：真部署时这里报过 NO_DSN）
    from apps._dsn import resolve_dsn

    return resolve_dsn(explicit)


def cmd_check(args: argparse.Namespace) -> int:
    dsn = _dsn(args.dsn)
    migrations = discover(args.dir or None)
    conn = connect(dsn)
    try:
        applied = read_applied(conn)
    finally:
        conn.close()

    p = plan(applied, migrations)
    print(report(p))
    if p.drifted:
        print(
            "refusing to continue: an already-applied migration was modified "
            "(history is append-only)",
            file=sys.stderr,
        )
        return EXIT_DRIFT
    if p.pending:
        return EXIT_PENDING
    return EXIT_UP_TO_DATE


def cmd_apply(args: argparse.Namespace) -> int:
    dsn = _dsn(args.dsn)
    migrations = discover(args.dir or None)
    conn = connect(dsn)
    try:
        with_lock(conn)
        try:
            ensure_bookkeeping(conn)
            p = plan(read_applied(conn), migrations)
            print(report(p))
            if p.drifted:
                print(
                    "refusing to apply: an already-applied migration was modified "
                    "(history is append-only)",
                    file=sys.stderr,
                )
                return EXIT_DRIFT
            if not p.pending:
                print("up to date")
                return EXIT_UP_TO_DATE
            applied = apply_pending(conn, p, log=lambda s: print(s))
            print(f"applied {len(applied)} migration(s)")
            return EXIT_UP_TO_DATE
        finally:
            release_lock(conn)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apps.migrate", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("check", "报告线上跑到哪一版、有没有待应用（只读）"),
                            ("apply", "补齐待应用的迁移")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--dsn", default="", help="默认取 $AGENTOS_PG_DSN")
        s.add_argument("--dir", default="", help="迁移目录，默认 infrastructure/postgres")
        s.set_defaults(func=cmd_check if name == "check" else cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MigrationError as e:
        print(str(e), file=sys.stderr)
        if e.code in _CONFIG_CODES:
            return EXIT_CONFIG
        if e.code == "MIGRATION_DRIFT":
            return EXIT_DRIFT
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
