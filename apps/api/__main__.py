"""python -m apps.api —— Control Plane 的 HTTP 进程。

HTTP 服务不是 `ProcessRuntime` 那种"跑 tick 直到被叫停"的循环型进程，
它没有 tick、没有领地、也不扫队列 —— 请求来了才干活。
所以它**不复用** `apps/_entrypoint.py`（那个是给后台循环用的），
退出码语义保持同一套：0 正常退出，2 配置/框架缺失拒绝启动。
"""
from __future__ import annotations

import sys

from apps._bootstrap import ConfigurationError, RuntimeConfig, build_api
from apps.api.app import FrameworkMissing, serve

EXIT_OK = 0
EXIT_REFUSED = 2


def main() -> int:
    try:
        config = RuntimeConfig.from_env()
        app = build_api(config)
    except (ConfigurationError, FrameworkMissing) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    serve(app, host=config.api_host, port=config.api_port)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
