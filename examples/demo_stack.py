"""一份**能跑起来**的 ToolRuntime + ModelGateway（M23）。

--------------------------------------------------------------------------
它不是测试 fixture

`tests/` 里的 Fake 只活在测试进程里；本文件是给 `AGENTOS_TOOL_PROVIDER` /
`AGENTOS_MODEL_PROVIDER` 直接指向用的：

    AGENTOS_TOOL_PROVIDER=examples.demo_stack:build_tool_runtime
    AGENTOS_MODEL_PROVIDER=examples.demo_stack:build_model_gateway

于是 `python -m apps.worker` 走的是**和真实部署完全相同**的装载路径
（`importlib.import_module` + `getattr`），不是测试专用的旁路。
区别只在这里的模型是确定性的、工具是进程内的 —— 真实部署把这两个函数
换成接 HTTP / MCP 的版本，**其他一行都不用改**。

--------------------------------------------------------------------------
刻意的取舍

工具里有一个 `note.write`（`side_effect=WRITE`）。它存在的理由是 T-2：
WRITE 工具必须带 `idempotency_key`，否则 `ToolRuntime` 直接拒绝。
把它放进示例栈，"漏传去重键会被拒"这件事在**最浅的那条路上**就能被看到，
而不是只在单测里成立。
"""
from __future__ import annotations

from typing import Any, Mapping

from packages.agent_runtime.model_gateway import (
    CompletionRequest,
    CompletionResponse,
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    SideEffect,
    ToolRuntime,
    ToolSpec,
)
from packages.agent_runtime.tool_runtime import ToolRegistry as RuntimeToolRegistry


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _echo(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"echo": args.get("text", "")}


def _add(args: Mapping[str, Any]) -> Mapping[str, Any]:
    numbers = args.get("numbers") or []
    return {"sum": sum(float(n) for n in numbers)}


def _write_note(args: Mapping[str, Any], *, idempotency_key: str = "") -> Mapping[str, Any]:
    """一个 WRITE 工具。`idempotency_key` 由 ToolRuntime 注入（T-2）。"""
    return {
        "stored": args.get("text", ""),
        "idempotency_key": idempotency_key,
    }


def build_tool_runtime() -> ToolRuntime:
    """进程内工具表。真实部署换成 MCP / HTTP 工具网关，接口形状不变。"""
    registry = RuntimeToolRegistry()
    registry.register(
        ToolSpec(name="echo", version="1.0.0", description="回显一段文本",
                 side_effect=SideEffect.READ),
        FunctionInvoker(_echo),
    )
    registry.register(
        ToolSpec(name="add", version="1.0.0", description="求和",
                 side_effect=SideEffect.READ,
                 input_schema={"required": ["numbers"]}),
        FunctionInvoker(_add),
    )
    registry.register(
        ToolSpec(name="note.write", version="1.0.0", description="写一条笔记",
                 # WRITE：T-2 要求调用方给出去重键，否则 ToolRuntime 直接拒绝
                 side_effect=SideEffect.WRITE,
                 input_schema={"required": ["text"]}),
        FunctionInvoker(_write_note, pass_idempotency_key=True),
    )
    return ToolRuntime(registry)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

_DEMO_MODEL = Model(model_id="demo-1", name="demo")
_DEMO_DEPLOYMENT = Deployment(
    deployment_id="demo-1@in-process",
    model_id="demo-1",
    provider="demo",
    endpoint="in-process",
)


def _demo_complete(
    deployment: Deployment, request: CompletionRequest
) -> CompletionResponse:
    """确定性"模型"：把 prompt 原样折回去。

    它不假装自己会思考 —— 但它是**真的** `ModelGateway`，
    于是 G-1（Fallback 不产生新 Attempt）、G-5（调用轨迹）走的都是生产那条路。
    """
    text = f"demo-1 received {len(request.prompt)} chars"
    return ok_response(
        deployment,
        request,
        text=text,
        prompt_tokens=max(1, len(request.prompt) // 4),
        completion_tokens=max(1, len(text) // 4),
        metadata={"prompt": request.prompt},
    )


def build_model_gateway() -> ModelGateway:
    """单 Deployment 的 Gateway。真实部署把 `FunctionProvider` 换成 HTTP 适配器。

    `default_model_id` 是**必填**的，不是可选装饰（G-8）：

        缺省模型由 Gateway 决定，不由调用方决定。

    这里曾经漏掉它，后果是 `LLMCallExecutor` 拿着一个空的 `model_id`
    去调 Gateway，Gateway 抛 `NO_MODEL_REQUESTED`（PERMANENT）——
    于是**这个示例栈从来没有真的跑通过一次模型调用**，
    而它是 `AGENTOS_STACK_PROVIDER` 的默认实现。
    测试全绿，因为没人测"跑得通"，只测了"装得上"。
    """
    return ModelGateway(
        ModelRouter([_DEMO_MODEL], [_DEMO_DEPLOYMENT]),
        {"demo": FunctionProvider("demo", _demo_complete)},
        max_fallbacks=0,
        default_model_id=_DEMO_MODEL.model_id,
    )


# ---------------------------------------------------------------------------
# 智能体栈（M24）
# ---------------------------------------------------------------------------


class DemoInterpreter:
    """把用户请求变成一个 Goal。"""

    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Any:
        from packages.agent_domain.intelligence.goal import Budget, Goal

        return Goal(
            run_id=str(context.get("run_id", "")),
            objective=user_request,
            success_criteria=("an answer is produced",),
            budget=Budget(max_steps=5),
        )


class DemoPlanner:
    """两个节点：先问模型，再 FINISH。"""

    def plan(self, state: Any) -> Any:
        from packages.agent_domain.intelligence.plan import Plan, PlanNode

        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n1", name="ask-llm"),),
        )


class DemoDecisionEngine:
    """第一步问模型，之后收尾。

    它不假装会思考 —— 但它是**真的** DecisionEngine，
    于是 I-4（Decision 不可执行）、§44 的 `LLM → Decision → Action` 走的是生产那条路。

    `approval_at_step`（默认 0 = 不演示）：在第几步走一次 **HIGH 风险的
    WRITE 工具调用**。它会被 Harness 拦下来要求人审批 —— 注意这里演示的不是
    "智能体主动请示"（`ActionType.HUMAN_APPROVAL`），而是
    **"智能体没想请示，是治理层说这一步得有人签字"**（I-9）。
    后者才是企业级平台真正要保证的那条路：审批不靠智能体自觉。
    """

    def __init__(self, *, approval_at_step: int = 0) -> None:
        self.calls = 0
        self.approval_at_step = approval_at_step

    def decide(self, state: Any) -> Any:
        from packages.agent_domain.intelligence.action import (
            Action,
            ActionType,
            RiskLevel,
        )
        from packages.agent_domain.intelligence.decision import Decision

        self.calls += 1
        if self.calls == 1:
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.LLM_CALL,
                payload={"prompt": "hello"},
            )
        elif self.approval_at_step and self.calls == self.approval_at_step:
            # 一个会**在外部世界留状态**的动作（`note.write` 是 WRITE 工具）。
            # 工具自己去重键由 ExecutionContext 注入（T-2），引擎不操心。
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "note.write", "args": {"text": "hello from demo"}},
                risk_level=RiskLevel.HIGH,
            )
        else:
            action = Action(run_id=state.run_id, action_type=ActionType.FINISH)
        return Decision(run_id=state.run_id, selected_action=action, rationale="demo")


def build_stack_factory(
    config: Any = None,
    *,
    kernel: Any = None,
    clock: Any = None,
    child_registry: Any = None,
    snapshots: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    approval_at_step: int = 0,
) -> Any:
    """`AGENTOS_STACK_PROVIDER` 的实现：返回 `(agent_id, approvals) -> RuntimeStack`。

    走的是与真实部署相同的装载路径。真实部署把三个 Demo* 换成接 Intelligence
    的实现，把 `kernel` 换成 PG 版 —— 其余一行不动。

    `child_registry` 是 M26 加的：**派生登记处必须跨 Run 共享，且必须持久**。
    不传就退化成一个进程内的字典 —— 那是测试用的，不是生产的：
    它只在进程活着的时候守得住 D-1。

    `approval_at_step`：第几步走一次会被 Harness 拦下等审批的动作（默认 0 = 不走）。
    演示/页面要跑"含审批的完整流程"时传 2。

    `snapshots` / `compensations`（M29 / 空洞 213、214）：这两样和 `kernel`
    是同一类 —— `assemble_runtime_stack` 给它们都写了 `or InMemory...()` 兜底，
    于是漏掉不报错，只是永不落库。组合根现在**要求** provider 收下它们。

    `cancellations`（M34 / 空洞 222）：Run 级取消意图的存储。
    它和上面三样的形状不同 —— `assemble_runtime_stack` **没有**给它内存兜底，
    所以漏掉不会"退化成内存"，而是**根本没有通道**：跨进程的取消请求
    写不进任何地方，父 Run 只能把登记处那一行判死，子 Run 照跑。
    """
    from packages.agent_runtime.assembly import assemble_runtime_stack
    from packages.agent_runtime.delegation import (
        ChildRunRegistry,
        InProcessChildRunSpawner,
    )

    gateway = build_model_gateway()
    tool_runtime = build_tool_runtime()
    interpreter = DemoInterpreter()
    planner = DemoPlanner()
    # 共享一份：登记处是**跨 Run 的账本**，不是某个 Run 的私有状态
    # （与 approvals / compensations 同一条判据）。
    registry = child_registry if child_registry is not None else ChildRunRegistry()

    def make_stack(agent_id: str, approvals: Any) -> Any:
        # 子 Run 派生器：它的 factory 就是**这个** make_stack。
        #
        # 起一条子 Run 的方式必须与起父 Run 完全一致 ——
        # 另写一个"子 Run 专用装配"就多了一份"Run 是怎么开始的"的定义。
        # 这里不是递归调用：`spawner` 只在真的要派生时才去调 factory，
        # 而每一层都只是把同一个 `make_stack` 再包一次（惰性，不是展开）。
        spawner = InProcessChildRunSpawner(
            factory=make_stack, approvals=approvals, registry=registry
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=interpreter,
            planner=planner,
            # 每个 Run 一个 DecisionEngine：它是**有状态的**（第几步了），
            # 跨 Run 共享会让第二个 Run 直接 FINISH。
            decision_engine=DemoDecisionEngine(approval_at_step=approval_at_step),
            gateway=gateway,
            tool_runtime=tool_runtime,
            kernel=kernel,
            clock=clock,
            approval_store=approvals,
            snapshots=snapshots,
            compensations=compensations,
            cancellations=cancellations,
            spawner=spawner,
        )

    return make_stack


def build_approval_demo_stack_factory(
    config: Any = None,
    *,
    child_registry: Any = None,
    kernel: Any = None,
    snapshots: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
) -> Any:
    """`AGENTOS_STACK_PROVIDER` 的**带审批**变体：第 2 步会被治理层拦下。

    为什么要单独一个入口而不是把它设成默认：

    审批这条路由**治理层**触发（`risk_level >= HIGH` → `REQUIRE_APPROVAL`），
    不由智能体决定。默认栈里所有动作都是 LOW 风险，于是"人在环"这条最该被
    看见的路，恰好是演示里唯一走不到的那条 —— 而界面上没有任何东西提示你
    它没走到。

    所以它必须被**显式打开**：`approval_at_step=2`。

    `child_registry` 必须照样接住（D-1）：演示栈也要能活过重启，
    否则"重试不会开出第二条子 Run"这条在演示里就是假的。

    `kernel` 也必须接住（M29 / 空洞 212）：不接的话栈会自己 new 一个
    **内存** Kernel，于是这条 Run 跑出来的 Execution 一行都不落库 ——
    演示界面看着走完了三步，`executions` 表却是空的。
    演示栈尤其不能这么演示：它是别人照着抄的那一份。
    """
    return build_stack_factory(
        config,
        approval_at_step=2,
        child_registry=child_registry,
        kernel=kernel,
        snapshots=snapshots,
        compensations=compensations,
        cancellations=cancellations,
    )


__all__ = [
    "build_approval_demo_stack_factory",
    "build_model_gateway",
    "build_stack_factory",
    "build_tool_runtime",
]
