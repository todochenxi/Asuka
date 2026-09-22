"""fastapi 的测试替身（M68）。

为什么需要它：单测机**没装** fastapi —— 那是 PR-15 的刻意后果
（`packages/` 与 `apps/` 零第三方依赖，于是 1172 个测试在裸机器上跑得通）。
但 `/readyz` 的行为只有真正走一遍路由才算验证过：
读源码能证明"写了这个端点"，证明不了"它返回 503 而不是 200"。

所以这里造一个**刚好够用**的 fastapi：它不实现 HTTP，
只把 `@app.get(path)` 注册进 `routes`，让测试能直接调用路由函数。

刻意**不**让它更真（不实现依赖注入、不实现校验）：
它变真的那一天，测的就不再是被测代码，而是这个替身（PR-28）。
"""
from __future__ import annotations

import sys
import types
from typing import Any


class JSONResponse:
    def __init__(self, content: Any = None, status_code: int = 200) -> None:
        self.body = content
        self.status_code = status_code


class FileResponse:
    def __init__(self, path: str, **kwargs: Any) -> None:
        self.path = path


class StaticFiles:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeApp:
    """只做一件事：把路由函数记下来，等测试来调。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.routes: dict[tuple[str, str], Any] = {}
        self.mounted: dict[str, Any] = {}

    def _register(self, method: str, path: str) -> Any:
        def decorate(fn: Any) -> Any:
            self.routes[(method, path)] = fn
            return fn

        return decorate

    def get(self, path: str) -> Any:
        return self._register("GET", path)

    def post(self, path: str) -> Any:
        return self._register("POST", path)

    def middleware(self, kind: str) -> Any:
        def decorate(fn: Any) -> Any:
            return fn

        return decorate

    def mount(self, path: str, app: Any = None, **kwargs: Any) -> None:
        self.mounted[path] = app

    def call(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        """测试入口：直接调注册的路由函数。"""
        fn = self.routes[(method, path)]
        return fn(*args, **kwargs)


def install() -> None:
    """把替身塞进 `sys.modules`，直到配套 `uninstall()`。"""
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = FakeApp  # type: ignore[attr-defined]
    # `Header(default="", alias=...)` 在真实 fastapi 里是一个"请求时求值"的标记；
    # 这里只需要让路由函数的默认参数拿到那个默认值。
    fastapi.Header = lambda default=None, **kwargs: default  # type: ignore[attr-defined]

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = JSONResponse  # type: ignore[attr-defined]
    responses.FileResponse = FileResponse  # type: ignore[attr-defined]

    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = StaticFiles  # type: ignore[attr-defined]

    fastapi.responses = responses  # type: ignore[attr-defined]
    fastapi.staticfiles = staticfiles  # type: ignore[attr-defined]

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules["fastapi.staticfiles"] = staticfiles


def uninstall() -> None:
    for name in ("fastapi", "fastapi.responses", "fastapi.staticfiles"):
        sys.modules.pop(name, None)
