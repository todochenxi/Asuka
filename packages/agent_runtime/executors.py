"""最小执行器：一个 LLM + 一个 Tool。

它们实现的是 Kernel 侧的 `worker.Executor` 协议，因此：

    · 不认识 Goal / Decision / State（X-2）
    · 不认识数据库
    · 只拿到 `ExecutionContext`（execution_id / attempt_no / cancellation / heartbeat）

Executor 里唯一需要"自觉"的两件事：
    1. 长任务要在安全点调 `ctx.heartbeat()` 续租
    2. 长任务要在安全点查 `ctx.check_cancelled()`（协作式取消）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from packages.agent_domain.execution import Task
from packages.agent_domain.execution.retry import FailureClass
from packages.agent_domain.execution.task import TaskType
from packages.execution_kernel.worker import ExecutionContext, Executor, ExecutorError

from .task_factory import ACTION_TO_TASK

from .model_gateway import (
    CompletionRequest,
    Deployment,
    FunctionProvider,
    GatewayError,
    Model,
    ModelGateway,
    ModelRouter,
    ProviderError,
    ok_response,
)
from .tool_runtime import (
    ToolExecutionError,
    ToolNotFoundError,
    ToolRuntime,
    ToolValidationError,
)
from .ports import LLMClient, Tool


class ToolRegistry:
    """进程内工具表（真实实现换成 MCP / HTTP 工具网关）。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool | Callable[..., Mapping[str, Any]]] = {}

    def register(self, name: str, tool: Tool | Callable[..., Mapping[str, Any]]) -> None:
        self._tools[name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        if name not in self._tools:
            # Tool 不存在是**永久**失败：重试没意义
            raise ExecutorError("TOOL_NOT_FOUND", f"no such tool: {name}",
                                FailureClass.PERMANENT)
        tool = self._tools[name]
        call = getattr(tool, "call", tool)
        return dict(call(args))

    def items(self) -> dict[str, Tool | Callable[..., Mapping[str, Any]]]:
        """给 `legacy_tool_runtime()` 用：把这张表迁移成真正的 ToolRuntime。"""
        return dict(self._tools)


def legacy_tool_runtime(registry: ToolRegistry) -> ToolRuntime:
    """把旧的进程内 `ToolRegistry` 迁移成真正的 `ToolRuntime`。

    和 `legacy_gateway()` 同一个理由：**不留第二条代码路径**。
    Executor 要么走 ToolRuntime，要么就是另一套语义 ——
    那 T-2（WRITE 工具必须带去重键）在旧路径上根本没被验证过。

    ⚠️ 兼容路径下的工具一律按 `side_effect=READ` 登记 ——
    进程内函数通常没有外部副作用，但这是**保守假设**。
    真实工具必须显式声明 `side_effect`，否则拿不到"漏传 key 就拒绝"的保护。
    """
    from .tool_runtime import FunctionInvoker, ToolRegistry as NewRegistry, ToolSpec

    new_registry = NewRegistry()
    for name, tool in registry.items().items():
        call = getattr(tool, "call", tool)
        new_registry.register(
            ToolSpec(name=name, version="legacy"),
            FunctionInvoker(lambda args, _c=call: _c(args)),
        )
    return ToolRuntime(new_registry)


class ToolCallExecutor:
    """payload = {"tool": str, "args": {...}}

    Kernel 语义的翻译层：ToolRuntime 只说"工具调用失败"，
    这里把它翻成 `ExecutorError` + `FailureClass`。
    """

    def __init__(self, runtime_or_registry: ToolRuntime | ToolRegistry) -> None:
        if isinstance(runtime_or_registry, ToolRuntime):
            self.runtime = runtime_or_registry
        else:
            self.runtime = legacy_tool_runtime(runtime_or_registry)
        #: 兼容字段：旧代码可能直接读 executor.registry
        self.registry = runtime_or_registry

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        ctx.heartbeat()                                  # 安全点：续租
        name = task.payload.get("tool")
        if not name:
            raise ExecutorError("BAD_PAYLOAD", "payload.tool is required",
                                FailureClass.PERMANENT)
        if ctx.check_cancelled():
            raise ExecutorError("CANCELLED", "cancelled before tool call",
                                FailureClass.PERMANENT)

        # §16：idempotency_key = execution_id，跨 Attempt 稳定。
        # WRITE 类工具靠它防重复副作用（T-2）。
        key = getattr(ctx, "idempotency_key", "") or getattr(ctx, "execution_id", "")

        try:
            result = self.runtime.call(
                name, task.payload.get("args") or {}, idempotency_key=key
            )
        except ToolNotFoundError as err:
            raise ExecutorError("TOOL_NOT_FOUND", str(err), FailureClass.PERMANENT) from err
        except ToolValidationError as err:
            # T-3：参数不会自己变对，重试纯属浪费
            raise ExecutorError("BAD_PAYLOAD", str(err), FailureClass.PERMANENT) from err
        except ToolExecutionError as err:
            raise ExecutorError(
                err.code, err.message,
                FailureClass.TRANSIENT if err.retryable else FailureClass.PERMANENT,
            ) from err

        return {
            "tool": result.tool_name,
            "version": result.version,
            "result": result.output,
            "side_effect": result.side_effect.value,
            "idempotency_key": result.idempotency_key,
        }


def legacy_gateway(client: LLMClient, *, model_id: str = "default") -> ModelGateway:
    """把一个裸 `LLMClient` 包成单 Deployment 的 Gateway。

    **为什么不让 Executor 分两条路（有 Gateway 走 Gateway、没有就直接调）：**

    两条路就等于有两条 Fallback 语义。测试跑通的那条和生产跑的那条不是同一条，
    那么"Fallback 不产生新 Attempt"（G-1）在直接调用那条路上根本没被验证过。
    所以宁可造一个只有一个候选的 Gateway，也要让所有调用走同一条代码路径。
    """
    model = Model(model_id=model_id, name=model_id)
    deployment = Deployment(
        deployment_id=f"{model_id}@legacy",
        model_id=model_id,
        provider="legacy",
        endpoint="in-process",
    )

    def _call(dep: Deployment, request: CompletionRequest):
        try:
            raw = client.complete(request.prompt)
        except ProviderError:
            raise
        except Exception as err:                          # noqa: BLE001
            # 裸客户端抛什么都当成可重试的供应商故障（TRANSIENT）
            raise ProviderError("LLM_ERROR", str(err), retryable=True) from err
        text = str(raw.get("text", "")) if isinstance(raw, Mapping) else str(raw)
        return ok_response(dep, request, text=text)

    return ModelGateway(
        ModelRouter([model], [deployment]),
        {"legacy": FunctionProvider("legacy", _call)},
        max_fallbacks=0,                                  # 只有一个候选，无所谓
    )


class LLMCallExecutor:
    """payload = {"prompt": str, "model": str|None, ...}

    Kernel 语义的**翻译层**：Gateway 只说"模型调用失败"，
    这里把它翻成 Kernel 认得的 `ExecutorError` + `FailureClass`（G-1 的另一半）。
    """

    def __init__(
        self,
        client_or_gateway: LLMClient | ModelGateway,
        *,
        default_model_id: str = "default",
    ) -> None:
        if isinstance(client_or_gateway, ModelGateway):
            self.gateway = client_or_gateway
            self._legacy = False
        else:
            self.gateway = legacy_gateway(client_or_gateway, model_id=default_model_id)
            self._legacy = True
        self.default_model_id = default_model_id

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        ctx.heartbeat()
        prompt = task.payload.get("prompt")
        if not prompt:
            raise ExecutorError("BAD_PAYLOAD", "payload.prompt is required",
                                FailureClass.PERMANENT)
        if ctx.check_cancelled():
            raise ExecutorError("CANCELLED", "cancelled before llm call",
                                FailureClass.PERMANENT)

        if self._legacy:
            model_id = self.default_model_id
        else:
            # G-8：没指定模型就交给 Gateway 决定 —— Executor 是翻译层，
            # 它不拥有"有哪些模型"这份知识。自己填一个默认值的话，
            # 路由失败时报的错会指向一个根本不存在的模型名。
            model_id = task.payload.get("model") or ""
        max_tokens = int(task.payload.get("max_tokens") or 1024)

        try:
            call = self.gateway.complete(
                CompletionRequest(
                    model_id=model_id,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    # M17：Context 由 Runtime 组装好随 Task 下来；Executor 只是搬运工，
                    # 它**不**在这里拼字符串（C-11：组装归 Runtime）。
                    context=tuple(task.payload.get("context") or ()),
                )
            )
        except GatewayError as err:
            # Gateway 明确说"全链失败" → 这才是 Kernel 该开 Attempt #2 的信号。
            # 注意：无论 Gateway 内部走过几个 Deployment，这里**只抛一次**。
            raise ExecutorError(
                "GATEWAY_FAILED",
                f"{err.code}: {err.message}",
                FailureClass.TRANSIENT if err.retryable else FailureClass.PERMANENT,
            ) from err

        resp = call.response
        return {
            "prompt": prompt,
            "response": {"text": resp.text},
            "model": resp.model_id,
            "deployment": resp.deployment_id,
            # M94：成本与延迟必须**随结果一起走**。Provider 已经把它们算出来了
            # （`CompletionResponse.latency_ms` / `metadata["cost_usd"]`），
            # 但 Executor 此前只搬运 `usage` —— 于是 Asuka 的评测拿到的成本恒为 0，
            # 而报告里那一栏读起来像"免费"。搬运工漏搬的东西，下游不会报错。
            "latency_ms": resp.latency_ms,
            "cost_usd": float((resp.metadata or {}).get("cost_usd") or 0.0),
            "usage": {
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.total_tokens,
            },
            # 轨迹留在结果里，成本归因与排障靠它（G-5）
            "gateway": {
                "fallback_count": call.fallback_count,
                "degraded": call.degraded,
                "calls": [a.deployment_id for a in call.attempts],
            },
        }


# ---------------------------------------------------------------------------
# PR-19：分派是两级的 —— executor_type（传输）× task_type（语义）
# ---------------------------------------------------------------------------


def _task_type_value(task_type: Any) -> str:
    """`TaskType` 是 `str, Enum`，裸字符串和枚举都可能进来 —— 统一成 value。"""
    return task_type.value if isinstance(task_type, TaskType) else str(task_type)


class TaskTypeRouter:
    """第二级分派：同一个 executor_type 下，按 `task_type` 交给不同的执行器。

    ------------------------------------------------------------------
    为什么必须有这一级

    `Worker._executor_for()` 只按 `executor_type` 查表（native / http / mcp /
    agent_runtime / workflow）。那是**传输**维度，是 Kernel 唯一认得的调度维度
    （E-12：Scheduler 只认识 Task）。但表里填的 `ToolCallExecutor` /
    `LLMCallExecutor` 是**语义**维度的东西。

    两个维度恰好对上，靠的是 `ACTION_TO_TASK` 把 LLM 送到 HTTP、把 TOOL 送到
    NATIVE。**只要有人偏离这个巧合，错误就会被伪装成另一种错误**：

        native:skill             → ToolCallExecutor → payload.tool 缺失
        native:human_approval    → ToolCallExecutor → payload.tool 缺失
        http:tool_call           → LLMCallExecutor  → payload.prompt 缺失

    三者的报错都是 `BAD_PAYLOAD`，而且都是 PERMANENT（不重试）。
    排障的人会去查"payload 是谁填的"，而真正的错是**分派表少了一行**。
    这是"请求的值 vs 实际拿到的值"在路由上的变体：你要的是 skill 执行器，
    拿到的是 tool 执行器，而报错说的是 payload 不对。

    ------------------------------------------------------------------
    边界：为什么这一级在 Runtime，不在 Kernel

    X-2：Kernel 不认识 Goal / Decision / Action。"这个 Task 到底要干什么"
    是 Runtime 的知识，所以第二级分派发生在 `agent_runtime`，Kernel 一行都不用改
    （`TaskTypeRouter` 本身就是一个 `Executor`，Worker 完全无感）。
    """

    def __init__(
        self,
        handlers: Mapping[TaskType | str, Executor],
        *,
        executor_type: str,
    ) -> None:
        self._handlers: dict[str, Executor] = {
            _task_type_value(key): value for key, value in handlers.items()
        }
        self._executor_type = executor_type

    @property
    def executor_type(self) -> str:
        return self._executor_type

    def task_types(self) -> frozenset[str]:
        """本路由器声明能接的 task_type —— 覆盖校验（PR-21）读的就是它。"""
        return frozenset(self._handlers)

    def handles(self, task_type: TaskType | str) -> bool:
        return _task_type_value(task_type) in self._handlers

    def handler(self, task_type: TaskType | str) -> Executor | None:
        """取回某个 task_type 的 handler。

        L-2（"Worker 的 Executor 与 Loop 持有的是同一批对象"）要靠它才能被断言 ——
        路由器把 Gateway 包在里面之后，"共享同一个对象"这件事必须仍然**看得见**，
        否则 L-2 就从"可断言的事实"退化成"一句注释"。
        """
        return self._handlers.get(_task_type_value(task_type))

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        handler = self._handlers.get(_task_type_value(task.task_type))
        if handler is None:
            # 点名**两个轴**。只说 "no executor" 会让人以为是 executor_type 配错了，
            # 而这里缺的往往是一个 task_type 的 handler。
            registered = sorted(self._handlers) or ["<none>"]
            raise ExecutorError(
                "EXECUTOR_NOT_FOUND",
                f"no handler for task_type={_task_type_value(task.task_type)!r} "
                f"under executor_type={self._executor_type!r}; "
                f"registered task_types here: {registered}",
                FailureClass.PERMANENT,
            )
        return handler.execute(task, ctx)


class DeferringExecutor:
    """Worker 不该执行它的执行器 —— 真正的主人在别处。

    ------------------------------------------------------------------
    为什么要有这么一个标记

    `executor_coverage()` 说"这个组合有没有 handler"。加了 `SkillExecutor` /
    `AgentDelegationExecutor` 之后，`unrouted` 会清零 ——
    于是启动日志什么都不报了，看上去"全覆盖"。

    但系统其实**仍然跑不了技能、跑不了委派**：这两个 handler 的全部内容
    就是拒绝。此时 `unrouted=∅` 是一句谎话，而且是最有害的那种 ——
    它把"做不到"报成了"做到了"，和 PR-24（版本号漂移）是同一类：
    **对外报的值与实际能做的事不是同一个。**

    所以覆盖度必须区分三档，不是两档：

        covered    有 handler，且 Worker 真能把它干完
        deferred   有 handler，但它的职责是**拒绝** —— 主人在别处
        unrouted   连 handler 都没有（报错会指向错误的地方）

    `deferral_owner` 是给**人**看的一句话，会被打到启动日志里。
    """

    deferral_owner: str = ""

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        raise NotImplementedError


class ApprovalGateExecutor(DeferringExecutor):
    """闸门 Task（`TaskType.HUMAN_APPROVAL`）在 Worker 侧的**诚实**归宿。

    正常路径里闸门根本不由 Worker 执行：

        Loop 造闸门 Task → Kernel 写 SUSPENDED(HUMAN_APPROVAL)（H-4）
        → 人批 → Wake-up Controller → Loop 关闸门

    Worker 唯一能拿到它的情形是：Lease 过期被 Recovery 重新排队，
    而此时**拥有它的那个 Loop 已经不在了**。

    这时候正确的行为不是"假装替人做了决定"，而是把话说清楚。
    退化到 `ToolCallExecutor` 的话报错是
    `BAD_PAYLOAD: payload.tool is required` —— 一句既不对、又把人引向 payload
    的话；闸门 payload 里本来就没有 `tool`，它有的是 `approval_id`。
    """

    deferral_owner = "Harness / AgentLoop（H-4）：人在 Loop 侧批，Worker 不替人做决定"

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        raise ExecutorError(
            "GATE_NOT_WORKER_EXECUTABLE",
            "task_type=human_approval is an approval gate: the decision belongs to "
            "Harness/Loop (H-4), not to a Worker; that a Worker received it means "
            "the owning Loop is gone and this gate can no longer be decided",
            FailureClass.PERMANENT,
        )


class SkillExecutor(DeferringExecutor):
    """`TaskType.SKILL`（`native`）在 Worker 侧的**诚实**归宿。

    Skill = Procedure（§25）—— 内部有多步。这意味着派发一次技能调用，
    实际是**开一条子 Run**，而不是"干一件活"。一条 Execution 装不下
    一整条子 Run（E-19），所以正常路径里它根本不由 Worker 执行：

        Loop 决定调技能 → `_suspend_for_child()` 派生子 SkillRun
        → Kernel 写 SUSPENDED(CHILD_SKILL) → 子 Run 跑完 → 唤醒父 Run

    Worker 唯一能拿到它的情形是：**拥有它的那个 Loop 已经不在了**
    （Lease 过期被 Recovery 重新排队，而父 Run 没被装载回来）。

    这时候正确的行为不是"假装把技能跑一遍"，而是把话说清楚。
    退化到 `ToolCallExecutor` 的话报错是
    `BAD_PAYLOAD: payload.tool is required` —— 技能 payload 里本来
    就没有 `tool`，它有的是 `skill` / `name`。一句既不对、又把人引向 payload 的话。
    """

    deferral_owner = (
        "parent AgentLoop（SuspensionReason.CHILD_SKILL）："
        "技能派生的是一条子 Run，由父 Loop 在派发前接走"
    )

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        raise ExecutorError(
            "SKILL_NOT_WORKER_EXECUTABLE",
            "task_type=skill spawns a child run, so it is owned by the parent "
            "AgentLoop (SuspensionReason.CHILD_SKILL), not by a Worker; that a "
            "Worker received it means the owning Loop is gone — reload the parent "
            "run instead of executing it here, otherwise a retry would spawn a "
            "second skill run",
            FailureClass.PERMANENT,
        )


class AgentDelegationExecutor(DeferringExecutor):
    """`TaskType.AGENT_DELEGATION`（`agent_runtime`）在 Worker 侧的诚实归宿。

    与 `SkillExecutor` 同构，指向的对象不同：委派派出的是一条**子 AgentRun**
    （A2A，§26）。`SuspensionReason.CHILD_AGENT` 从 M15 起就冻结在基线里，
    但直到 M25 才有代码真的去设置它 —— 概念冻结了六年、实现从没跟上，
    而因为没有一条测试断言"谁设置了它"，一直没人发现。
    """

    deferral_owner = (
        "parent AgentLoop（SuspensionReason.CHILD_AGENT）："
        "委派派出的是一条子 AgentRun，由父 Loop 在派发前接走"
    )

    def execute(self, task: Task, ctx: ExecutionContext) -> Mapping[str, Any]:
        raise ExecutorError(
            "DELEGATION_NOT_WORKER_EXECUTABLE",
            "task_type=agent_delegation spawns a child AgentRun, so it is owned by "
            "the parent AgentLoop (SuspensionReason.CHILD_AGENT), not by a Worker; "
            "that a Worker received it means the owning Loop is gone — reload the "
            "parent run instead of executing it here, otherwise a retry would spawn "
            "a second child run",
            FailureClass.PERMANENT,
        )


@dataclass(frozen=True)
class ExecutorCoverage:
    """执行器表覆盖了哪些 `(executor_type, task_type)`（PR-21 / M25）。

    covered    有 handler，且 Worker **真能把它干完**的组合
    deferred   有 handler，但它的职责是**拒绝** —— 主人在别处
                （`DeferringExecutor`）。这类必须单独报：
                把它们算进 `covered` 就等于宣布"全覆盖"，
                而系统其实一件也跑不了 —— PR-24 同类的谎话。
    unrouted   `ACTION_TO_TASK` 能产生、但连 handler 都没有的组合
                （报错会指向错误的地方，PR-19）

    `owners` 给 `deferred` 里每一对配一句"主人是谁"，启动日志直接打出来。
    """

    covered: frozenset[tuple[str, str]] = frozenset()
    deferred: frozenset[tuple[str, str]] = frozenset()
    unrouted: frozenset[tuple[str, str]] = frozenset()
    owners: Mapping[tuple[str, str], str] = field(default_factory=dict)

    def covers(self, executor_type: str, task_type: TaskType | str) -> bool:
        return (executor_type, _task_type_value(task_type)) in self.covered

    def executable(self, executor_type: str, task_type: TaskType | str) -> bool:
        """Worker 真能干完它吗 —— `covered` 减去 `deferred`。

        这才是"系统现在能做什么"的答案。`covers()` 回答的是
        "派发过去会不会报错指错地方"，两个问题不是一回事。
        """
        pair = (executor_type, _task_type_value(task_type))
        return pair in self.covered and pair not in self.deferred

    def missing(self, task_types: Iterable[str]) -> frozenset[str]:
        """声明了要干、但表里没有 handler 的 task_type。"""
        covered_types = {tt for _, tt in self.covered}
        return frozenset(task_types) - covered_types


def executor_coverage(executors: Mapping[str, Any]) -> ExecutorCoverage:
    """读一张执行器表，算出覆盖情况。

    裸执行器（直接 `{"native": ToolCallExecutor(...)}`）**不算**覆盖任何组合：
    它只认 executor_type，不认 task_type，正是 PR-19 要消灭的形态。
    这里刻意不给它兜底 —— 兜底就等于承认"两个维度可以不对齐"。
    """
    covered: set[tuple[str, str]] = set()
    deferred: set[tuple[str, str]] = set()
    owners: dict[tuple[str, str], str] = {}
    for executor_type, executor in executors.items():
        if not isinstance(executor, TaskTypeRouter):
            continue
        for task_type in executor.task_types():
            pair = (executor_type, task_type)
            covered.add(pair)
            handler = executor.handler(task_type)
            if isinstance(handler, DeferringExecutor):
                deferred.add(pair)
                owners[pair] = handler.deferral_owner

    produced: set[tuple[str, str]] = set()
    for mapping in ACTION_TO_TASK.values():
        if mapping is None:
            continue
        task_type, executor_type = mapping
        produced.add((executor_type.value, _task_type_value(task_type)))

    return ExecutorCoverage(
        covered=frozenset(covered),
        deferred=frozenset(deferred),
        unrouted=frozenset(produced - covered),
        owners=dict(owners),
    )
