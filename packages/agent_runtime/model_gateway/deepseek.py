"""DeepSeek（OpenAI 兼容）Provider —— AgentOS 的 ModelGateway 适配器。

这是**唯一**一份 DeepSeek 集成：AgentOS 的运行时（`/chat`、worker）用它，
Asuka 的评测 Agent 也用它。此前 Asuka 自带一份 `urllib` 直连实现
（`asuka/deepseek.py`），两套 HTTP / key / 单价 —— 那是同一个事实的两处定义
（B-7），所以合并到这里。

API key 在**调用时**从 `DEEPSEEK_API_KEY` 读，不写进这个被版本控制的源文件；
本地启动由仓库根被忽略的 `.env.local` 注入（见 `apps/api/__main__.py`）。

零第三方依赖：HTTP 走标准库 `urllib`，于是 `packages/` 仍能在裸解释器上 import。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from .gateway import ModelGateway
from .models import CompletionRequest, CompletionResponse, Deployment, Model
from .providers import FunctionProvider, ProviderError
from .router import ModelRouter

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _deepseek_complete(deployment: Deployment, request: CompletionRequest) -> CompletionResponse:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ProviderError(
            "DEEPSEEK_API_KEY_MISSING",
            "DEEPSEEK_API_KEY is required for the DeepSeek provider",
            retryable=False,
        )

    body = json.dumps(
        {
            "model": deployment.model_id,
            "messages": [
                {"role": "system", "content": "You are a helpful technical assistant."},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": float(os.environ.get("DEEPSEEK_TEMPERATURE", "0")),
            "max_tokens": request.max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        retryable = exc.code == 429 or exc.code >= 500
        raise ProviderError(
            f"DEEPSEEK_HTTP_{exc.code}",
            detail[:1000] or str(exc),
            retryable=retryable,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("DEEPSEEK_NETWORK_ERROR", str(exc), retryable=True) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError("DEEPSEEK_BAD_RESPONSE", str(exc), retryable=False) from exc

    try:
        content = raw["choices"][0]["message"]["content"]
        usage: Mapping[str, Any] = raw.get("usage") or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("DEEPSEEK_BAD_RESPONSE", "missing choices/message/content", retryable=False) from exc

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    return CompletionResponse(
        text=str(content),
        deployment_id=deployment.deployment_id,
        provider=deployment.provider,
        model_id=deployment.model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
        metadata={
            "usage": dict(usage),
            "cost_usd": (
                prompt_tokens / 1_000_000 * _env_float("DEEPSEEK_INPUT_COST_PER_MILLION", 0.27)
                + completion_tokens / 1_000_000 * _env_float("DEEPSEEK_OUTPUT_COST_PER_MILLION", 1.10)
            ),
        },
    )


def build_model_gateway() -> ModelGateway:
    """Build one DeepSeek deployment for ``AGENTOS_MODEL_PROVIDER``."""
    model_id = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    model = Model(
        model_id=model_id,
        name="DeepSeek Chat",
        context_window=65536,
        input_price_per_1k=_env_float("DEEPSEEK_INPUT_COST_PER_MILLION", 0.27) / 1000,
        output_price_per_1k=_env_float("DEEPSEEK_OUTPUT_COST_PER_MILLION", 1.10) / 1000,
    )
    deployment = Deployment(
        deployment_id=f"{model_id}@deepseek-api",
        model_id=model_id,
        provider="deepseek",
        endpoint=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
    )
    return ModelGateway(
        ModelRouter([model], [deployment]),
        {"deepseek": FunctionProvider("deepseek", _deepseek_complete)},
        max_fallbacks=0,
        default_model_id=model_id,
    )


__all__ = ["build_model_gateway"]
