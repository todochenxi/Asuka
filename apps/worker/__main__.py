"""python -m apps.worker —— 唯一真正执行 Task 的进程。"""
from __future__ import annotations

import sys

from apps._bootstrap import (
    RuntimeConfig,
    build_executors,
    build_worker_app,
    describe_coverage,
    stop_signal,
)
from apps._entrypoint import main
from apps.worker import WorkerApp


def build() -> WorkerApp:
    config = RuntimeConfig.from_env()
    executors = build_executors(config)
    # 覆盖度的三档缺口都要说出来：
    #   unrouted  不派发也不报错，只会安静堆在 PENDING
    #   deferred  有 handler 但只会拒绝 —— 系统其实仍然做不到
    # 只报前者会让人把"做不到"读成"做到了"（PR-24 同类）。
    report = describe_coverage(executors)
    if report:
        print(f"apps/worker: {report}", file=sys.stderr)
    # 事务边界由组合根接（PR-30）：入口文件不碰连接。
    return build_worker_app(config, executors=executors, signal=stop_signal())


if __name__ == "__main__":
    main(build)
