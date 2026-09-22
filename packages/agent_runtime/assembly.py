"""§44 第一条 End-to-End 闭环的**唯一装配点**。

```text
POST /agents/{agent_id}/runs      ← apps/api
        ↓
assemble_runtime_stack(...)       ← 本文件
        ↓
AgentRun → Step → Task → Execution → Worker → LLM → Decision
        → Action → Tool → Observation → State → Decision → Finish
        ↓
AgentRun = COMPLETED
```

**为什么要有这个文件，而不是让每个调用方自己拼：**

拼装的每一步都有一个"看起来也行"的错误版本：

    · Worker 的 Executor 用 `legacy_gateway(FakeLLM)` 造一个，
      Loop 上再挂一个真的 `ModelGateway` → 两条代码路径（违反 L-2）
    · Harness 用默认 Budget，Loop 的 max_steps 另配一份 →
      两个"步数上限"，跑起来不知道是谁先拦的
    · Clock 各自 new 一个 → 审批超时和 Lease 超时对不上

所以装配只在这里发生一次，并且这几处**共享同一个对象**是可以被断言的。

L-2  Worker 的 Executor 与 Loop 持有的 Gateway / ToolRuntime 必须是同一批对象。
     这不是整洁问题：一旦出现第二条路径，"Fallback 不产生新 Attempt"（G-1）
     和"WRITE 工具必须带去重键"（T-2）在没走到的那条路上根本没被验证过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping

from packages.agent_context.assembler import ContextAssembler
from packages.agent_domain.execution import ExecutorType
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.goal import GoalInterpreter
from packages.agent_domain.intelligence.state import State
from packages.agent_harness.approval import ApprovalStore
from packages.agent_harness.cost import Budget
from packages.agent_harness.harness import Harness
from packages.execution_kernel.inmemory import (
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    InMemoryTaskRepository,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.ports import Clock
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import Worker, WorkerConfig

from .cancellation import RunCancellationStore
from .executors import (
    AgentDelegationExecutor,
    ApprovalGateExecutor,
    LLMCallExecutor,
    SkillExecutor,
    TaskTypeRouter,
    ToolCallExecutor,
)
from .loop import AgentLoop, AgentLoopConfig
from .model_gateway import ModelGateway
from .ports import DecisionEngine, Planner
from .recovery import InMemoryRunSnapshotStore, RunSnapshotStore
from .saga import CompensationStore, InMemoryCompensationStore
from .tool_runtime import ToolRuntime
from .trace import RunTrace


@dataclass
class RuntimeStack:
    """一个 Run 的全部零件。

    `start()` / `run()` 只是转发给 Loop —— 这里**不复制**任何逻辑，
    否则"闭环怎么跑"就有了第二个定义。
    """

    kernel: ExecutionKernel
    scheduler: Scheduler
    worker: Worker
    loop: AgentLoop
    gateway: ModelGateway
    tool_runtime: ToolRuntime
    trace: RunTrace
    clock: Clock

    @property
    def run_id(self) -> str:
        return self.trace.run_id

    def start(self, user_request: str, run_id: str | None = None) -> State:
        return self.loop.start(user_request, run_id=run_id)

    def run(self) -> State:
        return self.loop.run()


def assemble_runtime_stack(
    *,
    agent_id: str = "agent-default",
    interpreter: GoalInterpreter,
    planner: Planner,
    decision_engine: DecisionEngine,
    gateway: ModelGateway,
    tool_runtime: ToolRuntime,
    kernel: ExecutionKernel | None = None,
    clock: Clock | None = None,
    harness: Harness | None = None,
    context_assembler: ContextAssembler | None = None,
    budget: Budget | None = None,
    approval_store: ApprovalStore | None = None,
    snapshots: RunSnapshotStore | None = None,
    compensations: CompensationStore | None = None,
    #: M25：子 Run 派生器。为 `None` 就不能派生 ——
    #: `SKILL_CALL` / `AGENT_DELEGATION` 会落到 Worker 上并被点名拒绝。
    #: 这里不给默认实现：起一条 Run 只有组合根知道怎么起。
    spawner: Any = None,
    #: M34 / 空洞 222：Run 级取消意图的存储。**必须持久** ——
    #: 跨进程取消的全部意义就是写到另一个进程看得到的地方，
    #: 所以这里刻意**不给**内存兜底：给了就等于两个进程各有一份，
    #: 看上去装上了，实际上还是没有通道（与 212~214 同款）。
    cancellations: RunCancellationStore | None = None,
    max_steps: int = 10,
    worker_id: str = "worker-1",
    lease_ttl: timedelta = timedelta(seconds=30),
    trace: RunTrace | None = None,
) -> RuntimeStack:
    """把 §44 那条链从头到尾接起来。

    注意 `max_steps` 与 `budget.max_steps`：
    Goal 的 budget 优先（它是 Goal 的一部分，不是调度参数），
    这里传的是**兜底值**。两者不一致时不抛错 —— 取小的那个才对，
    但那属于 Goal 的语义，不该由装配层裁决，所以这里只把 Budget 传下去。
    """
    clock = clock or ManualClock()
    # M40：内存栈也必须有**一个**事件落点，而且账本与 Kernel 共用它。
    #
    # 不共用就等于"内存里跑一遍，账本的变化一条事件都没有"，而 PG 栈有 ——
    # 于是"过了单测"的行为和真库上的行为又不一样了（替身比真的松）。
    memory_outbox = InMemoryOutbox()
    kernel = kernel or ExecutionKernel(
        repository=InMemoryExecutionRepository(),
        attempts=InMemoryAttemptRepository(),
        outbox=memory_outbox,
        clock=clock,
        # E-26：内存栈也要走 TaskRepository，否则"内存里跑得好好的"
        # 和"换了 PG 之后"是两条不同的代码路径 ——
        # 而差别只在重启之后才暴露（空洞 215 的形状）。
        tasks=InMemoryTaskRepository(),
    )
    scheduler = Scheduler(kernel)

    # L-2：Executor 由**传入的** gateway / tool_runtime 构造 ——
    # 装配函数里没有第二个可以造 Executor 的地方，于是"单一代码路径"
    # 不是靠自觉，是靠没有别的选择。
    worker = Worker(
        kernel=kernel,
        scheduler=scheduler,
        # PR-19：两级分派。`executor_type` 是传输（Kernel 的分派维度），
        # `task_type` 是语义（Runtime 的分派维度）。
        # 之前这里写的是 `NATIVE: ToolCallExecutor` / `HTTP: LLMCallExecutor` ——
        # 那两个维度**碰巧**对上了，于是 native:human_approval（审批闸门）
        # 会被 ToolCallExecutor 以 `BAD_PAYLOAD: payload.tool is required` 拒掉，
        # 一句既不对又误导的话。
        executors={
            ExecutorType.NATIVE.value: TaskTypeRouter(
                {
                    TaskType.TOOL_CALL: ToolCallExecutor(tool_runtime),
                    TaskType.HUMAN_APPROVAL: ApprovalGateExecutor(),
                    # M26：默认装配也要有安全网。
                    #
                    # 之前这里只挂了 TOOL_CALL / HUMAN_APPROVAL / LLM_CALL，
                    # 于是 `spawner=None` 时 SKILL_CALL 会以
                    # `EXECUTOR_NOT_FOUND`（PERMANENT）失败 ——
                    # 一句既不对又误导的话：真实原因是**没有派生器**，
                    # 而"执行器没找到"会让人以为该去补一个执行器。
                    # 与 PR-19 那次（审批闸门被 ToolCallExecutor 误拒）是同一类错。
                    TaskType.SKILL: SkillExecutor(),
                },
                executor_type=ExecutorType.NATIVE.value,
            ),
            ExecutorType.HTTP.value: TaskTypeRouter(
                {TaskType.LLM_CALL: LLMCallExecutor(gateway)},
                executor_type=ExecutorType.HTTP.value,
            ),
            ExecutorType.AGENT_RUNTIME.value: TaskTypeRouter(
                {TaskType.AGENT_DELEGATION: AgentDelegationExecutor()},
                executor_type=ExecutorType.AGENT_RUNTIME.value,
            ),
        },
        config=WorkerConfig(
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            heartbeat_interval=timedelta(seconds=max(1, lease_ttl.seconds // 3)),
        ),
    )

    if harness is None:
        # A-10：审批存储是**跨 Run 共享**的（它不是某个 Run 的私有状态）。
        # 不传就退化为进程内存储 —— 那是测试用的，不是生产的。
        harness = Harness.default(
            budget=budget or Budget(),
            clock=clock,
            approval_store=approval_store,
        )

    # ⚠️ 不能写 `trace or RunTrace(...)` —— RunTrace 有 `__len__`，
    # 空账本是 falsy，于是调用方传进来的那个会被悄悄换掉。
    # （这是阶段 11 装配时真实踩到的一次：Trace 一直是空的，
    #   因为断言查的是调用方手上那个引用。）
    if trace is None:
        trace = RunTrace(clock=clock)

    loop = AgentLoop(
        kernel=kernel,
        worker=worker,
        interpreter=interpreter,
        planner=planner,
        decision_engine=decision_engine,
        config=AgentLoopConfig(agent_id=agent_id, max_steps=max_steps),
        harness=harness,
        model_gateway=gateway,
        tool_runtime=tool_runtime,
        context_assembler=context_assembler,
        trace=trace,
        # R-1：快照存储必须与 `RunRecovery` 用的是同一份（写在这里，读在那边）
        snapshots=snapshots or InMemoryRunSnapshotStore(),
        # S-1：补偿账本同理 —— 跨 Run 共享，且**必须持久**（A-12：丢了变错）
        # X-15：它的每一次变化都要有事件，所以连事件落点一起给。
        compensations=compensations or InMemoryCompensationStore(events=memory_outbox),
        spawner=spawner,
        cancellations=cancellations,
    )
    return RuntimeStack(
        kernel=kernel,
        scheduler=scheduler,
        worker=worker,
        loop=loop,
        gateway=gateway,
        tool_runtime=tool_runtime,
        trace=trace,
        clock=clock,
    )
