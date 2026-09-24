"""AgentOS · agent_api（M18：Control Plane 契约层）

§44 的第一行是 `POST /agents/{agent_id}/runs` —— 在此之前它在代码里**没有落点**。
本包把 `apps/api` 需要的契约钉出来，但**不与 FastAPI 耦合**：

```text
HTTP（未来 FastAPI）
   ↓  只做一行绑定
handlers.py     校验形状 / 编排 / 翻译         ← A-1 / A-2 / A-7
   ↓
ports.py        ControlPlane 协议
   ↓
service.py      进程内实现（真实实现跨进程 + DB）
   ↓
agent_runtime   RuntimeStack（Loop / Kernel / Harness）
```

**为什么不直接写 FastAPI：**

业务一旦长进路由函数，换框架就等于重写一遍业务。
而且 FastAPI 是唯一一个**连 Port 都还没定义**的基础设施
（PG / Redis / Kafka 都在阶段 4~7 定义了 Port + 适配器）。
先有 Port，HTTP 层才不会开始自己判断业务 —— 那样就出现了第二个事实源。

**不变量 A-1 ~ A-8**

| # | 内容 |
|---|---|
| A-1 | API 不做业务判断：只校验形状 / 编排 / 翻译；状态变更一律走 Runtime / Kernel |
| A-2 | 领域异常必须映射成明确 HTTP 语义；**500 只能留给真的没预料到的**（否则调用方会去重试一件永远无效的事） |
| A-3 | `POST /runs` 幂等；**幂等键不能放 Redis**（`IdempotencyStore` 的语义是"丢了 = UNKNOWN"，Run 创建丢了键就会开出第二个 Run） |
| A-4 | 审批回调是改变审批状态的**唯一入口**，且必须经 `AgentLoop.approve()` —— 不能直接改 `ApprovalRequest.status`（那样 Kernel 里的 SUSPENDED 不会被唤醒） |
| A-5 | 审批回调做归属校验（404，不泄漏别的 Run）；已决定的再回调 → 409，不能静默成功 |
| A-6 | 响应不暴露 Kernel 内部对象（`RunView` 里没有 Execution / Attempt / Lease / fencing_token） |
| A-7 | handler 与框架无关：换框架不动业务 |
| A-8 | 审批必须带 `by` —— 匿名审批进不了审计 |
| A-9 | **过期 ≠ 已决定**：APPROVED/REJECTED → 409；EXPIRED → **410**；CANCELLED → 409 独立码。把超时报成"已决定（`decided_by=timeout`）"等于告诉人"有人批过了"，而审计要回答的正是"到底有没有人批过" |
| A-10 | 待审批列表查**存储**不查内存 Loop —— 否则服务一重启列表就空了，而 Run 还挂着等人批：界面上什么都没有，系统里全在等 |
"""
from __future__ import annotations

from .dto import ApprovalView, DecisionRequest, RunView, StartRunRequest  # noqa: F401
from .errors import (  # noqa: F401
    ApiError,
    BadRequest,
    Conflict,
    Forbidden,
    Gone,
    NotFound,
    TooManyRequests,
    Unauthenticated,
    Unprocessable,
    map_domain_error,
)
from .identity import (  # noqa: F401
    SCOPE_APPROVALS_DECIDE,
    SCOPE_RUNS_READ,
    SCOPE_RUNS_WRITE,
    Identity,
    IdentityProvider,
    InMemoryIdentityProvider,
    authenticate,
    bearer_token,
    require_scope,
)
from .handlers import (  # noqa: F401
    ApiResponse,
    decide_approval,
    get_run,
    list_approvals,
    list_run_executions,
    start_run,
)
from .ports import ControlPlane, require  # noqa: F401
from .service import InProcessControlPlane  # noqa: F401

__all__ = [
    "ApiError",
    "ApiResponse",
    "ApprovalView",
    "BadRequest",
    "Conflict",
    "ControlPlane",
    "DecisionRequest",
    "Forbidden",
    "Gone",
    "Identity",
    "IdentityProvider",
    "InMemoryIdentityProvider",
    "InProcessControlPlane",
    "NotFound",
    "RunView",
    "SCOPE_APPROVALS_DECIDE",
    "SCOPE_RUNS_READ",
    "SCOPE_RUNS_WRITE",
    "StartRunRequest",
    "TooManyRequests",
    "Unauthenticated",
    "Unprocessable",
    "authenticate",
    "bearer_token",
    "decide_approval",
    "get_run",
    "list_approvals",
    "list_run_executions",
    "map_domain_error",
    "require",
    "require_scope",
    "start_run",
]
