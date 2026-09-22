"""Provider Adapter：Gateway 与真实模型服务之间唯一的可替换点。

```text
ModelGateway
     ↓
ProviderAdapter        ← 本文件
     ↓
OpenAI / Anthropic / Qwen / vLLM / TensorRT-LLM / OpenAI-Compatible
```

**这里不 import 任何供应商 SDK。** 和 PG / Redis / Kafka 适配器同一个套路：
客户端是 duck-typed 的，包本身零第三方依赖。

### 为什么 `ProviderError.retryable` 必须由 Provider 自己声明

因为它决定 Gateway **要不要换 Deployment 重试**（G-6）：

    409 context_length_exceeded  → retryable=False   换 provider 也还是超限
    429 rate_limit               → retryable=True    换个 region 可能就通了
    500 upstream                 → retryable=True
    invalid api key              → retryable=False   换哪家都一样

不区分的话，一次"参数非法"会把候选链整个走一遍 ——
既浪费钱又拖慢 P99，而且最后抛的还是同一个错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .models import CompletionRequest, CompletionResponse, Deployment


class ProviderError(Exception):
    """Provider 调用失败。

    `retryable` 是 **Gateway 唯一的 Fallback 判据**（G-6）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.metadata = dict(metadata or {})


class ProviderAdapter(Protocol):
    """一个具体供应商的适配器。"""

    @property
    def provider(self) -> str: ...

    def complete(
        self, deployment: Deployment, request: CompletionRequest
    ) -> CompletionResponse: ...


@dataclass
class FunctionProvider:
    """把一个函数包成 ProviderAdapter —— 测试和脚本化场景用。

    真实适配器接 HTTP 客户端，但**接口形状完全一致**，
    所以测试里跑通的 Fallback 链在生产里是同一条代码路径。
    """

    name: str
    fn: Callable[[Deployment, CompletionRequest], CompletionResponse]
    _meta: dict[str, Any] = field(default_factory=dict)

    @property
    def provider(self) -> str:
        return self.name

    def complete(
        self, deployment: Deployment, request: CompletionRequest
    ) -> CompletionResponse:
        return self.fn(deployment, request)


def ok_response(
    deployment: Deployment,
    request: CompletionRequest,
    *,
    text: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> CompletionResponse:
    """构造成功响应（适配器内部用，省掉重复的字段搬运）。

    `model_id` 取 **deployment.model_id** 而不是 `request.model_id` ——
    降级时这两者不同：请求要 gpt-4o，实际服务的是 gpt-4o-mini。
    记成请求的模型会把小模型的账算到 gpt-4o 头上，成本归因直接错。
    """
    return CompletionResponse(
        text=text,
        deployment_id=deployment.deployment_id,
        provider=deployment.provider,
        model_id=deployment.model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        metadata=dict(metadata or {}),
    )
