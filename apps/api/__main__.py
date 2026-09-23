"""python -m apps.api —— Control Plane 的 HTTP 进程。

HTTP 服务不是 `ProcessRuntime` 那种"跑 tick 直到被叫停"的循环型进程，
它没有 tick、没有领地、也不扫队列 —— 请求来了才干活。
所以它**不复用** `apps/_entrypoint.py`（那个是给后台循环用的），
退出码语义保持同一套：0 正常退出，2 配置/框架缺失拒绝启动。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from apps._bootstrap import ConfigurationError, RuntimeConfig, build_api
from apps.api.app import FrameworkMissing, serve

EXIT_OK = 0
EXIT_REFUSED = 2


def _load_local_env() -> None:
    """加载仓库根目录的本机启动配置；已被 `.gitignore` 忽略。

    只补充当前进程里尚未设置的变量，不覆盖显式环境变量。
    密钥不进入 `app.py` 或版本库源码；`.gitignore` 对已跟踪源码不起保护作用。
    """
    path = Path(__file__).resolve().parents[2] / ".env.local"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_local_env()
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
