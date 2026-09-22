"""Model / Deployment / 请求响应（基线 §28）。

**G-3：`Model ≠ Deployment`。**

```text
Model        什么模型            gpt-4o / qwen-max        ← 逻辑概念，不含位置
Deployment   在哪里、怎么跑      azure-eastus / vllm-本地  ← 物理概念，可有多个
```

一个 Model 可以有 N 个 Deployment，这是 Fallback 能成立的前提：
换 Deployment 时**模型没变，输出语义等价**；换 Model 则是**降级**，语义不等价。
这两件事在 Router 里是分开的（见 `router.Candidate.degraded`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ModelCapability(str, Enum):
    """Router 的硬过滤维度之一：这个模型能不能干这件事。"""

    TEXT = "text"
    VISION = "vision"
    TOOL_USE = "tool_use"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"


@dataclass(frozen=True)
class Model:
    """什么模型。逻辑概念 —— 不含 endpoint、不含 provider。"""

    model_id: str
    name: str = ""
    capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.TEXT})
    context_window: int = 0
    #: 每 1K token 的价格（Gateway 只上报，不判断预算 —— G-7）
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("Model.model_id is required")

    @property
    def display(self) -> str:
        return self.name or self.model_id


@dataclass(frozen=True)
class Deployment:
    """模型在哪里、以什么方式运行。物理概念。

    同一个 Model 的多个 Deployment 互为 Fallback 候选 ——
    换过去不改变输出语义，所以默认允许。
    """

    deployment_id: str
    model_id: str
    provider: str
    endpoint: str = ""
    region: str = ""
    #: 越小越优先（Router 的第一排序键，静态配置）
    priority: int = 0
    enabled: bool = True
    #: 空 = 所有租户可用；否则只有列出的租户可用
    allowed_tenants: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.deployment_id:
            raise ValueError("Deployment.deployment_id is required")
        if not self.model_id:
            raise ValueError("Deployment.model_id is required")
        if not self.provider:
            raise ValueError("Deployment.provider is required")

    def allows(self, tenant_id: str) -> bool:
        if not self.allowed_tenants:
            return True
        return tenant_id in self.allowed_tenants


@dataclass(frozen=True)
class DeploymentMetrics:
    """运行时指标。动态 —— 由 `MetricsProvider` 注入，不写在 Deployment 里。

    为什么不放进 `Deployment`：
    Deployment 是**配置**（可冻结、可比对、可进版本库），
    指标是**观测**（每毫秒都在变）。混在一起就没法对配置做 diff 了。
    """

    inflight: int = 0
    p95_latency_ms: int = 0
    error_rate: float = 0.0
    healthy: bool = True


# ============================================================ 请求 / 响应


@dataclass(frozen=True)
class CompletionRequest:
    """Gateway 的输入。不含任何 Kernel 概念（execution_id / attempt_no 一概没有）。"""

    #: **允许为空**（G-8）：空 = "由 Gateway 决定用哪个模型"。
    #: 调用方（Executor）是翻译层，它不拥有"有哪些模型"这份知识 ——
    #: 让它必填一个模型名，就等于让它替 Gateway 做路由决定。
    #: 真正"两边都没有"的情况由 `ModelGateway._resolve_model()` 拦，
    #: 报错信息也更准（指向配置缺失，而不是某个不存在的模型名）。
    prompt: str
    model_id: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0
    #: 需要的能力：Router 用它做硬过滤（比如要 tool_use 的模型）
    required_capabilities: frozenset[ModelCapability] = frozenset()
    tenant_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: M17：组装好的 Context（有序文本块）。Gateway 不认识 Context 是怎么来的，
    #: 它只负责把它和 prompt 一起交给 Provider —— 但**它会出现在轨迹里**，
    #: 于是"模型当时看到了什么"和"调了哪个模型"在同一个地方可查。
    context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("CompletionRequest.prompt is required")


@dataclass(frozen=True)
class CompletionResponse:
    """一次成功的模型调用。"""

    text: str
    deployment_id: str
    provider: str
    model_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
