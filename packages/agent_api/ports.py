"""Control Plane 的端口（M18）。

**A-1：API 不做业务判断。**

这一层只做三件事：

    1. 校验请求**形状**（缺字段 / 类型不对 / 枚举值非法）
    2. 编排（找到那个 Run、把调用转下去）
    3. 翻译（领域异常 → HTTP 语义，领域对象 → DTO）

它**不**：判断这个动作该不该做（那是 Harness）、驱动循环（那是 Runtime）、
管生命周期（那是 Kernel）。

**A-7：handler 与框架无关。**

这里全是普通函数，`FastAPI` / `Flask` / 内部 RPC 都只是一行绑定：

```python
@app.post("/agents/{agent_id}/runs")
def _start(agent_id: str, body: dict):
    status, payload = api.start_run(cp, {**body, "agent_id": agent_id})
    return JSONResponse(payload, status_code=status)
```

不这么分层的话，业务逻辑会长进路由函数里 —— 换框架等于重写一遍业务。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .dto import (
    ApprovalView,
    DecisionRequest,
    RunView,
    StartRunRequest,
    TraceView,
)


class ControlPlane(Protocol):
    """`apps/api` 背后需要的能力。真实实现跨进程（HTTP → 服务 → DB）；
    这里的进程内实现用于把契约钉住。"""

    def start_run(self, request: StartRunRequest) -> RunView: ...

    def get_run(self, run_id: str) -> RunView: ...

    def list_runs(self) -> Sequence[RunView]:
        """M110：**本进程装载过的** Run（查询语义，空集不是错误）。

        ⚠️ 不是"全部 Run" —— 进程重启后这张表为空，除非那条 Run 被重新装载。
        真正的全局清单要一个能"列出全部"的持久源，目前没有；这里如实说清楚。
        """
        ...

    # ------------------------------------------------------------ 推进（F-1）
    #
    # 这两个方法在 M28 之前**不存在**。没有它们的后果不是"少两个接口"：
    # `start_run` 只做初始化，于是通过 API 开出来的 Run 永远停在 `created`，
    # 不产生审批、不做工具调用、永不完成。一个开得出却跑不动的 API
    # 比没有 API 更危险 —— 它看起来是通的。
    def step_run(self, run_id: str) -> RunView: ...

    def drive_run(self, run_id: str) -> RunView: ...

    # ------------------------------------------------------------ 叫停（B-8）
    #
    # 空洞 221：M33 之前这条**不存在**，于是一条通过 API 开出来的 Run
    # 只能被推进，不能被叫停 —— 想停它的唯一办法是杀进程。
    #
    # 它不是"推进"的一种：推进要走 Decision → Policy → Action 那条链，
    # 而取消恰恰必须绕开它（否则一条被 Policy 拦住的 Run 永远取消不掉）。
    #
    # 空洞 222：它也不一定是"**当场**停" —— 一条跑在别的进程里的 Run，
    # 这一进程既没有它的 stack、也可能没有它的快照（R-1：快照只在挂起时拍）。
    # 那种情况下它落下一**条持久的取消意图**（`run_cancellations`），
    # 并在 `RunView.cancel_requested` 上如实说"还没停"，而不是谎报已取消。
    #
    # 空洞 223：`idempotency_key` 是**必带**的形参（默认空串 = 不吃幂等）。
    # 取消是一次写操作，重试必须拿到第一次的答案而不是一个 409（D-35）。
    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str,
        by: str,
        idempotency_key: str = "",
    ) -> RunView: ...

    def get_trace(self, run_id: str) -> TraceView: ...

    def list_approvals(self, run_id: str | None = None) -> Sequence[ApprovalView]: ...

    def decide(self, run_id: str, request: DecisionRequest) -> RunView: ...


class RunNotFound(Exception):
    pass


def require(body: Mapping[str, Any], key: str) -> Any:
    """取必填字段。缺失是 **400**（形状问题），不是 500。"""
    if key not in body or body[key] in (None, ""):
        raise ValueError(f"{key} is required")
    return body[key]
