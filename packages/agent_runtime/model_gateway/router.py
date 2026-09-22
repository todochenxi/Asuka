"""Model Router：从一堆 Deployment 里排出一条**候选链**（基线 §28）。

```text
Capability     硬过滤     要 tool_use 的模型，没有就别来
Policy         硬过滤     enabled / 租户白名单
Availability   排序       healthy 优先
Load           排序       inflight 小的优先
Latency        排序       p95 小的优先
Cost           排序       便宜的优先
Priority       排序       静态配置的第一排序键
```

### 返回的是链，不是单个

`select()` 返回**有序候选链**，Gateway 依次尝试。
如果只返回一个，Fallback 就无从谈起 —— "换一个 Deployment 再试"这个语义
必须体现在 Router 的返回类型上，否则 Gateway 只能自己瞎猜下一个该找谁。

### Fallback ≠ 降级（G-4）

```text
Fallback   换 Deployment：同一个 Model，不同 provider / region   语义等价   默认允许
降级        换 Model     ：gpt-4o → gpt-4o-mini                  语义不等价  默认禁止
```

两者必须分开，因为**降级会悄悄改变输出质量**。
一个"为了可用性自动降级到小模型"的系统，出问题时你根本不知道
是模型变笨了还是 Agent 写错了 —— 所以降级必须显式声明（`allow_degraded=True`）
并在候选上打 `degraded=True` 的标记，让审计能查出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

from .models import (
    CompletionRequest,
    Deployment,
    DeploymentMetrics,
    Model,
    ModelCapability,
)


@dataclass(frozen=True)
class Candidate:
    """候选链上的一环。"""

    deployment: Deployment
    #: True = 换 Model 换来的（语义不等价），False = 同 Model 换 Deployment
    degraded: bool = False
    #: 排序分（越小越优），留着做审计与排障
    score: tuple = ()


@dataclass(frozen=True)
class Rejection:
    """为什么某个 Deployment 没进候选链。排障时最想看的东西。"""

    deployment_id: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    candidates: tuple[Candidate, ...] = ()
    rejected: tuple[Rejection, ...] = ()

    @property
    def primary(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def has_degraded(self) -> bool:
        return any(c.degraded for c in self.candidates)


@dataclass(frozen=True)
class RoutingContext:
    """路由策略输入（每次请求可不同）。"""

    tenant_id: str = ""
    #: 额外的能力要求，与 request.required_capabilities 取并集
    required_capabilities: frozenset[ModelCapability] = frozenset()
    #: 允许降级到别的 Model 吗？默认**不允许**（G-4）
    allow_degraded: bool = False
    #: 允许降级到哪些 Model（`allow_degraded=True` 时按此顺序）
    fallback_models: tuple[str, ...] = ()
    #: 代价权重：0 = 完全不看成本，1 = 成本优先于 priority
    prefer_cheap: bool = False


class MetricsProvider(Protocol):
    """运行时指标来源。不注入则视为全健康、零负载。"""

    def metrics(self, deployment_id: str) -> DeploymentMetrics: ...


@dataclass(frozen=True)
class NoMetrics:
    """默认指标源：什么都不知道，于是一视同仁。"""

    def metrics(self, deployment_id: str) -> DeploymentMetrics:
        return DeploymentMetrics()


class ModelRouter:
    """Model → 候选 Deployment 链。纯函数式：同样输入必得同样输出。"""

    def __init__(
        self,
        models: Sequence[Model],
        deployments: Sequence[Deployment],
        *,
        metrics: MetricsProvider | None = None,
    ) -> None:
        self._models: dict[str, Model] = {m.model_id: m for m in models}
        self._deployments = tuple(deployments)
        self._metrics = metrics or NoMetrics()
        if not self._models:
            raise ValueError("ModelRouter requires at least one Model")

    # ------------------------------------------------------------ 选择
    def select(
        self, request: CompletionRequest, ctx: RoutingContext | None = None
    ) -> RoutingDecision:
        ctx = ctx or RoutingContext()
        required = frozenset(request.required_capabilities) | frozenset(
            ctx.required_capabilities
        )
        candidates: list[Candidate] = []
        rejected: list[Rejection] = []

        for dep in self._deployments:
            reason = self._reject(dep, request, ctx, required)
            if reason is not None:
                rejected.append(Rejection(dep.deployment_id, reason))
                continue
            candidates.append(self._score(dep, request, ctx))

        candidates.sort(key=lambda c: c.score)
        return RoutingDecision(candidates=tuple(candidates), rejected=tuple(rejected))

    # ------------------------------------------------------------ 内部
    def _reject(
        self,
        dep: Deployment,
        request: CompletionRequest,
        ctx: RoutingContext,
        required: frozenset[ModelCapability],
    ) -> str | None:
        if not dep.enabled:
            return "deployment disabled"
        if not dep.allows(ctx.tenant_id):
            return f"tenant {ctx.tenant_id!r} not allowed"

        same_model = dep.model_id == request.model_id
        degraded_target = dep.model_id in ctx.fallback_models
        if not same_model and not (ctx.allow_degraded and degraded_target):
            return f"model mismatch (want {request.model_id}, got {dep.model_id})"

        model = self._models.get(dep.model_id)
        if model is None:
            return f"unknown model {dep.model_id}"
        missing = required - frozenset(model.capabilities)
        if missing:
            return "missing capability: " + ",".join(sorted(c.value for c in missing))

        if not self._metrics.metrics(dep.deployment_id).healthy:
            return "deployment unhealthy"
        return None

    def _score(
        self, dep: Deployment, request: CompletionRequest, ctx: RoutingContext
    ) -> Candidate:
        model = self._models[dep.model_id]
        m = self._metrics.metrics(dep.deployment_id)
        degraded = dep.model_id != request.model_id

        # 粗估成本：prompt 按 4 字符 ≈ 1 token，输出按 max_tokens 上限
        est_prompt_tokens = max(1, len(request.prompt) // 4)
        est_cost = (
            est_prompt_tokens / 1000.0 * model.input_price_per_1k
            + request.max_tokens / 1000.0 * model.output_price_per_1k
        )

        # 排序键：(降级, 优先级, 可用域权重, 成本, 负载, 延迟, id)
        # 前四项是"策略"，后三项是"观测"，最后是稳定排序的 tie-breaker。
        if ctx.prefer_cheap:
            policy = (round(est_cost, 6), dep.priority)
        else:
            policy = (dep.priority, round(est_cost, 6))

        score = (
            1 if degraded else 0,                 # 降级永远排在最后
            *policy,
            m.inflight,
            m.p95_latency_ms,
            dep.deployment_id,
        )
        return Candidate(deployment=dep, degraded=degraded, score=score)

    # ------------------------------------------------------------ 视图
    def model(self, model_id: str) -> Model | None:
        return self._models.get(model_id)

    def deployments_for(self, model_id: str) -> tuple[Deployment, ...]:
        return tuple(d for d in self._deployments if d.model_id == model_id)

    def registry(self) -> Mapping[str, Model]:
        return dict(self._models)
