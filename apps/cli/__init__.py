"""`python -m apps.cli` —— 控制面的命令行（M50 / M11 第一块）。

--------------------------------------------------------------------------
它是什么，不是什么

它是 **HTTP API 的薄壳**，不是第二套逻辑。每一条子命令对应一个端点，
屏幕上每一个数字都由服务真的回答 —— CLI 不推断、不缓存、不补全。

这与控制台页面是同一条规矩的两端：页面是给人点的，CLI 是给脚本和终端用的，
两者都只能显示服务端说出的话。

--------------------------------------------------------------------------
为什么退出码必须有意义

脚本靠退出码分支。一个"连不上服务却返回 0"的 CLI 会让 CI 以为
流程跑通了 —— 而它其实什么都没做。所以：

    0   成功
    2   服务返回了错误（4xx / 5xx）—— 原因打到 stderr
    3   连不上服务
    4   用法错误（缺参数 / 未知子命令）

刻意**不**吞异常：连不上就是连不上，不伪装成空结果。

--------------------------------------------------------------------------
为什么不复用 `apps/_entrypoint.py`

那份样板是给**长驻进程**用的（读配置 → 装配 → 信号 → run → 退出码）。
CLI 是一次性命令，不装配组合根、不接信号，它只认一个 HTTP 地址。
硬套那份样板会让它背上"读 PG 配置"这类与自己无关的前提 ——
于是"没配数据库"这种事会让一个纯查询命令失败（B-7：前提要匹配）。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from packages.agent_sdk import DEFAULT_BASE, AgentOSClient, Unreachable

#: 直接复用 SDK 的默认地址（`--base` > `$AGENTOS_API_BASE` > 默认）。

#: 退出码与错误语义见 SDK —— HTTP 封装（含"绕过系统代理"）只在那儿有一份，
#: CLI 与评估平台共用它（B-7：一个事实一处定义）。

EXIT_OK = 0
EXIT_HTTP_ERROR = 2
EXIT_UNREACHABLE = 3
EXIT_USAGE = 4


def _base_url(explicit: str = "") -> str:
    """服务地址。`--base` 优先，其次环境变量，最后默认。"""
    # 与 SDK 同一套优先级：`--base` > `$AGENTOS_API_BASE` > 默认
    return AgentOSClient(base=explicit).base


def request(
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | None = None,
    base: str = "",
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any, str]:
    """打一次 HTTP。返回 `(状态码, 解析后的 JSON 或 None, 原始文本)`。

    刻意**不**把错误吞成异常：调用方要能区分"服务说了不"和"根本没连上"，
    这两种失败的处置完全不同（前者是业务结果，后者是环境问题）。

    实现在 `packages.agent_sdk` —— CLI 与评估平台**共用同一份**。
    "绕过系统代理"那段知识（本机服务没起时会被代理误报成 502）
    只许有一处，否则改一处忘一处，就会得到只对了一半的修复（B-7）。
    """
    return AgentOSClient(base=base, timeout=timeout).request(
        method, path, body=body, headers=headers
    )


def _build_no_proxy_opener() -> Any:
    """兼容旧引用：真正的实现在 SDK 里（测试用它断言"没走代理"）。"""
    from packages.agent_sdk import _build_no_proxy_opener as _impl

    return _impl()


def _fail(message: str, code: int = EXIT_HTTP_ERROR) -> int:
    print(message, file=sys.stderr)
    return code


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _error_of(payload: Any, raw: str) -> str:
    """把服务端给的错误说清楚（PR-19：报错要说中真发生了什么）。"""
    if isinstance(payload, Mapping):
        err = payload.get("error")
        if isinstance(err, Mapping):
            code = err.get("code", "")
            msg = err.get("message", "")
            return f"{code}: {msg}" if code else str(msg)
        if isinstance(err, str):
            return err
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return raw[:300] if raw else "(empty response)"


# ---------------------------------------------------------------- 子命令

def cmd_health(args: argparse.Namespace) -> int:
    status, payload, raw = request("GET", "/health", base=args.base)
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_start(args: argparse.Namespace) -> int:
    if not args.agent:
        return _fail("缺 agent_id：起一条 Run 必须指明是哪个 agent", EXIT_USAGE)
    body: dict[str, Any] = {"user_request": args.request}
    headers = {"Idempotency-Key": args.key} if args.key else None
    status, payload, raw = request(
        "POST",
        f"/agents/{_quote(args.agent)}/runs",
        body=body,
        base=args.base,
        headers=headers,
    )
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    status, payload, raw = request(
        "GET", f"/runs/{_quote(args.run_id)}", base=args.base
    )
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def _advance(args: argparse.Namespace, mode: str) -> int:
    status, payload, raw = request(
        "POST", f"/runs/{_quote(args.run_id)}/{mode}", body={}, base=args.base
    )
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_step(args: argparse.Namespace) -> int:
    return _advance(args, "step")


def cmd_drive(args: argparse.Namespace) -> int:
    return _advance(args, "run")


def cmd_cancel(args: argparse.Namespace) -> int:
    """B-8：归因必填。缺了就拒绝，不替用户编一句"为什么"。"""
    if not args.reason or not args.by:
        return _fail(
            "缺归因：`--reason` 与 `--by` 都要给 —— "
            "一条说不出谁叫停、为什么的取消等于没有发生过（B-8 / A-8）",
            EXIT_USAGE,
        )
    body = {"reason": args.reason, "by": args.by}
    headers = {"Idempotency-Key": args.key} if args.key else None
    status, payload, raw = request(
        "POST",
        f"/runs/{_quote(args.run_id)}/cancel",
        body=body,
        base=args.base,
        headers=headers,
    )
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_approvals(args: argparse.Namespace) -> int:
    status, payload, raw = request("GET", "/approvals", base=args.base)
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_decide(args: argparse.Namespace) -> int:
    """契约是 `decision: "approve" | "reject"`，不是 `approved: true`。"""
    body = {
        "decision": "approve" if args.approve else "reject",
        "by": args.by or "human",
        "comment": args.comment or "",
    }
    path = (
        f"/runs/{_quote(args.run_id)}/approvals/"
        f"{_quote(args.approval_id)}/decision"
    )
    status, payload, raw = request("POST", path, body=body, base=args.base)
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def cmd_trace(args: argparse.Namespace) -> int:
    status, payload, raw = request(
        "GET", f"/runs/{_quote(args.run_id)}/trace", base=args.base
    )
    if status == 0:
        return _fail(f"连不上服务 {_base_url(args.base)}", EXIT_UNREACHABLE)
    if status >= 400:
        return _fail(_error_of(payload, raw))
    print(_dump(payload))
    return EXIT_OK


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


# ---------------------------------------------------------------- 入口

def cmd_executions(args: argparse.Namespace) -> int:
    """`GET /runs/{id}/executions` —— 这条 Run 手上各 Execution 的取消归因。

    这条命令本身就是空洞 234 的答案：M48 把"谁叫停了这一刀"写进了库，
    但一直没有人读它。现在可以问了。
    """
    client = AgentOSClient(base=args.base)
    try:
        payload = client.request_or_raise("GET", f"/runs/{_quote(args.run_id)}/executions")
    except Unreachable as e:
        return _fail(str(e), EXIT_UNREACHABLE)
    print(_dump(payload))
    return EXIT_OK


def cmd_manifest(args: argparse.Namespace) -> int:
    """校验一份部署清单（M59）。

    `check`  只校验，打印它声明了什么
    `env`    打印它翻译出的环境变量（供 shell `eval`，或写进 K8s ConfigMap）
    """
    from packages.agent_manifest import ManifestError, load

    try:
        manifest = load(args.path)
    except ManifestError as e:
        return _fail(str(e), EXIT_USAGE)
    except OSError as e:
        return _fail(f"读不到清单 {args.path}: {e}", EXIT_USAGE)

    if args.manifest_cmd == "env":
        print(_dump(manifest.to_env()))
    else:
        print(_dump(manifest.values))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.cli",
        description="AgentOS 控制面命令行 —— 每个数字都由服务真的回答",
    )
    parser.add_argument(
        "--base",
        default="",
        help=f"服务地址（默认 $AGENTOS_API_BASE 或 {DEFAULT_BASE}）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="探活").set_defaults(func=cmd_health)

    p = sub.add_parser("start", help="发起一条 Run")
    p.add_argument("agent")
    p.add_argument("request")
    p.add_argument("--key", default="", help="幂等键")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("status", help="查一条 Run")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("step", help="走一步")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_step)

    p = sub.add_parser("drive", help="推进到阻塞点或终态")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_drive)

    p = sub.add_parser("cancel", help="叫停（要求归因）")
    p.add_argument("run_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--key", default="", help="幂等键")
    p.set_defaults(func=cmd_cancel)

    sub.add_parser("approvals", help="列出待审批").set_defaults(func=cmd_approvals)

    p = sub.add_parser("decide", help="批准或驳回")
    p.add_argument("run_id")
    p.add_argument("approval_id")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--by", default="alice")
    p.add_argument("--comment", default="")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("trace", help="看账本")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("executions", help="看各 Execution 的取消归因（谁叫停的、为什么）")
    p.add_argument("run_id")
    p.add_argument("--base", default="")
    p.set_defaults(func=cmd_executions)

    m = sub.add_parser("manifest", help="校验部署清单（agentos.toml）")
    m.add_argument("manifest_cmd", choices=("check", "env"))
    m.add_argument("path")
    m.set_defaults(func=cmd_manifest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
