"""Model Gateway：把"调一次模型"变成"在候选链上走到成功为止"（基线 §28）。

**G-1（本阶段最重要的一条）：Gateway 内部的 Fallback 不产生新的 Kernel Attempt。**

```text
Attempt #1
   ↓
Provider A 500
   ↓
Gateway 内部 Fallback → Provider B
   ↓
仍然是 Attempt #1
```

只有当 Gateway **明确返回失败**（所有候选都不可用）时，Kernel 才创建 Attempt #2。
否则会出现 §28 说的重试放大：

```text
Gateway Fallback(3) × Kernel Retry(3) = 9 次调用
```

更准确地说，这里要防的是 **Attempt 数爆炸**而不是单纯的调用次数：
如果 Gateway 每次内部失败都算一个 Attempt，那么
`3 次 fallback → 3 个 Attempt → 每个 Attempt 又重试 3 次` 是指数级的。
所以 Gateway 必须**整体对外呈现为一次调用**：

    · 成功 → 一个 `GatewayCall`，里面带着走过的 Deployment 轨迹
    · 失败 → 一个 `GatewayError`，Kernel 才知道该开 Attempt #2

### 命名澄清（很容易看错）

本文件里所有 `attempt` 都是 **Deployment 尝试**，与 Kernel 的 `Attempt` 毫无关系。
Gateway 不认识 Kernel —— 它不 import `execution_kernel`，字段里也没有
execution_id / attempt_no / lease 这些概念。**Kernel 语义由 Executor 翻译**：
Gateway 抛 `GatewayError`，`LLMCallExecutor` 才把它翻成 `ExecutorError` + `FailureClass`。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from .models import CompletionRequest, CompletionResponse
from .providers import ProviderAdapter, ProviderError
from .router import ModelRouter, RoutingContext, RoutingDecision


@dataclass(frozen=True)
class DeploymentAttempt:
    """候选链上走过的一环（成功或失败都留下）。审计与成本归因的输入。"""

    deployment_id: str
    provider: str
    ok: bool
    error_code: str = ""
    error_message: str = ""
    latency_ms: int = 0


@dataclass(frozen=True)
class GatewayCall:
    """一次 Gateway 调用的结果。

    注意 `attempts` 是**轨迹不是重试计数** ——
    它有 N 项不代表 Kernel 会产生 N 个 Attempt（G-1）。
    """

    response: CompletionResponse
    attempts: tuple[DeploymentAttempt, ...] = ()
    #: True = 最终用的是另一个 Model，输出质量可能变了（G-4）
    degraded: bool = False

    @property
    def fallback_count(self) -> int:
        """在成功之前失败过几次（= 走了几次 Fallback）。"""
        return sum(1 for a in self.attempts if not a.ok)

    @property
    def total_latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)

    @property
    def total_tokens(self) -> int:
        return self.response.total_tokens


class GatewayError(Exception):
    """所有候选 Deployment 都失败 —— 这才是 Kernel 该建新 Attempt 的信号。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        attempts: tuple[DeploymentAttempt, ...] = (),
        retryable: bool = True,
        degraded_tried: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.attempts = attempts
        self.retryable = retryable
        self.degraded_tried = degraded_tried

    @property
    def call_count(self) -> int:
        return len(self.attempts)


@dataclass
class ModelGateway:
    """Agent Runtime 与模型供应商之间的唯一通道。

    Agent Runtime 不直接调供应商 SDK —— 一律经过这里，
    否则 Routing / Fallback / 成本归因就会散落到每个调用点。
    """

    router: ModelRouter
    adapters: Mapping[str, ProviderAdapter] = field(default_factory=dict)
    #: G-2：Fallback 次数必须显式封顶。
    #: 总调用放大 = (1 + max_fallbacks) × Kernel.max_attempts，
    #: 再往上由 `RetryPolicy.retry_budget`（Run 级）兜底。
    max_fallbacks: int = 2
    #: G-8：**缺省模型由 Gateway 决定，不由调用方决定**。
    #: Executor 是翻译层，它不拥有"有哪些模型"这份知识 ——
    #: 让调用方各填一个默认值的后果是各填各的，路由选不出来时
    #: 报的错还是 `no usable deployment for model 'default'` 这种假信息
    #: （阶段 11 装配 E2E 时就真的撞上了）。
    default_model_id: str = ""

    def __post_init__(self) -> None:
        if self.max_fallbacks < 0:
            raise ValueError("max_fallbacks must be >= 0")
        object.__setattr__(self, "adapters", dict(self.adapters))

    # ------------------------------------------------------------ 调用
    def complete(
        self, request: CompletionRequest, ctx: RoutingContext | None = None
    ) -> GatewayCall:
        request = self._resolve_model(request)
        decision = self.router.select(request, ctx)
        if not decision.candidates:
            raise GatewayError(
                "NO_CANDIDATE",
                f"no usable deployment for model {request.model_id!r}",
                retryable=False,          # 路由都选不出来，换多少次 Attempt 也没用
            )

        attempts: list[DeploymentAttempt] = []
        for index, cand in enumerate(decision.candidates):
            if index > self.max_fallbacks:
                break                     # G-2：调用预算耗尽，不再往下走

            dep = cand.deployment
            adapter = self.adapters.get(dep.provider)
            if adapter is None:
                attempts.append(
                    DeploymentAttempt(
                        deployment_id=dep.deployment_id,
                        provider=dep.provider,
                        ok=False,
                        error_code="NO_ADAPTER",
                        error_message=f"no adapter registered for provider {dep.provider}",
                    )
                )
                continue

            try:
                response = adapter.complete(dep, request)
            except ProviderError as err:
                attempts.append(
                    DeploymentAttempt(
                        deployment_id=dep.deployment_id,
                        provider=dep.provider,
                        ok=False,
                        error_code=err.code,
                        error_message=err.message,
                    )
                )
                # G-6：不可重试的错不走完整条链 ——
                # "context 超长"换哪个 provider 都一样超长。
                if not err.retryable:
                    break
                continue

            attempts.append(
                DeploymentAttempt(
                    deployment_id=dep.deployment_id,
                    provider=dep.provider,
                    ok=True,
                    latency_ms=response.latency_ms,
                )
            )
            return GatewayCall(
                response=response, attempts=tuple(attempts), degraded=cand.degraded
            )

        # ── 走到这里说明整条链都失败了 ──
        # retryable 取"是否至少有一个失败是可重试的"：
        # 全是 PERMANENT 的话，Kernel 再开 Attempt 也没意义。
        retryable = any(
            a.error_code not in _NON_RETRYABLE_CODES for a in attempts
        ) and bool(attempts)
        raise GatewayError(
            "ALL_DEPLOYMENTS_FAILED",
            f"all {len(attempts)} deployment(s) failed for model {request.model_id!r}",
            attempts=tuple(attempts),
            retryable=retryable,
            degraded_tried=any(c.degraded for c in decision.candidates),
        )

    # ------------------------------------------------------------ 模型解析
    def _resolve_model(self, request: CompletionRequest) -> CompletionRequest:
        """G-8：请求没指定模型时用 Gateway 的默认模型。

        两边都没有 → **直接报错**，而不是拿空字符串去路由。
        空字符串会命中 "model mismatch" 分支，报出来的错指向一个不存在的模型名，
        排障时会被带偏。宁可在这里说"你没告诉我用哪个模型"。
        """
        if request.model_id:
            return request
        if self.default_model_id:
            return replace(request, model_id=self.default_model_id)
        raise GatewayError(
            "NO_MODEL_REQUESTED",
            "CompletionRequest.model_id is empty and gateway.default_model_id is not set",
            retryable=False,
        )

    # ------------------------------------------------------------ 只读
    def route(self, request: CompletionRequest, ctx: RoutingContext | None = None) -> RoutingDecision:
        """只问路由，不真的调（排障 / 预检用）。"""
        return self.router.select(request, ctx)


#: 这些错误码即使出现在轨迹里，也不该让 Kernel 再开 Attempt
_NON_RETRYABLE_CODES = frozenset(
    {
        "NO_ADAPTER",
        "CONTEXT_LENGTH_EXCEEDED",
        "INVALID_REQUEST",
        "AUTH_FAILED",
        "CONTENT_FILTERED",
    }
)
