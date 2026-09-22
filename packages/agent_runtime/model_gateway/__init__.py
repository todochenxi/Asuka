"""Model Gateway（基线 §28，M15 阶段 9）。

```text
Agent Runtime
     ↓
ModelGateway          ← 本包：Fallback 链 + 轨迹
     ↓
ModelRouter           ← 选候选链（不是选一个）
     ↓
ProviderAdapter       ← OpenAI / Anthropic / vLLM / ...（duck-typed，零 SDK 依赖）
```

**不变量 G-1 ~ G-7**

| # | 内容 |
|---|---|
| G-1 | Gateway 内部 Fallback **不产生新的 Kernel Attempt**；只在整条链失败时才抛错 |
| G-2 | Fallback 次数显式封顶（`max_fallbacks`），总放大由 Run 级 `retry_budget` 兜底 |
| G-3 | `Model ≠ Deployment`：前者是逻辑模型，后者是物理位置 |
| G-4 | **Fallback ≠ 降级**：换 Deployment 语义等价（默认允许），换 Model 语义不等价（默认禁止，且必须标记） |
| G-5 | 每次调用留下完整 Deployment 轨迹（审计 / 成本归因） |
| G-6 | `ProviderError.retryable` 决定要不要走完整条链 —— 不可重试的错不浪费钱 |
| G-7 | Gateway 只**上报** token / cost，不判断预算 —— 预算是 Harness 的拦截条件 |
| G-8 | **缺省模型由 Gateway 决定**：`CompletionRequest.model_id` 允许为空，Executor 是翻译层，不拥有"有哪些模型"这份知识 |

**边界**：本包不 import `execution_kernel`，字段里没有 execution_id / attempt_no / lease。
Kernel 语义由 `agent_runtime.executors.LLMCallExecutor` 翻译。
"""
from .gateway import (
    DeploymentAttempt,
    GatewayCall,
    GatewayError,
    ModelGateway,
)
from .models import (
    CompletionRequest,
    CompletionResponse,
    Deployment,
    DeploymentMetrics,
    Model,
    ModelCapability,
)
from .providers import (
    FunctionProvider,
    ProviderAdapter,
    ProviderError,
    ok_response,
)
from .router import (
    Candidate,
    MetricsProvider,
    ModelRouter,
    NoMetrics,
    Rejection,
    RoutingContext,
    RoutingDecision,
)

__all__ = [
    "Candidate",
    "CompletionRequest",
    "CompletionResponse",
    "Deployment",
    "DeploymentAttempt",
    "DeploymentMetrics",
    "FunctionProvider",
    "GatewayCall",
    "GatewayError",
    "MetricsProvider",
    "Model",
    "ModelCapability",
    "ModelGateway",
    "ModelRouter",
    "NoMetrics",
    "ProviderAdapter",
    "ProviderError",
    "Rejection",
    "RoutingContext",
    "RoutingDecision",
    "ok_response",
]
