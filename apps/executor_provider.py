"""`AGENTOS_EXECUTOR_PROVIDER` 的默认实现（M23 / §61）。

--------------------------------------------------------------------------
它解决的洞

M22 把 `AGENTOS_EXECUTOR_PROVIDER` 定义成了一个 `module:function` 指针，
并且明确"没有默认执行器"。但仓库里**没有任何一个模块实现它** ——
指针指向空气。于是 `apps/worker` 依然起不来：

    ConfigurationError: AGENTOS_EXECUTOR_PROVIDER is required for apps.worker
    ...there is no default executor because executors belong to
    Runtime and Intelligence, not to infrastructure

这条消息是对的，但它把球踢给了部署方，而部署方手里没有球。本文件就是那个球。

--------------------------------------------------------------------------
它顺手修掉的洞（PR-19）

执行器表按 `executor_type`（传输）分派，表里填的却是语义执行器。
本文件第一次把两级都填齐：

    native ─┬─ tool_call        → ToolCallExecutor
            └─ human_approval   → ApprovalGateExecutor（诚实拒绝，不是伪装）
    http   ──  llm_call         → LLMCallExecutor

其中 `native:human_approval` 这一格在 M23 之前是**空的**，而审批闸门的 payload
里没有 `tool` 键 —— 于是它会被 `ToolCallExecutor` 以
`BAD_PAYLOAD: payload.tool is required`（PERMANENT，不重试）拒绝。
一句既不对、又把排障引向 payload 的话。

--------------------------------------------------------------------------
工具与模型从哪来

两者都属于 Runtime / Intelligence，组合根不替你猜（PR-16 同一判据）：

    AGENTOS_TOOL_PROVIDER   module:function → ToolRuntime
    AGENTOS_MODEL_PROVIDER  module:function → ModelGateway

仓库里 `examples/demo_stack.py` 给出一份可直接跑起来的实现，
它走的是和真实部署**完全相同**的装载路径（不是测试专用的旁路）。
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Mapping

from packages.agent_domain.execution import ExecutorType
from packages.agent_domain.execution.task import TaskType
from packages.agent_runtime.executors import (
    AgentDelegationExecutor,
    ApprovalGateExecutor,
    LLMCallExecutor,
    SkillExecutor,
    TaskTypeRouter,
    ToolCallExecutor,
)
from packages.agent_runtime.model_gateway import ModelGateway
from packages.agent_runtime.tool_runtime import ToolRuntime

from ._bootstrap import ConfigurationError, RuntimeConfig


def _load(spec: str, *, what: str, env_key: str) -> Callable[..., Any]:
    """按 `module:function` 装载一个工厂。

    和 `build_executors` 用的是同一套解析规则 —— 不在这里另写一份，
    否则"provider 名怎么写"就有两个答案。
    """
    if not spec:
        raise ConfigurationError(
            f"{env_key} is required to build the {what} "
            f"(format: 'package.module:build_x'); the composition root will not "
            f"invent one, because a guessed {what} fails every task in a way "
            f"that looks like a business error"
        )
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ConfigurationError(
            f"{env_key} must look like 'module:function', got {spec!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"cannot import {module_name!r} from {env_key}: {exc}"
        ) from exc
    factory = getattr(module, attr, None)
    if factory is None:
        raise ConfigurationError(f"{module_name} has no attribute {attr!r}")
    return factory


def build_tool_runtime(config: RuntimeConfig) -> ToolRuntime:
    """工具表。`AGENTOS_TOOL_PROVIDER` 必须返回一个真的 `ToolRuntime`。

    之所以要 `isinstance` 卡一道：返回一个裸 dict 也能"跑"（工具能查到），
    但 T-2（WRITE 工具必须带去重键）在裸 dict 那条路上根本不存在 ——
    `ToolRuntime` 才是那个拒绝"漏传 idempotency_key"的地方。
    """
    runtime = _load(
        config.tool_provider, what="tool runtime", env_key="AGENTOS_TOOL_PROVIDER"
    )()
    if not isinstance(runtime, ToolRuntime):
        raise ConfigurationError(
            f"AGENTOS_TOOL_PROVIDER must return a ToolRuntime, "
            f"got {type(runtime).__name__}; a plain mapping would skip T-2 "
            f"(WRITE tools require an idempotency key)"
        )
    return runtime


def build_model_gateway(config: RuntimeConfig) -> ModelGateway:
    """模型网关。同理必须是真的 `ModelGateway`，不能是裸 client。

    裸 client 也能"跑"，但 G-1（Fallback 不产生新 Attempt）就没人兑现了 ——
    那是 Gateway 的职责，不是 client 的。
    """
    gateway = _load(
        config.model_provider, what="model gateway", env_key="AGENTOS_MODEL_PROVIDER"
    )()
    if not isinstance(gateway, ModelGateway):
        raise ConfigurationError(
            f"AGENTOS_MODEL_PROVIDER must return a ModelGateway, "
            f"got {type(gateway).__name__}; a bare client would skip G-1 "
            f"(fallback must not create a new Attempt)"
        )
    return gateway


def build_executors(config: RuntimeConfig) -> dict[str, Any]:
    """装配两级执行器表。这是 `AGENTOS_EXECUTOR_PROVIDER` 的入口。

    返回的 key 是 `executor_type`（Kernel 的分派维度，E-12），
    value 是一个 `TaskTypeRouter`（Runtime 的第二级分派，PR-19）。
    """
    tool_runtime = build_tool_runtime(config)
    gateway = build_model_gateway(config)

    return {
        ExecutorType.NATIVE.value: TaskTypeRouter(
            {
                TaskType.TOOL_CALL: ToolCallExecutor(tool_runtime),
                # 闸门：不由 Worker 决定，但**必须占这一格** ——
                # 空着的话它会落到默认的语义执行器上，报出误导性的 BAD_PAYLOAD。
                TaskType.HUMAN_APPROVAL: ApprovalGateExecutor(),
                # M25：技能派生的是一条子 Run，主人是父 Loop。占这一格是必要的
                # （否则会退化成 ToolCallExecutor 报 payload.tool 缺失），
                # 但它是**拒绝**，不是实现 —— 覆盖度会把它记进 deferred 并说出来。
                TaskType.SKILL: SkillExecutor(),
            },
            executor_type=ExecutorType.NATIVE.value,
        ),
        ExecutorType.HTTP.value: TaskTypeRouter(
            {TaskType.LLM_CALL: LLMCallExecutor(gateway)},
            executor_type=ExecutorType.HTTP.value,
        ),
        ExecutorType.AGENT_RUNTIME.value: TaskTypeRouter(
            {
                # M25：委派派出一条子 AgentRun，主人同样是父 Loop。
                TaskType.AGENT_DELEGATION: AgentDelegationExecutor(),
            },
            executor_type=ExecutorType.AGENT_RUNTIME.value,
        ),
    }


def build_capability(config: RuntimeConfig) -> Mapping[str, Any]:
    """给组合根推导能力用：本 provider 覆盖了哪些 `(executor_type, task_type)`。

    刻意**不**在这里返回 `WorkerCapability` —— 能力应当由**真实装配出来的表**
    推导（PR-21），而不是由 provider 另行声明一份。两份声明就会漂移。
    """
    from packages.agent_runtime.executors import executor_coverage

    coverage = executor_coverage(build_executors(config))
    return {
        "covered": coverage.covered,
        "unrouted": coverage.unrouted,
        # M25：deferred 必须一起交出去。只交 covered + unrouted 的话，
        # 下游会以为"没有 unrouted = 全能干"，而其中三格其实只会拒绝。
        "deferred": coverage.deferred,
    }


__all__ = [
    "build_capability",
    "build_executors",
    "build_model_gateway",
    "build_tool_runtime",
]
