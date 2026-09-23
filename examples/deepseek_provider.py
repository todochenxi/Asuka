"""兼容壳：DeepSeek provider 已移进 `packages.agent_runtime.model_gateway.deepseek`。

保留这个文件是为了不打断既有的部署声明
（`.env.local` / `deploy/` 里的 `AGENTOS_MODEL_PROVIDER=examples.deepseek_provider:build_model_gateway`）。
真正的实现只有一份，住在 `packages/` 里 —— 这里只是转发。
"""
from __future__ import annotations

from packages.agent_runtime.model_gateway.deepseek import build_model_gateway

__all__ = ["build_model_gateway"]
