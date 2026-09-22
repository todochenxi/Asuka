"""五个进程共用的入口样板（M22）。

`__main__.py` 只做四件事，且顺序固定：

    1. 读配置（缺 PG 就在这里死，不会起来一个"什么都不持久"的进程）
    2. 装配（组合根决定接哪个真实客户端，入口文件不碰）
    3. 显式构造 SignalStop（响应 SIGTERM / SIGINT）
    4. run() 并按终态给退出码

退出码是有意义的：编排层靠它区分"我让它停的"和"它自己撑不住了"。

    0  STOPPED（收到信号 / 跑满）
    1  FAILED（连续失败达阈值，PR-8）

刻意**不**吞异常：配置错了就该以非 0 退出并把原因打到 stderr，
而不是 catch 住返回 0 —— 返回 0 的崩溃进程会被编排层当成"正常结束"。
"""
from __future__ import annotations

import sys
import traceback
from typing import Callable

from ._bootstrap import ConfigurationError
from ._runtime import ProcessReport, ProcessState


def run_process(build: Callable[[], "object"], *, max_ticks: int | None = None) -> int:
    """`build()` 返回一个带 `run()` 与 `runtime` 的 app 对象。

    `max_ticks` 只在测试里传；生产跑 `None`（一直跑到收到信号为止）。
    """
    try:
        app: object = build()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        report: ProcessReport = app.run(max_ticks=max_ticks)  # type: ignore[attr-defined]
    except Exception:
        # 不吞：让编排层看见栈，而不是看见一个干净的退出码
        traceback.print_exc()
        return 1

    print(f"{report.name}: {report.reason} ticks={report.ticks} work={report.work}")
    if report.state is ProcessState.FAILED:
        print(f"last error: {report.last_error}", file=sys.stderr)
        return 1
    return 0


def main(build: Callable[[], "object"]) -> None:
    sys.exit(run_process(build))
