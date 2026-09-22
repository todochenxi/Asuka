"""`apps/api` 的 HTTP 绑定（M24 / §62）。

--------------------------------------------------------------------------
PR-22：框架绑定只有这一个地方

PR-14 说的是"客户端（psycopg / redis / kafka）只许出现在组合根"。
FastAPI 不是客户端 —— 它是**框架**，是进程被构建于其上的东西。
两者混在同一个禁令里会逼出一个别扭的结果：要么组合根里塞满路由，
要么偷偷放宽禁令。所以这里把判据说清楚：

    客户端   有很多个（每个进程都可能自己连一次库）→ 必须单点 → 组合根
    框架     只有一个（只有 HTTP 进程需要它）      → 单点即可 → 本文件

单点这件事**仍然被静态扫描断言**（`test_pr22_framework_binding_is_single_sited`），
不是靠自觉。

--------------------------------------------------------------------------
这个文件**不认识领域**

允许 import 的只有两类：
    · `fastapi`（框架）
    · `packages.agent_api`（契约层：handler + DTO + 错误映射）

不允许 `packages.agent_domain` / `agent_harness` / `agent_runtime` /
`execution_kernel`。业务判断在 Harness，循环驱动在 Runtime，
生命周期在 Kernel —— 路由函数里出现任何一个，A-1 就被破坏了。
这条同样被静态扫描断言（`test_pr22_the_route_file_does_not_know_the_domain`）。

--------------------------------------------------------------------------
为什么 fastapi 是**惰性**导入

PR-15 的同一条理由：顶层 `import fastapi` 会让 647 个测试在没装 fastapi 的
机器上 import 期全红。所以框架在 `build_app()` 内部才被导入，
缺的时候给一条能照着做的提示。
"""
from __future__ import annotations

import importlib
from typing import Any, Mapping

from packages.agent_api.handlers import (
    cancel_run,
    decide_approval,
    drive_run,
    get_run,
    get_trace,
    list_approvals,
    list_run_executions,
    start_run,
    step_run,
)
from packages.agent_api.ports import ControlPlane


class FrameworkMissing(Exception):
    """fastapi 没装。和 `ConfigurationError` 是同一类错：缺了就不能假装能跑。"""


def _fastapi() -> Any:
    try:
        return importlib.import_module("fastapi")
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise FrameworkMissing(
            "fastapi is not installed; install it with: pip install fastapi uvicorn"
        ) from exc


def build_app(
    control_plane: ControlPlane,
    *,
    uow: Any = None,
    readiness: Any = None,
) -> Any:
    """把 Control Plane 绑成 FastAPI app。

    每个路由只做**一行转发**：形状校验 / 编排 / 翻译全在
    `packages.agent_api.handlers` 里，那里与框架无关（A-7）。
    这里有且只应该有 HTTP 术语：路径、Header、状态码。

    `uow`（PR-31）：一个请求 = 一个事务。不传就没有事务边界 —— 那时
    `pg_connection` 的 autocommit 语义就是全部保证，而 X-3 不成立。

    `readiness`（M68）：就绪探针，由组合根注入。它**不在**这里连库：
    DSN 在清单模式下只存在于 TOML 里，路由层翻不到；而一旦它自己去
    环境变量里找，清单模式部署的 `/readyz` 就会永远报"没配 DSN" ——
    一个看起来像配置错、实际是这里越权去找的答案。
    """
    fastapi = _fastapi()
    from fastapi.responses import JSONResponse

    app = fastapi.FastAPI(title="AgentOS Control Plane", version="2.1.74")

    if uow is not None:
        _add_transaction_middleware(app, uow)

    @app.post("/agents/{agent_id}/runs")
    def _start_run(
        agent_id: str,
        body: Mapping[str, Any],
        idempotency_key: str = fastapi.Header(default="", alias="Idempotency-Key"),
    ) -> Any:
        """§44 的第一行。幂等键走 Header，不放 body —— 它是传输语义，不是业务字段。"""
        response = start_run(
            control_plane,
            {**dict(body), "agent_id": agent_id},
            idempotency_key=idempotency_key,
        )
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.get("/runs/{run_id}")
    def _get_run(run_id: str) -> Any:
        response = get_run(control_plane, run_id)
        return JSONResponse(dict(response.body), status_code=response.status)

    # ── 推进（F-1）──────────────────────────────────────────────
    # M28 之前这里只有 `POST /runs`（开）和 `GET /runs/{id}`（看），
    # 缺的是中间那个"推"。于是这个 API 能开出 Run、能查询 Run，
    # 唯独不能让 Run 往前走 —— 而它看起来完全是通的。
    @app.post("/runs/{run_id}/step")
    def _step_run(run_id: str, body: Mapping[str, Any] | None = None) -> Any:
        """走一步。终态 → 409 RUN_TERMINAL（F-2）。"""
        response = step_run(control_plane, run_id, dict(body or {}))
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.post("/runs/{run_id}/run")
    def _drive_run(run_id: str, body: Mapping[str, Any] | None = None) -> Any:
        """一路走到停下来（挂起 / 终态 / 到上限）。"""
        response = drive_run(control_plane, run_id, dict(body or {}))
        return JSONResponse(dict(response.body), status_code=response.status)

    # ── 叫停（B-8 / 空洞 221）──────────────────────────────────
    # M33 之前这里只有"推"，没有"停"。少一个推进行不行得通看得出来，
    # 少一个叫停看不出来 —— Run 只是继续跑，而没人有办法让它停。
    @app.post("/runs/{run_id}/cancel")
    def _cancel_run(
        run_id: str,
        body: Mapping[str, Any],
        idempotency_key: str = fastapi.Header(default="", alias="Idempotency-Key"),
    ) -> Any:
        """把一条 Run 叫停。`reason` 与 `by` 都必填（A-8 同款）。

        `Idempotency-Key` 走 Header（与 `POST /runs` 同款）：它是**传输语义**，
        不是业务字段 —— 混进 body 里就会被当成"取消的理由"的一部分。
        """
        response = cancel_run(
            control_plane, run_id, dict(body), idempotency_key=idempotency_key
        )
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.get("/runs/{run_id}/trace")
    def _get_trace(run_id: str) -> Any:
        """账本（F-4）。只增，所以它是排障与审计的入口。"""
        response = get_trace(control_plane, run_id)
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.get("/approvals")
    def _list_approvals(run_id: str | None = None) -> Any:
        response = list_approvals(control_plane, run_id)
        return JSONResponse(dict(response.body), status_code=response.status)

    # ── agent 注册表（M61 / 空洞 231）──────────────────────────────
    # 没配注册表时返回**空列表**，不是 404：
    # "有哪些 agent 可选"是查询语义，空集不是错误
    # （与 `list_approvals` 同一条判据：服务重启后不该在最需要它的时候看不见东西）。
    @app.get("/runs/{run_id}/executions")
    def _list_run_executions(run_id: str) -> Any:
        """M65 / 空洞 234：这条 Run 手上各 Execution 的取消意图与归因。"""
        response = list_run_executions(control_plane, run_id)
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.get("/agents")
    def _list_agents() -> Any:
        registry = getattr(control_plane, "registry", None)
        agents = registry.all() if registry is not None else ()
        return JSONResponse({"items": [a.as_dict() for a in agents]})

    @app.get("/runs/{run_id}/approvals")
    def _list_run_approvals(run_id: str) -> Any:
        response = list_approvals(control_plane, run_id)
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.post("/runs/{run_id}/approvals/{approval_id}/decision")
    def _decide(run_id: str, approval_id: str, body: Mapping[str, Any]) -> Any:
        """HITL 回调。`by` 必填（A-8）—— 匿名审批进不了审计。"""
        response = decide_approval(control_plane, run_id, approval_id, body)
        return JSONResponse(dict(response.body), status_code=response.status)

    @app.get("/health")
    def _health() -> Any:
        """存活 ≠ 就绪，但这里只回答存活 —— 就绪要看它依赖的存储。"""
        return {"status": "ok"}

    @app.get("/readyz")
    def _ready() -> Any:
        """就绪 = 它依赖的存储**此刻**真的能连。

        没有探针时返回 503 而不是 200：一个"没配就绪探针"的服务
        不该被当成就绪的。200 是"可以放流量进来"，说不出依据的 200
        比 503 危险得多。
        """
        if readiness is None:
            return JSONResponse(
                {"status": "unavailable", "detail": "no readiness probe configured"},
                status_code=503,
            )
        result = readiness()
        return JSONResponse(
            {"status": "ok" if result.ok else "unavailable", "detail": result.detail},
            status_code=200 if result.ok else 503,
        )

    _mount_console(app, fastapi)

    return app


def _add_transaction_middleware(app: Any, uow: Any) -> None:
    """PR-31：一个 HTTP 请求 = 一个事务（**X-3**）。

    为什么放在中间件而不是每个路由里：六个路由以后会是十六个，
    让每个路由自己记得提交，漏掉的那个**不报错** ——
    它只是让这一批写在进程退出时静默回滚（正是 M28 开头那个 P0 的形态）。

    为什么 5xx 才回滚、4xx 照常提交：

        4xx 是"这个请求不合法"，系统**没有做错事**，做过的写该留下。
        （`decide()` 的 409 发生在任何写之前，所以这里的提交是空操作。）
        5xx 是"我们不知道发生了什么"，那就一个字都不许留下 ——
        半截状态比没有状态难解释得多。

    泄漏防线：每次请求**结束**都必须落到 commit 或 rollback 之一，
    没有第三条路。否则一个没被关闭的事务会把下一个请求的写一起带进去，
    "哪个请求的写生效了"就变成一个没有答案的问题。
    """

    @app.middleware("http")
    async def _transaction(request: Any, call_next: Any) -> Any:
        try:
            response = await call_next(request)
        except Exception:
            uow.rollback()
            raise
        if response.status_code >= 500:
            uow.rollback()
        else:
            uow.commit()
        return response


def _mount_console(app: Any, fastapi: Any) -> None:
    """把单页控制台挂在 `/` 上（M28）。

    为什么要有一个页面：**没有页面的系统，第一次被看见的时候就是出事的时候。**
    `GET /runs/{id}` 能回答"这个 Run 现在是什么状态"，但回答不了
    "它是不是真的在动" —— 后者要一眼看见账本在长、审批在等人、终态在拒绝。

    它挂在 API 进程上而不是单起一个 `apps/console`：同源、单进程、一条 URL
    就能跑起来。缺点（静态资源与控制面耦合）在演示规模上远小于
    "多一个进程就多一份装载路径"的代价。

    目录不存在就**静默跳过**，不报错 —— 服务的可用性不依赖一个演示页面。
    """
    from pathlib import Path

    directory = Path(__file__).parent / "console"
    if not directory.is_dir():
        return

    try:
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError:  # pragma: no cover
        return

    app.mount("/static", StaticFiles(directory=str(directory)), name="console-static")

    @app.get("/")
    def _console() -> Any:
        return FileResponse(str(directory / "index.html"))


def serve(app: Any, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """跑起来。uvicorn 在这里惰性导入 —— 理由同 fastapi（PR-15）。

    刻意**不**开一个单独的 `run.py`：那样 `apps/api/` 下就有两个文件认识框架，
    PR-22 的"单点"立刻变成两点。
    """
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise FrameworkMissing(
            "uvicorn is not installed; install it with: pip install uvicorn"
        ) from exc
    uvicorn.run(app, host=host, port=port)


__all__ = ["FrameworkMissing", "build_app", "serve"]
