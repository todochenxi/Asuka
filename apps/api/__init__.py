"""`apps/api` —— Control Plane 的 HTTP 进程。

    app.py      唯一的框架绑定点（PR-22）：FastAPI + uvicorn 都只在这里出现
    __main__.py 进程入口：配好 → 建 app → serve

业务不在这个包里 —— 它在 `packages/agent_api`（与框架无关的 handler）
以及它后面的 Harness / Runtime / Kernel。
"""
from __future__ import annotations

from .app import FrameworkMissing, build_app, serve

__all__ = ["FrameworkMissing", "build_app", "serve"]
