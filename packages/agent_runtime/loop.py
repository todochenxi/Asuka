"""Agent Loop：Runtime 的心脏。

它**驱动**，但不替 Intelligence 决定，也不替 Kernel 管生命周期：

    ┌─ Intelligence ──┐        ┌─ Runtime ──┐        ┌─ Kernel ──┐
    │  Plan / Decision│───────►│  AgentLoop │───────►│ Execution │
    └─────────────────┘        └────────────┘        └───────────┘
                                     ▲                      │
                                     └──── Observation ─────┘
                                        （经 Reducer 进 State）

关键约束：

    I-1   Goal 必须由 Interpreter 解释，不能用原始 user_request 当 Goal
    I-2   Goal 必须带 success_criteria
    I-4   Decision 不可执行 —— 只有 Action → TaskFactory → Task → Kernel 才能产生副作用
    I-7   大对象走 Artifact，不内联进 Observation
    I-9   confidence_signal 再高也不能自动执行高风险动作（它只是 signal，不是概率）
    X-1   造出 Task 交给 Kernel 即交棒，Loop 不再触碰执行生命周期
    X-10  同一 run 的 State 写入必须串行化（这里用 expected_version 校验）

失败不是终点：执行失败也会变成 Observation 进 State —— 因为**失败也是事实**，
Agent 要靠它来决定下一步（Replan / 换 Tool / 放弃）。

阶段 11（完整 Agent Loop）补上的三件事：

    L-1  Loop 不自己实现模型/工具调用 —— 它只持有 ModelGateway / ToolRuntime
    L-3  Token 用量必须回到 Harness 的 CostManager（成本是拦截条件，不是账单）
    L-5  Run Checkpoint 在 **Step 完成**时也要写（§14），不只是挂起前
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Mapping

from packages.agent_domain.business import AgentRun, AgentRunStatus, Step, StepStatus
from packages.agent_domain.business.snapshot import (
    RunSnapshot,
    state_from_dict,
    state_to_dict,
    step_from_dict,
    step_to_dict,
)
from packages.agent_domain.errors import IllegalTransition, InvariantViolation
from packages.agent_domain.events.event import (
    CHILD_RUN_CANCELLED,
    CHILD_RUN_COMPLETED,
    CHILD_RUN_FAILED,
    new_event,
)
from packages.agent_domain.execution import (
    ErrorInfo,
    ExecutionStatus,
    FailureClass,
    RunCheckpoint,
)
from packages.agent_domain.execution.execution import (
    TERMINAL_EXECUTION_STATUSES,
    SuspensionReason,
)
from packages.agent_domain.ids import new_run_id
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import ActionResolver
from packages.agent_domain.intelligence.goal import GoalInterpreter, interpret_goal
from packages.agent_domain.intelligence.observation import Observation
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_domain.intelligence.state import State
from packages.agent_harness.approval import ApprovalRequest, ApprovalStatus, HumanLoop
from packages.agent_harness.harness import Harness
from packages.agent_context.assembler import ContextAssembler, ContextRequest
from packages.agent_harness.policy import PolicyContext, Verdict
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.worker import Worker, WorkerOutcome

from .cancellation import RunCancellation, RunCancellationStore
from .checkpoints import InMemoryRunCheckpointStore, RunCheckpointStore, build_run_checkpoint
from .recovery import (
    InMemoryRunSnapshotStore,
    RunSnapshotStore,
    trace_entries_to_dicts,
    trace_entry_from_dict,
)
from .model_gateway import ModelGateway
from .ports import DecisionEngine, Planner
from .tool_runtime import ToolRuntime
from .trace import (
    APPROVAL,
    APPROVED,
    CHECKPOINT,
    CONTEXT,
    FINISHED,
    OBSERVED,
    COMPENSATION_DEFERRED,
    COMPENSATION_DONE,
    COMPENSATION_FINISHED,
    COMPENSATION_RECORDED,
    COMPENSATION_RELEASED,
    COMPENSATION_STARTED,
    COMPENSATION_UNRESOLVED,
    CANCELLED,
    RECOVERED,
    SNAPSHOT,
    SUBMITTED,
    RunTrace,
    served_from_result,
)
from .reducer import (
    APPROVAL_GRANTED,
    APPROVAL_REJECTED,
    APPROVAL_REQUESTED,
    CHILD_RUN_FINISHED,
    CHILD_RUN_SPAWNED,
    CHILD_RUN_UNKNOWN,
    EXECUTION_FAILED,
    EXECUTION_RESULT,
    EXECUTION_UNRESOLVED,
    PLAN_CREATED,
    PLAN_INVALIDATED,
    POLICY_DENIED,
    RUN_CANCELLED,
    RUN_FINISHED,
    RuntimeReducer,
)
from .delegation import (
    ChildRunHandle,
    ChildRunIdentity,
    ChildRunKind,
    ChildRunRequest,
    ChildRunSpawner,
    child_run_kind_of,
)
from .saga import CompensationStore, InMemoryCompensationStore, SagaCoordinator
from .task_factory import TaskFactory


class StepOutcome(str, Enum):
    PLANNED = "planned"
    REPLANNED = "replanned"
    EXECUTED = "executed"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    #: M25：等子 Run。与 WAITING_APPROVAL 并列 —— 两者都是"这一步没干完"，
    #: 且都要在 Kernel 里留下一条 SUSPENDED 的 Execution。
    WAITING_CHILD = "waiting_child"
    FINISHED = "finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DENIED = "denied"                # Harness 拒绝：没产生 Task，直接回 Observation
    DENY_LOOP = "deny_loop"          # L-7：连续被拒到上限 → Run 判定 FAILED
    APPROVAL_EXPIRED = "approval_expired"
    #: M33 / B-8：被叫停。它必须是一个**独立**的 outcome，不能复用 `FINISHED`
    #: 也不能复用 `FAILED` —— 三种"停"在页面上是同一副样子正是 F-1 要治的病。
    CANCELLED = "cancelled"


#: D-9 / D-10：子 Run 的两个**不成功**终态，在父侧的处置方式。
#:
#: 刻意做成一张表而不是散在 if/else 里："这个终态要不要重试"这件事
#: 只有一处（B-7）。将来多一个终态就加一行，而不是再加一个分支。
#:
#: 两个 FailureClass 为什么不同：
#:   failed    → PERMANENT。子 Run 自己的 Kernel 与重试预算已经判过了，
#:               父侧再判一次不会得到不同的答案（D-9）。
#:   cancelled → POLICY_DENIED。取消不是"没做成"，是"有人决定到此为止"
#:               （S-15）。把它算进失败会让人去找"它为什么失败"，
#:               而正确的问题是"谁取消了它"。
CHILD_OUTCOME_FAILURE: dict[str, tuple[str, FailureClass]] = {
    "failed": ("CHILD_RUN_FAILED", FailureClass.PERMANENT),
    "cancelled": ("CHILD_RUN_CANCELLED", FailureClass.POLICY_DENIED),
    # D-19 / 空洞 229：等到上限也没有结果。
    #
    # 为什么是 EXTERNAL_UNKNOWN 而不是 PERMANENT：
    # PERMANENT 说的是"这件事做不成"，而我们**不知道**它做没做成 ——
    # 那条子 Run 可能还在跑、可能马上就交回结果。
    # 写 PERMANENT 会让排障的人去找"它为什么失败"（PR-19 同款），
    # 而真实答案是"没人知道它在哪"。
    #
    # 两者都**不可重试**，所以 D-9（委派不可重试）照样成立：
    # 重试拿回的是同一条子 Run（D-1），第二次不会得到不同的答案。
    "unknown": ("CHILD_WAIT_EXPIRED", FailureClass.EXTERNAL_UNKNOWN),
}


def _child_verb(outcome: str) -> str:
    """`failed` / `cancelled` 直接就是谓语，`unknown` 不是。

    "child agent run X unknown" 读起来像"X 是个未知数"；
    真实要说的是"X 在等待上限之前没有交回任何结果"。
    措辞不是排版问题：这行 summary 会进 State、进模型上下文，
    而模型要靠它决定"换条路走"还是"先查那条子 Run 还在不在"（PR-19）。
    """
    return (
        "produced no result before its wait deadline"
        if outcome == "unknown"
        else outcome
    )


#: I-18：这个运行时**真的能执行**的 `PlanNode.kind` 集合。
#:
#: 现在只有 `TASK`，这不是"还没做完"，而是**事实**：
#: `_ensure_step()` 把节点变成 Step 之后，"这一步到底做什么"由
#: `DecisionEngine.decide()` 决定 —— 运行时**没有**任何按 kind 分派的机制。
#:
#: 于是 `kind="human"` 的节点不会去要人的签字，`kind="agent"` 的节点
#: 不会派生子 Run。探针实测（`probe88.py`）：
#:
#:     kind='human'   ->  step() 序列 ['executed', 'finished']
#:                       Run 终态 completed
#:                       approval.requested 条数 0      ← 没有任何人签过字
#:
#:     kind='agent'   ->  Run 终态 completed
#:                       子 Run 条数 0                  ← 委派从未发生
#:
#: 一份**计划声明了一件运行时做不到的事**，而运行时把它当普通 task 跑完、
#: 报 COMPLETED —— 这不是"少了个功能"，是**系统主动说了假话**
#: （与 `SkillExecutor` 那条 `SKILL_NOT_WORKER_EXECUTABLE` 同一族病）。
#:
#: 处置：**在执行这份计划的第一步之前**就带着点名理由拒绝。
#: 不许执行它（那是编造"我做到了"），不许跳过它（那是编造"它做过了"）。
#:
#: M12 落地真正的按 kind 分派时，这个集合随之扩大 ——
#: 扩大的那一刻，"声明"与"能力"重新对齐，拒绝自动消失。
SUPPORTED_PLAN_NODE_KINDS: frozenset[PlanNodeKind] = frozenset({PlanNodeKind.TASK})


def _unsupported_plan_nodes(plan: Plan) -> list[PlanNode]:
    """这份计划里，运行时**执行不了**的节点（按计划顺序）。

    ⚠️ 查的是**整份计划**，不是"下一个要跑的节点"。
    只查下一个的话，一份 5 个好节点 + 1 个 `kind='human'` 的计划会先跑完
    前 5 个（**副作用已经发生了**），然后才失败 ——
    而这份计划从一开始就不可能被完整执行。
    "宁可拒绝"的意思正是：**在产生任何副作用之前**拒绝。
    """
    return [n for n in plan.nodes if n.kind not in SUPPORTED_PLAN_NODE_KINDS]


def _unsupported_kinds_reason(offenders: list[PlanNode]) -> str:
    """把"执行不了"说成一句运维能照着办的话（PR-19：说中真发生了什么）。

    要点齐四样：**哪个节点**、**它声明了什么**、**运行时支持什么**、
    **为什么不能凑合**。少最后一样的话，读的人会以为"当 task 跑"是个
    合理的降级 —— 而那恰恰是 M88 要消灭的那条路。
    """
    supported = sorted(k.value for k in SUPPORTED_PLAN_NODE_KINDS)
    declared = ", ".join(f"{n.node_id}={n.kind.value!r}" for n in offenders)
    return (
        f"plan declares node kind(s) this runtime cannot execute: {declared}; "
        f"supported kinds are {supported} — this runtime has no per-kind "
        f"dispatch, so running such a node as an ordinary task would fabricate "
        f"the behaviour its kind declares (I-18)"
    )


def _plan_shape(plan: Plan) -> tuple:
    """一份计划的**形状**（I-12 的比较依据）。

    刻意**不**比 `plan_id`：它是 `new_id()`，每次必然不同。
    比对象或者比 id，等于宣布"每次重规划都换了一份计划" ——
    而实际上节点一个没变，那条路还是那条路。

    也不比 `metadata`：那是给 Planner 自己带的东西，
    不是"走哪条路"的一部分。
    """
    return (
        tuple((n.node_id, n.name, n.kind) for n in plan.nodes),
        tuple(plan.constraints),
    )


@dataclass
class AgentLoopConfig:
    agent_id: str = "agent-default"
    """B-1：AgentRun 必须能说清自己跑的是哪个 Agent。"""
    max_steps: int = 10
    require_approval_for: frozenset[RiskLevel] = frozenset({RiskLevel.HIGH})
    """必须过 Harness 审批的风险等级。

    I-9：`confidence_signal > 0.9` **不能**换来自动执行 ——
    它只是一个 signal（模型自评），不是校准过的概率。
    高风险动作必须由人或策略放行，Loop 自己说了不算。

    这个集合只在**没有显式传入 harness** 时用于构造默认 Harness；
    一旦传入了 harness，策略以 harness 为准（避免两处配置打架）。
    """
    max_consecutive_denials: int = 3
    """L-7：连续被 Harness 拒绝多少次就判定 Run FAILED。

    **没有这条，一个永远被拒的 Run 会永远转下去。**

    被拒不计入步数预算（`steps` 只数真正执行过的动作），所以
    "Agent 反复提出一个被禁的动作" 这个循环**不会**被预算拦住 ——
    它既不花钱也不消耗步数，只是每轮多一条 DENY 的 Observation。
    阶段 11 装配 E2E 时真的撞上了（把 max_tokens 压到 1 之后进程直接跑飞）。

    为什么是"连续"而不是"累计"：
    一次被拒之后换个动作是**正常的收敛行为**，不该受罚；
    一直不换才是死循环。所以只要中间有一次非 DENIED 的结果就清零。
    """
    approval_timeout: timedelta = timedelta(minutes=30)
    """审批的截止时间（H-5 / I-8）：没有它，一个人就能把 Run 永久挂住。

    与 `require_approval_for` 一样，只在**没有显式传入 harness** 时生效；
    传入了 harness 就以 `harness.approvals.default_ttl` 为准。
    真正的单一事实源是那条 `ApprovalRequest.expires_at` ——
    闸门 Action 的 I-8 timeout 也是从它推出来的，两处不可能对不上。
    """


@dataclass
class AgentLoop:
    kernel: ExecutionKernel
    worker: Worker
    interpreter: GoalInterpreter
    planner: Planner
    decision_engine: DecisionEngine
    task_factory: TaskFactory = field(default_factory=TaskFactory)
    reducer: RuntimeReducer = field(default_factory=RuntimeReducer)
    config: AgentLoopConfig = field(default_factory=AgentLoopConfig)
    harness: Harness | None = None

    #: L-1：Loop **持有**运行时，但不**实现**运行时。
    #: 它只认这两个端口，不认识 provider / invoker / endpoint。
    #: 真实装配走 `assembly.assemble_runtime_stack()` —— 那里保证
    #: Worker 的 Executor 和这两个对象是**同一个**（L-2：单一代码路径）。
    model_gateway: ModelGateway | None = None
    tool_runtime: ToolRuntime | None = None

    #: M17：Context 组装器（C-11 —— **组装归 Runtime**）。
    #: 只有 LLM_CALL 会用到它；没有配置就按老样子只发 prompt。
    context_assembler: ContextAssembler | None = None

    #: §44 六要素之一。Trace 不进 State —— 见 trace.py 的理由。
    trace: RunTrace = field(default_factory=RunTrace)

    #: Business Domain（基线 §3.1）：Loop 现在真的有一个 Run 了。
    #: 字段名不能叫 `run` —— `AgentLoop.run()` 是"跑到终态"的方法，
    #: 同名的实例属性会在 `start()` 之后把它整个盖掉（'AgentRun' object is not callable）。
    agent_run: AgentRun | None = None
    checkpoints: RunCheckpointStore = field(default_factory=InMemoryRunCheckpointStore)
    #: M20 / R-1：可恢复快照的存储。**必须持久** —— 快照存在的唯一理由
    #: 就是活过进程重启，放内存里等于没写（默认只是为了让单测能跑）。
    snapshots: RunSnapshotStore = field(default_factory=InMemoryRunSnapshotStore)
    #: M10 / S-1：补偿账本。**必须持久**（A-12：丢了变错，不是变慢）——
    #: 账本没了 = 副作用还在但没人知道要撤销。默认内存版只为了让单测能跑。
    compensations: CompensationStore = field(default_factory=InMemoryCompensationStore)
    #: M10 / S-1：Saga 协调器。为 `None` 时按 `compensations` 现场构造 ——
    #: 这样"换掉存储"只需要改一处，不会出现"Loop 用 PG、Saga 用内存"的错配。
    saga: SagaCoordinator | None = None
    #: M34 / 空洞 222：**Run 级**取消意图的存储。跨进程取消的唯一通道。
    #:
    #: 为 `None` 时安全点与 Sweeper 全部空转 —— 那正是 M33 结束时的状态：
    #: 进程内取消走的是对象引用，跨进程只有登记处那一行被判死。
    #: 生产必须由组合根接上 PG 版（`_REQUIRED_EXTRAS["cancellations"]`），
    #: 放内存里等于两个进程各有一份，那就还是没有通道。
    cancellations: RunCancellationStore | None = None

    #: M25：子 Run 派生器。**没有它就不能派生** ——
    #: `SKILL_CALL` / `AGENT_DELEGATION` 会退化成派发给 Worker，
    #: 然后被安全网执行器点名拒绝（"拥有你的 Loop 不在了"），而不是静默失败。
    #: 刻意不给默认实现：起一个 Run 只有一种方式（组合根的 factory），
    #: 给 Loop 造一个私有的就等于有了第二种。
    spawner: ChildRunSpawner | None = None
    #: 当前挂起等待的子 Run。真正的事实源是 Kernel 里那条 SUSPENDED Execution 的
    #: `wait_condition`，这里只是**快查指针** —— 与 `pending_approval` 同构。
    pending_child: ChildRunHandle | None = None
    #: 挂起时那件 Task 的 **id**（不是对象）。补偿登记要它（S-1）。
    #: 为什么是 id 而不是对象：恢复出来的父 Run 只有子 Run 的 handle，
    #: 手上没有 Task 对象；存 id 是唯一能跨重启带过去的东西。
    pending_child_task_id: str = ""

    #: M30：**我自己**是不是一条子 Run。由 `spawner.spawn()` 在派生时写入
    #: （见 `InProcessChildRunSpawner.spawn`）。
    #:
    #: 为什么不能靠"子 Run 的栈里有没有 spawner"来推断：
    #: 那等于把"这条 Run 能不能宣布自己完成"交给**每份 provider 工厂**去接。
    #: 漏接的后果和空洞 212~214 一模一样 —— 不报错，只是事件永远不发，
    #: 于是父 Run 永远等一个不会来的结果。
    #:
    #: 显式身份还有一个好处：`child_run.completed` 的 payload 需要
    #: parent_run_id / parent_execution_id / kind / target，
    #: 而它们**本来就在** handle 里 —— 不用再去查一次登记处。
    #:
    #: 为什么是"身份"而不是光一个 handle：结果要有地方**写**（X-5）。
    #: 见 `ChildRunIdentity` 的说明 —— 少了登记处，子 Run 跑到终态时
    #: 结果无处落库，只能靠事件捎出去，而事件会被 retention 吃掉。
    child_identity: ChildRunIdentity | None = None

    state: State | None = None
    steps: int = 0              # 已执行的动作数（预算计数）
    #: 业务 Step（Plan Node 的运行实例），与上面的计数器不是一回事
    steps_of_run: list[Step] = field(default_factory=list)
    current_step: Step | None = None
    #: S-12：补偿专用 Step 的 id（撤销动作挂在这里，不挂原 Step）
    _compensation_step_id: str = field(default="", repr=False)
    #: 连续被拒计数（L-7）；任何一次非 DENIED 的结果都会把它清零
    consecutive_denials: int = 0
    #: F-1：最后一步的结果。由 Runtime 自己记，终态声明权不外包（B-7）。
    #: `run()` 一路跑完之后它停在最后一次的结果上，于是"为什么停"
    #: 是一个可查的事实，不是调用方猜出来的。
    last_outcome: StepOutcome | None = None
    #: 兼容字段：被挂起的原始 Action
    pending_action: Action | None = None
    #: 真正的事实：Harness 的审批请求（含 Kernel 里那条 SUSPENDED 的 execution_id）
    pending_approval: ApprovalRequest | None = None
    history: list[StepOutcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.harness is None:
            # 只在没有显式 harness 时才用 config 构造默认 Harness。
            # 注意把 approval_timeout 传下去 —— 否则 HumanLoop 的 default_ttl
            # 会变成第二个"审批截止时间"旋钮，两处不一致时无从判断谁生效。
            self.harness = Harness.default(
                require_approval_for=self.config.require_approval_for,
                approval_ttl=self.config.approval_timeout,
            )
        if self.saga is None:
            self.saga = SagaCoordinator(store=self.compensations)

    @property
    def approvals(self) -> HumanLoop:
        assert self.harness is not None
        return self.harness.approvals

    # ------------------------------------------------------------ 启动
    def start(self, user_request: str, run_id: str | None = None) -> State:
        """I-1：Goal 必须由 Interpreter 解释，不能用原始请求当 Goal。

        同时创建 `AgentRun` —— 在此之前 Loop 只有一个 `run_id` 字符串，
        基线 §3.1 的 Business Domain 在代码里是没有落点的。
        """
        run_id = run_id or new_run_id()
        goal = interpret_goal(user_request, self.interpreter, run_id=run_id)
        self.trace.run_id = run_id
        self.state = State(run_id=run_id, goal=goal)
        self.agent_run = AgentRun(
            run_id=run_id,
            agent_id=self.config.agent_id,
            goal=goal,
            state=self.state,
        )
        return self.state

    def run(self) -> State:
        """跑到终态为止（FINISHED / BUDGET_EXHAUSTED / 等待审批 / 等待子 Run）。

        ⚠️ 停止条件有两半，缺一不可：

            ① 这一步的结果属于"**现在该停了**"那一类（下面那张表）
            ② 或者**这条 Run 已经到了终态**（`TERMINAL_RUN_STATUSES`）

        此前只有 ①。漏掉 ② 的后果（probe87 场景 3 实测，**不需要任何变异**）：

            重规划换不出形状不同的计划 -> `_plan()` 返回 False
            -> `_declare_terminal(FAILED)` -> `step()` 从此每次都返回 FAILED
            -> `steps` 不再增长（FAILED 那条路不吃预算）
            -> `steps >= budget` 永远不成立 -> **`run()` 永远转下去**

        没有报错，也没有尽头 —— 而 L-7 自己写下的正是这句话：
        "一个永远被拒的 Run 会永远空转，且没有任何报错。"
        L-7 把 `DENY_LOOP` 加进了那张表，却没看见**判据本身选错了对象**。

        两张表说的是两件事，不能互相代替：

            StepOutcome 那张表 = "**这一步**现在干不完 / 不该再干"
            Run 的终态         = "**这条 Run**结束了"

        它们不重合的地方就是空转。最清楚的一处：`FAILED` 作为**这一步的结果**
        通常不是终点（失败是事实，Agent 要据此换计划，I-11 就是这么设计的），
        所以它**不该**进那张表；但作为**这条 Run 的终态**，它必须让 `run()` 停。
        用前者代替后者，就必然在某个角落转不完。
        """
        assert self.state is not None, "call start() first"
        while True:
            outcome = self.step()
            if outcome in (
                StepOutcome.FINISHED,
                StepOutcome.BUDGET_EXHAUSTED,
                StepOutcome.WAITING_APPROVAL,
                # D-5：等子 Run 与等审批**同等地位** —— 两者都是"这一步现在干不完"。
                # M25 漏了它，于是 `run()` 会在子 Run 还没跑完时继续往下走，
                # 一路走到 FINISH 把父 Run 判成 COMPLETED（探针实测）。
                StepOutcome.WAITING_CHILD,
                StepOutcome.DENY_LOOP,          # L-7：再转下去也不会变
                # 空洞 222：被叫停也是"停"的一种，而且它发生在 `step()` 内部
                # （安全点读到意图 → 自己调 `cancel()`）。
                # 少了它，`run()` 会在一条已经 CANCELLED 的 Run 上继续转 ——
                # 终态之后再推进，B-10 的下半句就没了。
                StepOutcome.CANCELLED,
            ):
                return self.state
            # ② 这条 Run 已经结束了 —— 无论这一步的结果叫什么名字。
            # （`is_terminal` 是 property，不是方法。）
            if self.agent_run is not None and self.agent_run.is_terminal:
                return self.state

    # ------------------------------------------------------------ 一步
    def step(self) -> StepOutcome:
        """走一步，并把结果记在 `self.last_outcome` 上。

        **为什么 Runtime 要自己记一份结果**（F-1 / M28）：

        `run()` 一路走到停，返回的是 `State` 而不是"最后一步是什么"。
        于是调用方只知道"停了"，说不出"因为什么停的" ——
        挂起、预算耗尽、完成，三种"停"在页面上是同一副样子。

        记在 Runtime 侧而不是让契约层自己推：
        终态与停止理由由 Runtime 声明（B-7），契约层只是读出来。
        `run()` 里调的正是这个 `step()`，所以一路跑完之后
        `last_outcome` 自然停在最后一次的结果上。
        """
        self.last_outcome = self._step()
        return self.last_outcome

    # ---------------------------------------------------------- 叫停（B-8）
    def cancel(self, *, reason: str, by: str = "system") -> StepOutcome:
        """把一条 Run 叫停。

        ------------------------------------------------------------------
        为什么"没有这个入口"本身就是个洞（空洞 221）

        D-13 处理的是"父 Run 已终态、子 Run 还在跑"的孤儿。
        可那个状态此前在 AgentOS 里**只能靠直接改快照造出来** ——
        换句话说：一条 Run 根本不能被叫停。
        取消只能从外部发生（kill -9、手工改库、把策略改成 REQUIRE_APPROVAL 再驳回）。

        于是"把一条正在跑的 Run 停下来"这个动作的实现者是运维，不是系统：
        它能派生子 Run（M25）、能挂起等人审批（M18），
        唯独没有"算了，别跑了"。而 D-13 那种局面 ——
        父已终态、子在跑 —— 恰恰是取消缺位时**唯一**会发生的局面。

        ------------------------------------------------------------------
        `reason` 与 `by` 为什么都必填（A-8 同款）

        审计要回答的是"谁叫停的、为什么"。
        一条查不到是谁的 `cancelled`，等于取消这件事没有发生过 ——
        事后看到它的人无从判断是用户点的、策略拦的，还是系统崩了。

        ------------------------------------------------------------------
        它**不是** `step()` 的一个分支

        取消不是"再走一步"，是"不走了"。走 `step()` 意味着要经过
        Decision → Policy → Action 那条链，而取消恰恰要绕开它 ——
        否则一条被 Policy 拦住的 Run 永远取消不掉（它每次都卡在同一个闸门上）。
        """
        if not reason:
            raise InvariantViolation(
                "B-8: cancel() requires a non-empty reason; "
                "a run nobody can explain the stopping of is unauditable"
            )
        if not by:
            raise InvariantViolation(
                "B-8: cancel() requires a non-empty 'by'; "
                "an anonymous cancellation cannot be attributed (A-8)"
            )

        state = self.state
        if state is None:
            raise RuntimeError("AgentLoop.cancel() before start()")

        # B-10 / B-3 的另一半：终态不能再被取消。
        #
        # 静默返回"取消成功"会骗人：调用方以为它按停了，
        # 而那条 Run 其实早就停了 —— 而且可能是 COMPLETED。
        # "取消一条已经完成的 Run"若成立，"完成"就没有意义了。
        assert self.agent_run is not None
        if self.agent_run.is_terminal:
            raise InvariantViolation(
                f"B-10: run {self.agent_run.run_id!r} is already "
                f"{self.agent_run.status.value!r}; a terminal run cannot be cancelled"
            )

        # R-7：取消**意图**必须先于取消**宣告**落库。
        #
        # 顺序反了会留下一个窗口：Run 已经对外是 CANCELLED 了，
        # 而"有人要求取消它"这件事还没持久化。窗口里崩一次，
        # 重启后没有任何人知道这条 Run 该停 —— 它就是一条普通的 RUNNING。
        # 与 X-3 同款形状（状态与事件同事务），只是这里的两件事实是
        # "我要停"与"我停了"。
        self._request_cancellation(reason=reason, by=by)

        # B-9：先子后父。两条的顺序为什么不能反，见 `_cancel_pending_child`。
        self._cancel_pending_child(reason=reason, by=by)
        self._cancel_gate(reason=reason, by=by)

        self._apply(
            Observation(
                run_id=state.run_id,
                kind=RUN_CANCELLED,
                summary=f"run cancelled by {by}: {reason}",
                content={"by": by, "reason": reason},
            )
        )
        self._trace(CANCELLED, payload={"by": by, "reason": reason})
        self._declare_terminal(
            AgentRunStatus.CANCELLED, reason=f"cancelled by {by}: {reason}"
        )

        # R-1：终态**也要**落快照。
        #
        # 少了这一步，取消只改了内存：重启后恢复出来的是一条 RUNNING 的 Run
        # （快照还停在挂起那一帧），它会被继续推进 —— 取消等于没发生，
        # 而且没有任何报错。这正是"父 Run 已终态"此前只能手工造出来的原因。
        self._capture_snapshot(reason=f"cancelled by {by}: {reason}")

        # R-8 / R-10：我自己停了，所以这条意图到此为止。
        #
        # 不结掉它，Sweeper 下一轮还会捞到它 —— 而那时 `cancel()` 会撞 B-10 抛。
        # 一个每轮都抛的后台进程，比一个不干活的后台进程更难发现：
        # 它把日志刷满，而真正的故障淹没在里面。
        self._settle_cancellation()
        return self._record(StepOutcome.CANCELLED)

    # ------------------------------------------------- 取消意图（空洞 222）
    def _request_cancellation(self, *, reason: str, by: str) -> None:
        """R-7：把"我要停"先写成一条持久事实。"""
        if self.cancellations is None or self.agent_run is None:
            return
        self.cancellations.request(
            self.agent_run.run_id, reason=reason, by=by
        )

    def _settle_cancellation(self) -> None:
        """R-8 / R-10：只有真的停了才结掉。"""
        if self.cancellations is None or self.agent_run is None:
            return
        self.cancellations.settle(self.agent_run.run_id)

    def _pending_cancellation(self) -> RunCancellation | None:
        """我有没有一条**还没被认领**的取消意图？

        这是跨进程取消的**安全点**。三条路只有它和 Sweeper 能从外部进来：

            · 本进程有人调 `cancel()`     → 对象引用，不需要这张表
            · 我在跑，下一步会调 `step()` → 这里
            · 我挂在 WAITING_CHILD 上     → Sweeper（`RunCancellationService`）
        """
        if self.cancellations is None or self.agent_run is None:
            return None
        if self.agent_run.is_terminal:
            # B-10：已经终态。再读一次意图只是为了让 Sweeper 有机会结掉它。
            return None
        request = self.cancellations.for_run(self.agent_run.run_id)
        if request is None or request.is_settled:
            return None
        return request

    def _cancel_pending_child(self, *, reason: str, by: str) -> None:
        """B-9：取消**级联**到我正在等的那条子 Run。

        ------------------------------------------------------------------
        不级联会怎样

        父 Run 判了 CANCELLED，它派出去的那条子 Run 还在跑：
        继续花钱、继续产生外部副作用，而它的结果再也交不回来
        （父已终态 ⟹ 唤醒路径只能记一条 D-13 孤儿）。
        用户按的是"停止"，得到的是**停止了一半**。

        ------------------------------------------------------------------
        为什么必须先子后父

        反过来（先宣布自己终态，再回头叫停子 Run）中间崩一次，
        留下的残局是"父已终态、子还在跑" —— 而那正是 D-13 必须存在的理由，
        也就是**必须有人来看一眼**的那一类残局。

        先子后父崩在同一个位置，留下的是"父还活着、子已取消"：
        父 Run 会被正常唤醒，拿到 `cancelled`，走 D-12 记账。
        两种残局系统都收得住，但只有后者不需要人来判断。

        ------------------------------------------------------------------
        叫不停就必须喊出来（PR-26 / PR-19 同款）

        `spawner` 没有 `cancel_child` = 它起得出却停不了。
        静默跳过会返回一个"取消成功"，而那条子 Run 还在跑 ——
        说的和发生的不是同一件事。
        """
        pending = self.pending_child
        if pending is None:
            return

        # 空洞 222：不管那条子 Run 在不在本进程，**先把意图落库**。
        #
        # 顺序不能反（R-7 的级联版）：先判死登记处、再落意图，
        # 中间崩一次就是 M33 结束时那个形状 —— 记录说 cancelled，
        # 而跑在另一个进程里的那条 Run 从来没听说过这件事。
        #
        # 落点在这里而不是 spawner 里，是因为"我有没有一个持久通道"
        # 只有 Loop 自己知道（B-7：不把同一件事的定义分给两个对象）。
        if self.cancellations is not None:
            self.cancellations.request(
                pending.child_run_id,
                reason=f"parent run cancelled by {by}: {reason}",
                by=by,
            )

        canceller = getattr(self.spawner, "cancel_child", None)
        if canceller is None:
            raise InvariantViolation(
                f"B-9: run {self.state.run_id if self.state else ''!r} is waiting "
                f"for child run {pending.child_run_id!r} but its spawner cannot "
                f"cancel it; refusing to report a cancellation that only stopped "
                f"half the tree"
            )

        handle = canceller(
            pending.child_run_id,
            reason=f"parent run cancelled by {by}: {reason}",
            by=by,
        )

        # ------------------------------------------------------------------
        # D-16：赛跑有两种结局，负责人不同。**先看是哪种。**
        #
        # 旧实现不分结局，一律 `mark_delivered()` 结掉，于是两个方向的错
        # 都由这同一行代码产生（空洞 224）：
        #
        #   脸 A（取消赢）：父替它写了 `cancelled` → 它跑完时撞 B-3 抛异常，
        #        真实结果（含 S-1 的撤销参数）丢失。
        #   脸 B（完成赢）：结果已经产生却从未被看过，被"已交付"结掉
        #        → D-13 孤儿永不登记 → 副作用**静默消失**。
        #
        # 按 A-12，脸 B 更坏：脸 A 至少会喊，脸 B 什么都不喊。

        if not handle.is_finished:
            # 它还在跑（跨进程）。意图已经落进两处：
            #   · `run_cancellations`（R-7 的级联版）—— 让它**知道**
            #   · `child_runs.cancel_requested_at`（D-14）—— 让这件事可查
            # 剩下的事是它自己在下一个安全点停下来（空洞 222 那三段式）。
            #
            # 这里**不** mark_delivered：D-7 说结果不可能在产生之前被交付，
            # 而"它还没终态"正是结果还不存在。结掉由唤醒路径做 ——
            # 到那时父 Run 已终态，D-13 孤儿会接住它的副作用。
            return

        if handle.status != "cancelled":
            # 脸 B：它先跑完了。这是**既成事实**，取消改不动它（D-15）——
            # 取消只能拦住还没产生的结果。
            #
            # 关键改动：**不** mark_delivered。旧实现照样结掉，于是那条
            # "已经产生、却从未被任何人看过"的结果被记为已交付，
            # 唤醒路径再见它只会说 ALREADY_DELIVERED，D-13 孤儿永不登记。
            # 交给唤醒路径（事件丢失时由兜底扫接住）—— A-12：变慢，不变错。
            #
            # 同理这里**不**记 D-12：它没被叫停，它是做完了（PR-19）。
            return

        # B-9 的第三半（重述）：叫停之后必须**有一条路径**把它结掉，
        # 否则它永远留在 `undelivered()` 里，兜底扫每轮记一条 D-13 孤儿。
        #
        # 判据从"我取消过它"改成"**它**说自己停了"（D-14）——
        # 只有这一支拿得到子 Run 自己写下的 `cancelled`，也只有这一支能结。
        #
        # 不结的后果不是立刻出错，是一条慢慢长大的尾巴：
        # 取消的次数越多，sweep 越慢，而账本上多出一堆其实早已处理过的"孤儿"。
        registry = getattr(self.spawner, "registry", None)
        if registry is None:
            raise InvariantViolation(
                "B-9: the spawner that spawned child run "
                f"{pending.child_run_id!r} exposes no registry; cannot close out "
                f"its delivery, so the sweep would pick it up forever"
            )
        registry.mark_delivered(pending.child_run_id)

        # D-12：级联取消之后，这次委派**仍然**要记账。
        #
        # 子 Run 被叫停之前可能已经在外部世界留下了东西（建了工单、发了邮件），
        # 而这条账本以后再也没有机会补 —— 父 Run 马上就要终态，
        # 不会再有任何人走 `_finish_child`。
        step = self.current_step
        action = (
            self.pending_action if self.pending_action is not None else pending.action
        )
        task_id = self.pending_child_task_id or pending.parent_task_id
        if action is not None and step is not None and task_id:
            self._record_delegation_unresolved(
                action,
                step,
                task_id=task_id,
                execution_id=pending.parent_execution_id,
                child_run_id=pending.child_run_id,
                outcome="cancelled",
            )

    def _cancel_gate(self, *, reason: str, by: str) -> None:
        """把 Kernel 里那条因为闸门而 SUSPENDED 的 Execution 真的判死。

        不判死的话它会**永远 SUSPENDED**：等的那个人（审批人 / 子 Run）
        再也不会来了，而 Recovery 不会救它 —— 它没有超时，它只是在等人。

        用 `kernel.cancel()` 而不是 `kernel.fail()`：
        这一步没有失败，它只是**不必再发生**了。
        判成 FAILED 会让排障的人去找"它为什么失败"（PR-19 同款）。
        """
        execution_id = ""

        if self.pending_approval is not None:
            approval = self.pending_approval
            if approval.status is ApprovalStatus.PENDING:
                # A-9：撤销 ≠ 驳回。驳回说"不准做"，撤销说"不用做了"。
                # 并成一个状态之后，审计回答不了"有没有人真的审过它"。
                self.harness.approvals.cancel(      # type: ignore[union-attr]
                    approval.approval_id, by=by
                )
            execution_id = approval.execution_id or ""
            self._clear_pending()
        elif self.pending_child is not None:
            execution_id = self.pending_child.parent_execution_id

        self._clear_pending_child()

        if not execution_id:
            return
        if self.kernel.status_of(execution_id) in TERMINAL_EXECUTION_STATUSES:
            return
        # M48 / 空洞 228：先**请求**，再判死（X-11 / R-7 同款）。
        #
        # 在此之前这里直接 `kernel.cancel()`：意图那个布尔位
        # （`executions.cancellation_requested`）从头到尾没被写过 ——
        # 它是一列死列，`EXECUTION_CANCEL_REQUESTED` 事件永远发不出来，
        # 而"谁叫停、为什么"在这条链路上无处可写。
        #
        # 顺序不能反：反了就是"先宣告它死了，再补一句有人要求它死"，
        # 中间崩一次就只剩一个说不出缘由的 CANCELLED。
        self.kernel.request_cancel(execution_id, reason=reason, by=by)
        self.kernel.cancel(execution_id)

    def _step(self) -> StepOutcome:
        state = self.state
        if state is None:
            raise RuntimeError("AgentLoop.step() before start()")

        # 空洞 222：**安全点**。读到一条未认领的取消意图 → 我自己停。
        #
        # 位置必须在最前面，早于 `pending_approval` / `pending_child` 那两个
        # 提前返回：一条被叫停的 Run 不该先去报告"我在等 X" ——
        # 它没有在等，它要结束了。
        request = self._pending_cancellation()
        if request is not None:
            return self.cancel(reason=request.reason, by=request.by)

        if self.pending_approval is not None:
            return StepOutcome.WAITING_APPROVAL          # 等 Harness 放行

        # D-5（M26）：派生过子 Run 之后，**这一步就到此为止**。
        #
        # M25 漏了这一句。后果不是"多走一步"那么轻：探针实测，下一次 `step()`
        # 会完全无视 `pending_child` 继续往下推进，一路走到 FINISH，
        # 于是 **子 Run 还在跑，父 Run 已经宣布 COMPLETED** ——
        # 委派的结果永远不会回到 State，而且没有任何报错（B-7 意义上的"说谎"）。
        if self.pending_child is not None:
            return StepOutcome.WAITING_CHILD

        budget = state.goal.budget.max_steps or self.config.max_steps
        if self.steps >= budget:
            # 预算耗尽是 Run 的终态，不只是 Loop 停下来
            self._declare_terminal(
                AgentRunStatus.FAILED,
                reason=f"step budget exhausted ({self.steps}/{budget})",
            )
            return self._record(StepOutcome.BUDGET_EXHAUSTED)

        if state.current_plan is None:
            if not self._plan():
                # I-12：换不出一份**不一样**的计划，就不是重规划。
                # 与其拿着同一份计划再撞一次墙，不如诚实地说"没办法了"。
                self._declare_terminal(
                    AgentRunStatus.FAILED,
                    reason="replan produced a plan with the same shape as the "
                           "invalidated one; no alternative path available",
                )
                return self._record(StepOutcome.FAILED)

        # ── I-18：计划声明了运行时做不到的事，就不许开始执行它 ──
        #
        # 位置很讲究，三件事同时成立才放这儿：
        #
        #   ① 在 `_plan()` **之后** —— 刚落的计划立刻被审，第一步都不会执行；
        #   ② 在 `_ensure_step()` / `decision_engine.decide()` **之前** ——
        #      拒绝发生在**任何副作用之前**（"宁可拒绝"的意思就是这一步）；
        #   ③ 在**每一次 step 都过**的位置，而不是只挂在 `_plan()` 后面 ——
        #      计划还有第二条来路：**快照恢复**（`state_from_dict` 直接
        #      `current_plan=plan`，不走 reducer）。只挂在 `_plan()` 后面的话，
        #      一条从快照恢复回来的、带 `kind='human'` 的 Run 会**绕过**这道判据，
        #      然后照旧把它当 task 跑掉。
        #      判据要住在**所有路径的汇合处**，不是某一条路径上（M85）。
        #
        # 这与 I-12 的处置同一精神：计划层面的缺陷 → 诚实判死 + 点名理由。
        # 不 REPLAN 是刻意的：计划本身没错，错的是**这个运行时做不到** ——
        # 让 Planner "再换一份"等于告诉它"你的计划有问题"，那是一句假话，
        # 而且换回来十有八九还是同一个 kind（它凭什么知道运行时支持什么）。
        # 说清楚"我做不到什么"，比反复要一份做不到的计划要诚实。
        if state.current_plan is not None:
            offenders = _unsupported_plan_nodes(state.current_plan)
            if offenders:
                self._declare_terminal(
                    AgentRunStatus.FAILED,
                    reason=_unsupported_kinds_reason(offenders),
                )
                return self._record(StepOutcome.FAILED)

        # ── I-16：计划卡住了就不许往下编造 ──
        #
        # "计划用完了"（节点都做过）是常态 —— 动态决策图允许 ad-hoc 步。
        # "计划卡住了"（还有节点没做，但一个就绪的都没有）不是常态：
        # 它意味着这条路走不通了，多半是某个依赖那一步失败了。
        #
        # 两条路必须分开处置。混起来的话，一个卡住的计划会**静默退化成
        # ad-hoc 步**：Runtime 自己编一个计划没批准过的步，照常往下走，
        # 而账本上读不出任何区别（M82 的"把存在当成被处理"同族）。
        #
        # 处置与 I-11 同一精神：**带着走不通的路不许往下走**。
        # 换计划而不是判死 —— 失败说明"这条计划行不通"，不等于"目标无解"。
        # REPLAN 吃预算（I-10），所以一条真的走不通的路会在预算耗尽时
        # 走到 FAILED：既不谎报成功，也不空转。
        #
        # 只在"这一步做完了、该开新步了"的时候判 —— 否则会把一个
        # 正走到一半的 Step 从脚下抽掉。
        if (
            state.current_plan is not None
            and (self.current_step is None or self._step_is_done())
            and self._plan_is_stuck(state.current_plan)
        ):
            self.steps += 1
            self._apply_plan_invalidated()
            return self._record(StepOutcome.REPLANNED)

        decision = self.decision_engine.decide(state)
        # 注意：Decision **必须**带 Action（构造时校验），所以"没有下一步"不是
        # 用空 Decision 表达，而是用 `ActionType.FINISH` 表达 —— 否则 I-4 会
        # 开一个"Decision 可以什么都不选"的口子，副作用来源就模糊了。
        action = ActionResolver().resolve(decision)      # I-4：唯一通道

        if action.action_type is ActionType.FINISH:
            # I-11：带着**没被处理过的失败**不许宣布完成。
            #
            # 这是 FINISH 分支上此前缺的一句。后果（探针实测）：
            #
            #     步骤 1：工具不存在 → StepOutcome.FAILED，Run 仍 running
            #     步骤 2：脚本用完 → FINISH → agent_run = **completed**
            #
            # 也就是说，**一个关键步骤失败了的 Run 会被宣布完成**，
            # 而失败的证据就在 `state.variables["result:…"]` 里 —— 只是没人看。
            #
            # 按"宁可拒绝，不许编造"判：这不是"少了个功能"，是系统主动说了假话。
            # 而终态由 Runtime 声明（B-7），所以这条判据必须钉在**这里** ——
            # 换一个笨 DecisionEngine 也不会让它重新说谎。
            #
            # 正确应对不是判死，是**换计划**（REPLAN）：失败意味着当前这条
            # 计划行不通，而不是这个目标无解。REPLAN 吃预算（I-10），
            # 所以一个真的救不回来的 Run 会在预算耗尽时走到 FAILED ——
            # 既不会谎报成功，也不会空转。
            if self._failures_since_last_plan():
                self.steps += 1
                self._apply_plan_invalidated()
                return self._record(StepOutcome.REPLANNED)
            return self._finish()

        if action.action_type is ActionType.REPLAN:
            # I-10：重规划**必须吃预算**。
            #
            # 这一句此前没有。后果是一个一直返回 REPLAN 的 DecisionEngine
            # 会让 Run **永远跑下去**：`self.steps` 只在执行完成那条路径上
            # 自增（I-10 之前），于是第 748 行那句 `steps >= budget`
            # 永远不成立 —— 预算耗尽这个终态根本到不了。
            #
            # 它与 L-7 是同一族病（"一个永远被拒的 Run 会永远空转，
            # 且没有任何报错"）：不消耗预算的分支，就是一条可以无限走的分支。
            # 而 REPLAN 恰恰是唯一一个"不产生 Task、也不产生终态"的出口 ——
            # 它是循环里最容易变成空转的那一条。
            self.steps += 1
            self._apply_plan_invalidated()
            return self._record(StepOutcome.REPLANNED)

        # ── 唯一拦截点（基线 §2 的 P0-3）：Runtime 主动调用 Harness ──
        # Harness 是**被调用方**，它不认识 Kernel；要挂起的话由 Loop 去办。
        assert self.harness is not None
        verdict = self.harness.before_action(
            action, context=self._policy_context()
        )

        if verdict.verdict is Verdict.DENY:
            return self._denied(action, verdict)

        if verdict.verdict is Verdict.REQUIRE_APPROVAL:
            return self._suspend_for_approval(action, verdict)

        # M25：派生子 Run 的 Action **在派发之前**就被接走。
        #
        # 位置很讲究：放在 Harness 之后 —— 委派和调技能同样是会改变外部世界的动作，
        # 策略必须先审；放在 `_execute` 之前 —— 一旦交给 Worker 就晚了，
        # 一条 Execution 装不下一整条子 Run（E-19）。
        if self.spawner is not None and child_run_kind_of(action.action_type) is not None:
            return self._suspend_for_child(action)

        return self._execute(action)

    # ------------------------------------------------------------ 审批
    def approve(self, by: str = "human", comment: str = "") -> StepOutcome:
        """人放行了：先关掉 Kernel 里那条挂起，再执行被挂起的 Action（X-11）。

        顺序不能反 —— 先执行再关挂起的话，进程在中途崩溃会留下一条
        永远 SUSPENDED 的 Execution，Recovery 也救不回来（它等人，人已经批过了）。
        """
        approval = self._must_pending()
        action = approval.action
        assert action is not None

        self.harness.approvals.approve(                  # type: ignore[union-attr]
            approval.approval_id, by=by, comment=comment
        )
        self._close_gate(approval, approved=True)
        self._apply(
            Observation(
                run_id=approval.run_id,
                kind=APPROVAL_GRANTED,
                summary=f"approval {approval.approval_id} granted by {by}",
                content={"approval_id": approval.approval_id, "decided_by": by},
            )
        )
        self._trace(
            APPROVED,
            execution_id=approval.execution_id or "",
            payload={"approval_id": approval.approval_id, "decided_by": by, "approved": True},
        )
        self._clear_pending()
        return self._execute(action)

    def reject(self, by: str = "human", comment: str = "") -> StepOutcome:
        """人驳回了：不产生 Task，但**同样要留下 Observation**。

        驳回是事实。不留的话 Agent 会以为"这一步没发生过"，下一次决策
        很可能又提出同一个动作，形成驳回—重试的死循环。
        """
        approval = self._must_pending()
        self.harness.approvals.reject(                   # type: ignore[union-attr]
            approval.approval_id, by=by, comment=comment
        )
        self._close_gate(approval, approved=False)
        self._apply(
            Observation(
                run_id=approval.run_id,
                kind=APPROVAL_REJECTED,
                summary=f"approval {approval.approval_id} rejected by {by}",
                content={
                    "approval_id": approval.approval_id,
                    "decided_by": by,
                    "comment": comment,
                },
            )
        )
        self._trace(
            APPROVED,
            execution_id=approval.execution_id or "",
            payload={"approval_id": approval.approval_id, "decided_by": by, "approved": False},
        )
        self._clear_pending()
        # B-11：关掉闸门后必须立刻重新派生 Step/Run 状态。
        # _close_gate 已经把 Execution 从 SUSPENDED 走完到 COMPLETED，
        # 但如果不重新派生，Step/Run 会停在 SUSPENDED，而 pending_approval
        # 已经是 None 了 —— Run 说"在等"但说不出在等谁（B-4 违反），
        # 而且快照会撞上 R-1/R-6 的断言（SUSPENDED 但没有 pending_*_id）。
        self._sync_after_execution()
        return self._record(StepOutcome.DENIED)

    def expire_approvals(self) -> list[str]:
        """把超时的审批推进到 EXPIRED（与 Cancellation 的 sweep 同理）。

        **意图不会自己变成终态** —— 没人来扫，一条过期审批会永远停在
        PENDING，Run 也就永远挂着，而且没有任何报错。
        """
        assert self.harness is not None
        expired = self.harness.approvals.expire_due()
        if self.pending_approval is not None:
            approval = self.pending_approval
            if approval.status is ApprovalStatus.EXPIRED:
                self._close_gate(approval, approved=False)
                self._apply(
                    Observation(
                        run_id=approval.run_id,
                        kind=APPROVAL_REJECTED,
                        summary=f"approval {approval.approval_id} expired",
                        content={"approval_id": approval.approval_id, "decided_by": "timeout"},
                    )
                )
                self._trace(
                    APPROVED,
                    execution_id=approval.execution_id or "",
                    payload={
                        "approval_id": approval.approval_id,
                        "decided_by": "timeout",
                        "approved": False,
                    },
                )
                self._clear_pending()
                # B-11：与 reject() 同款 —— _close_gate 走完了 Execution，
                # 不重新派生的话 Step/Run 停在 SUSPENDED 而 pending_approval 是 None。
                self._sync_after_execution()
                self._record(StepOutcome.APPROVAL_EXPIRED)
        return expired

    # ------------------------------------------------------------ Business Domain
    def _consumed_plan_nodes(self) -> set[str]:
        """这份 Run 已经实例化成 Step 过的 plan node（按 node_id）。

        **从 `steps_of_run` 现算，不另存一份。** 理由有两条：
        ① 它是派生值（B-2 的同一精神），另存一份就有两个地方会不同步；
        ② `steps_of_run` 本来就进快照（`snapshot.steps`），
           现算等于**恢复之后它自动是对的** —— 不需要再补一条 R-7 式的进度。
        """
        return {s.plan_node_id for s in self.steps_of_run}

    def _satisfied_plan_nodes(self) -> set[str]:
        """依赖已经**被满足**的 plan node。

        判据是"那个节点对应的 Step **完成了**"，不是"它开始过"。

        刻意**只认 COMPLETED**：`FAILED` / `CANCELLED` 都不算满足 ——
        "B 依赖 A" 的意思是 B 要用 A 的**产物**，而一个失败掉的 A
        没有产物。把失败也算成"满足"，依赖图就又变回注释了。
        """
        return {
            s.plan_node_id
            for s in self.steps_of_run
            if s.status is StepStatus.COMPLETED
        }

    def _next_plan_node(self, plan: Plan) -> PlanNode | None:
        """I-16：挑出**下一个该做的**节点 —— 按**意义**挑，不按**位置**挑。

        在此之前这里是 `plan.nodes[len(self.steps_of_run)]`，也就是"下标轮到谁
        就是谁"。两个后果（probe87.py 实测，不是推演）：

            ① 计划说 `n2 depends_on n1`，而 n2 排在 n1 前面
               → 运行时**先跑了 n2**。`depends_on` 只被校验过，从没被执行过。
            ② `len(self.steps_of_run)` 是**这条 Run 已经跑了多少步**，
               不是**这份计划消费到第几个节点**。重规划换出一份新计划之后，
               它从 `plan.nodes[已跑步数]` 开始取 —— 新计划的前 N 个节点
               **被静默跳过**，然后计划就用完了。

        ⭐ 两张脸是同一个根因：**计划是按位置消费的，不是按意义消费的。**

        现在改成：按计划自己的顺序，取第一个
        「**没被实例化过** 且 **依赖都已完成**」的节点。

        计划自己的顺序仍然决定"同时就绪时先做哪个" —— 那是有意义的
        （它是 Planner 表达偏好的唯一方式），只是不再是**唯一**依据。
        """
        consumed = self._consumed_plan_nodes()
        satisfied = self._satisfied_plan_nodes()
        for node in plan.nodes:
            if node.node_id in consumed:
                continue
            if all(dep in satisfied for dep in node.depends_on):
                return node
        return None

    def _plan_has_unconsumed_nodes(self, plan: Plan) -> bool:
        consumed = self._consumed_plan_nodes()
        return any(n.node_id not in consumed for n in plan.nodes)

    def _plan_is_stuck(self, plan: Plan) -> bool:
        """计划里还有节点没做，但**一个就绪的都没有**。

        这不是"计划用完了"（那是常态，动态决策图允许 ad-hoc 步），
        而是"这条路走不通了" —— 多半是某个依赖那一步失败了。

        ⚠️ 两者必须分得开：混起来的话，一个卡住的计划会**静默退化成
        ad-hoc 步**，账本上读不出任何区别（M82 的"把存在当成被处理"同族）。
        """
        return self._plan_has_unconsumed_nodes(plan) and self._next_plan_node(plan) is None

    def _ensure_step(self) -> Step:
        """取出当前 Step，没有就开一个（基线 §3.1：Step 由 Runtime 动态产生）。

        Step 是 **Plan Node 的运行时实例**，不是静态图节点 ——
        所以它在这里被"造出来"，而不是从某张预定义的表里读出来。

        I-16：取哪个节点由 `_next_plan_node()` 决定（依赖已满足 + 没做过），
        **不是**由下标决定。
        """
        if self.current_step is not None and not self._step_is_done():
            return self.current_step

        state = self.state
        assert state is not None
        assert self.agent_run is not None
        plan = state.current_plan
        node = self._next_plan_node(plan) if plan is not None else None
        if node is not None:
            node_id, name = node.node_id, node.name
        else:
            # 没有 Plan（或节点已用完）也要能开 Step —— 动态决策图里这是常态。
            #
            # ⚠️ "节点已用完"与"计划卡住"在这里长得一样，但**不**一样：
            # 卡住那条路在 `step()` 里就被 REPLAN 接走了，到不了这儿。
            # 让它到这儿，就等于用一个 ad-hoc 步把"计划卡住了"盖掉。
            index = len(self.steps_of_run)
            node_id, name = f"ad-hoc-{index}", f"step-{index}"

        step = Step(run_id=self.agent_run.run_id, plan_node_id=node_id, name=name)
        self.steps_of_run.append(step)
        self.current_step = step
        return step

    def _step_is_done(self) -> bool:
        from packages.agent_domain.business import TERMINAL_STEP_STATUSES

        return (
            self.current_step is not None
            and self.current_step.status in TERMINAL_STEP_STATUSES
        )

    def _statuses_for(self, step: Step) -> list[ExecutionStatus]:
        """把一个 Step 下所有 Task 的 Execution 状态捞出来（Step.status 的输入）。"""
        repository = self.kernel.repository
        statuses: list[ExecutionStatus] = []
        for task_id in step.task_ids:
            execution = repository.get_by_task(task_id)
            if execution is not None:
                statuses.append(execution.status)
        return statuses

    def _sync_after_execution(self) -> None:
        """每次执行后重投影一次 Step / Run 的状态。

        注意顺序：先 Step 后 Run —— Run 的输入就是 Step 的状态。
        """
        assert self.agent_run is not None
        for step in self.steps_of_run:
            step.sync(self._statuses_for(step))
        self._sync_run()

    def _sync_run(self, *, terminal: AgentRunStatus | None = None) -> None:
        """B-2：Run 的状态只能被投影，不能被设置。"""
        assert self.agent_run is not None
        self.agent_run.sync(self.steps_of_run, runtime_terminal=terminal)

    def _write_run_checkpoint(self, *, reason: str) -> RunCheckpoint:
        """基线 §14：Step 完成 / 进入 SUSPENDED 前写 Run Checkpoint。

        "挂起前强制写"最容易漏，漏了后果最严重：
        唤醒后要从 Checkpoint 继续，**不重新执行整个 Run**（§10）。
        """
        assert self.agent_run is not None
        state = self.state
        assert state is not None
        step = self.current_step
        cp = build_run_checkpoint(
            run_id=self.agent_run.run_id,
            current_step=step.step_id if step else "",
            completed_tasks=tuple(state.completed_tasks),
            variables={
                "reason": reason,
                "steps": self.steps,
                "step_count": len(self.steps_of_run),
            },
        )
        self.checkpoints.save(cp)
        self._trace(
            CHECKPOINT,
            step_id=step.step_id if step else "",
            payload={"reason": reason, "checkpoint_id": cp.checkpoint_id},
        )
        return cp

    # ------------------------------------------------------------ 快照 / 恢复
    def _port_progress(self) -> dict[str, Any]:
        """R-7（M86）：问一遍注入的两个 Intelligence 实现"你走到哪了"。

        无状态 / 未实现 `ProgressBearing` 的 Port 返回 None ——
        那表示"我随时可以从 state 重算"，于是恢复时不需要核对。

        ⚠️ 这里刻意**不**试图替 Port 保存进度：Runtime 不知道每种实现的
        进度长什么样，硬做就是把 Intelligence 的内部状态搬进 Runtime（B-7）。
        Runtime 只负责**把它带走**、**把它比一次**。
        """
        return {
            "planner": self._progress_of(self.planner),
            "decision_engine": self._progress_of(self.decision_engine),
        }

    @staticmethod
    def _progress_of(port: Any) -> Any:
        """可选能力探测（与 `cancel_child` / `attempts` 同一套风格）。

        ⚠️ 两个"说不清"要分得开（这一条是 M5 变异逼出来的）：

            **没有** `progress` 属性   → 无状态。合法，返回 None。
            **有** `progress()` 但返回 None → 它自述了、却说不出自己在哪。
                                            这是实现的问题，当场拒绝。

        混为一谈的后果：一个有状态的引擎只要 `progress()` 返回 None，
        就把自己伪装成无状态的 → 快照里存 None → 恢复时"两边都是 None"→
        **静默放行**，而它实际上会从第 1 步重来。R-7 就被这一个返回值绕过了。

        一个**故意撒谎**的 Port 永远防不住（`progress()` 是它唯一的自述渠道），
        但"我实现了这个方法却说不出值"是可测的 —— 而它不是撒谎，是疏漏。
        """
        if not hasattr(port, "progress"):
            return None                      # 无状态：什么都不用做
        value = port.progress()
        if value is None:
            raise InvariantViolation(
                f"R-7: {type(port).__name__} implements progress() but returned None. "
                "That is ambiguous: either it is stateless (then it should not "
                "implement progress() at all), or it is stateful and cannot say "
                "where it is (then a snapshot cannot be trusted to resume it). "
                "Return a value, or remove the method."
            )
        try:
            json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 兜底
            raise InvariantViolation(
                f"R-7: {type(port).__name__}.progress() must return a JSON-able "
                f"value (it is stored in the snapshot); got {type(value).__name__}: {exc}"
            ) from exc
        return value

    def _assert_port_progress_matches(self, snapshot: RunSnapshot) -> None:
        """R-7（M86）：让当前引擎接上这条 Run 的进度 —— 接不上就点名拒绝。

        两段，顺序不能反：

            1. 先问引擎 `resume(progress)`：能接上就接上，恢复照常走。
            2. 接不上（或没实现 `resume`）才拒绝。

        ⚠️ 为什么不能直接拒绝

        "挂起 → 进程重启 → 恢复"是最常规的一条路径：一条 Run 挂起等人
        审批（可能几小时），期间 Pod 被调度、被重启、被滚动更新 ——
        它回来时必须能接上。直接拒绝会把这条常规路径变成不可用，
        那是**用一个正确的判据弄坏一个正常的系统**。

        这条判据要挡的是"**静默**重来"，不是"恢复"本身。
        """
        stored = dict(snapshot.progress or {})
        for name, port in (
            ("planner", self.planner),
            ("decision_engine", self.decision_engine),
        ):
            want = stored.get(name)
            if want is None:
                # 快照说"这个 Port 当时没带进度"。若当前实现也说不出进度
                # （`progress()` 返回 None / 没实现），两边一致 → 放行。
                # 若当前实现自述了值 → 那是"换了一个世界的引擎"，往下走拒绝。
                if self._progress_of(port) is None:
                    continue

            # 先试着接上 —— 这是常态路径。
            if self._try_resume(port, want):
                continue

            have = self._progress_of(port)
            if want == have:
                continue          # 本来就在同一个进度上，不需要接
            raise InvariantViolation(
                f"R-7: run {snapshot.run_id!r} cannot be restored — the injected "
                f"{name} is not the one this snapshot was taken with, and it cannot "
                f"be resumed onto that progress. snapshot says {want!r}, the current "
                f"implementation says {have!r}. A stateful Intelligence implementation "
                "must be resumed with the same progress, or it will silently start "
                "over from its first step."
            )

    def _try_resume(self, port: Any, progress: Any) -> bool:
        """问引擎"你能接到这个进度上吗"，并且**验它是不是真的接上了**。

        两段，第二段是必须的：

            1. 调 `resume(progress)`；没实现 ⟹ False；抛异常 ⟹ False。
            2. ★ **接完再自述一次，看对不对得上**。

        第 2 段不是保险，是这条判据的**唯一守点**。少了它，
        "实现一个什么都不做的 `resume()`"就是绕过 R-7 的后门 ——
        而那个后门比不实现 `resume` 更隐蔽：它看起来接上了，
        实际 `calls` 还是 0，于是恢复之后**照样重复调一次模型**。

        判据是"接上了没有"，不是"有没有调用过 resume" ——
        前者可测，后者只是调用记录。
        """
        resumer = getattr(port, "resume", None)
        if resumer is None:
            return False
        try:
            resumer(progress)
        except Exception:  # noqa: BLE001 - 见下面注释
            # 引擎内部的失败原因对"这条 Run 能不能恢复"这个问题没有帮助
            # （PR-19：报错要说中真发生了什么 —— 而这里真发生的是"恢复不了"）。
            return False
        # ★ 关键：**问它现在在哪**，而不是相信它说自己接上了。
        return self._progress_of(port) == progress

    def capture(self, *, reason: str = "") -> RunSnapshot:
        """R-1：造一个可恢复快照（**只造不存** —— 存由 `_capture_snapshot` 做）。

        为什么 RunCheckpoint 不够：它只记 `current_step` / `completed_tasks`，
        那是**指针**不是**数据**。恢复时仍然不知道 Agent 当时认为世界是什么样、
        走了几步、花了多少钱 —— 于是只能重跑，而重跑对已产生外部副作用的 Task 是灾难。
        """
        state = self.state
        assert state is not None, "capture() before start()"
        run = self.agent_run
        assert run is not None

        spent: dict[str, Any] = {}
        if self.harness is not None:
            cost = self.harness.cost
            spent = {
                "cost": cost.spent_cost,
                "tokens": cost.spent_tokens,
                "steps": cost.steps,
            }
        return RunSnapshot(
            run_id=run.run_id,
            agent_id=self.config.agent_id,
            status=run.status.value,
            state=state_to_dict(state),
            steps=tuple(step_to_dict(s) for s in self.steps_of_run),
            current_step_id=self.current_step.step_id if self.current_step else "",
            # 注意 `step_count` 是**预算计数**（已执行动作数），不是 Step 个数 ——
            # 混了之后恢复出来的 Run 会被误判成"才刚起步"
            step_count=self.steps,
            consecutive_denials=self.consecutive_denials,
            pending_approval_id=(
                self.pending_approval.approval_id if self.pending_approval else None
            ),
            # R-6：等子 Run 与等审批同等 —— 都得带走"在等谁"。
            # 少了它，恢复出来的父 Run 是一条"挂起但没人叫得醒"的 Run：
            # 子 Run 跑完了，事件来了，`_must_pending_child` 却说"你没有在等"。
            pending_child_id=(
                self.pending_child.child_run_id if self.pending_child else None
            ),
            spent=spent,
            trace=trace_entries_to_dicts(self.trace),
            # R-7（M86）：注入的 Intelligence 实现自述的进度。
            # Runtime 不解释它，只是在恢复时比一次 —— 对不上就拒绝恢复，
            # 而不是让一个"走到第 3 步"的 Run 被一个"从未走过"的引擎接管。
            progress=self._port_progress(),
            reason=reason,
        )

    def _capture_snapshot(self, *, reason: str) -> RunSnapshot:
        snapshot = self.capture(reason=reason)
        self.snapshots.save(snapshot)
        self._trace(
            SNAPSHOT,
            payload={"reason": reason, "snapshot_id": snapshot.snapshot_id},
        )
        return snapshot

    def restore(self, snapshot: RunSnapshot) -> None:
        """把一个 Run 从快照重新装载回来（R-2 / R-3 / R-4 / R-7）。

        **恢复不是重跑**：Kernel 里的 Execution / Attempt 本来就持久化着，
        这里只补回 Runtime 侧的内存状态。已经发出去的 Task 不会被重发
        （幂等键 = execution_id）。

        ⚠️ R-7（M86）：这里还多一道 —— 注入的 Intelligence 实现若**有状态**，
        它必须自述进度并对得上快照里那个值。对不上就拒绝恢复。
        """
        if snapshot.is_terminal:
            raise IllegalTransition(
                f"R-3: run {snapshot.run_id!r} is already {snapshot.status}; "
                "a terminal run cannot be restored"
            )

        # ── R-7（M86）：不认识的引擎不许接管这条 Run ──
        #
        # 放在这里（而不是 restore 末尾）是刻意的：这是在改任何内存状态**之前**
        # 就能判掉的事，而它判掉的是一个"静默重跑"的后果。
        #
        # 后果长什么样（probe86.py 实测）：
        #
        #     正常路径   第 1 次 decide → LLM_CALL   第 2 次 → FINISH
        #     恢复之后   新引擎第 1 次 → LLM_CALL     ← 又调了一次模型
        #
        # 调模型要花钱、要在外部世界留痕。**静默重来一次不等于没发生。**
        #
        # 三条设计决定：
        #
        # 1. 只比**自述了进度**的那些 Port。无状态实现（`progress()` 返回 None）
        #    什么都不用做 —— 它随时可以从 state 重算，恢复对它本来就成立。
        # 2. 快照里存的也是 None 而当前引擎自述了值 → 同样算对不上。
        #    那意味着"这条 Run 是在一个无状态引擎下跑起来的"，
        #    换成有状态的接管一样是换了一个世界。
        # 3. 拒绝而不是修：Runtime 不替引擎还原进度（B-7），
        #    也绝不假装自己还原了（判据：宁可拒绝，不许编造）。
        self._assert_port_progress_matches(snapshot)
        state = state_from_dict(snapshot.state)
        self.state = state

        # R-4：账本必须延续。run_id 先对齐，再把丢掉的条目读回来 ——
        # `RunTrace.restore()` 自带"只能装进空账本 + 序号必须连续"两道校验。
        self.trace.run_id = snapshot.run_id
        self.trace.restore([trace_entry_from_dict(e) for e in snapshot.trace])

        # R-2：预算计数与已花费必须回来。少任何一个，"挂起—恢复"就成了
        # 一条重置预算的后门：走到上限 → 被闸门挡住 → 恢复 → 又能走一轮。
        self.steps = snapshot.step_count
        self.consecutive_denials = snapshot.consecutive_denials

        self.steps_of_run = [step_from_dict(s) for s in snapshot.steps]
        self.current_step = next(
            (s for s in self.steps_of_run if s.step_id == snapshot.current_step_id),
            None,
        )
        self.agent_run = AgentRun(
            run_id=snapshot.run_id,
            agent_id=snapshot.agent_id,
            goal=state.goal,
            state=state,
        )
        # M74：status 是**派生值**（B-2），所以这里不能赋值，只能重新投影一次。
        #
        # 少了这一句，装载回来的 Run 一律是 `created` ——
        # 不管快照里存的是 suspended 还是 running。
        # 而在 Kubernetes 里 Pod 重启是常态，于是"查一条正在等审批的 Run"
        # 会得到一个"刚创建、还没开始"的答案：
        # 审批在库里等着，界面上说这条 Run 还没起步，
        # 于是人以为系统卡在创建环节，而去查一个根本没问题的地方。
        #
        # 用 `sync()` 而不是 `object.__setattr__` 是刻意的：
        # status 由下层 Step 投影得出，直接赋值等于承认"状态可以被设置"，
        # 那正是 B-2 要挡的那件事。
        self.agent_run.sync(self.steps_of_run)

        if self.harness is not None and snapshot.spent:
            cost = self.harness.cost
            cost.spent_cost = float(snapshot.spent.get("cost") or 0.0)
            cost.spent_tokens = int(snapshot.spent.get("tokens") or 0)
            cost.steps = int(snapshot.spent.get("steps") or 0)

        # 接上人：恢复出来的 Run 必须知道自己在等哪条审批，
        # 否则 `approve()` 找不到 `pending_approval`，人点了也没用。
        if snapshot.pending_approval_id:
            assert self.harness is not None
            approval = self.harness.approvals.get(snapshot.pending_approval_id)
            if approval is None:
                raise InvariantViolation(
                    f"R-1: snapshot for run {snapshot.run_id!r} points at approval "
                    f"{snapshot.pending_approval_id!r} which no longer exists"
                )
            self.pending_approval = approval
            self.pending_action = approval.action

        # 接上子 Run（R-6 / M26）：恢复出来的 Run 必须知道自己在等哪条子 Run。
        #
        # 少了这一段，恢复出来的父 Run 是一条"挂起但没人叫得醒"的 Run：
        # 子 Run 跑完了、事件来了，`_must_pending_child` 却说"你没有在等"，
        # 而 D-3 那句"拿错 id 要报错"也因此退化成"根本没有 id"。
        #
        # 更糟的是它的另一半：不知道自己在等的 Run 会**再派生一次** ——
        # 而重新 `submit` 会拿到一个新的 execution_id，派生键本身就换了，
        # 于是连 `UNIQUE(parent_execution_id)` 也拦不住第二条子 Run（D-1）。
        if snapshot.pending_child_id:
            registry = getattr(self.spawner, "registry", None)
            if registry is None:
                raise InvariantViolation(
                    f"R-6: snapshot for run {snapshot.run_id!r} is waiting for child "
                    f"run {snapshot.pending_child_id!r}, but this loop has no "
                    f"spawner/registry to look it up; refusing to restore a run that "
                    f"nobody could ever wake"
                )
            handle = registry.for_child(snapshot.pending_child_id)
            if handle is None:
                raise InvariantViolation(
                    f"R-6: snapshot for run {snapshot.run_id!r} points at child run "
                    f"{snapshot.pending_child_id!r} which is not registered; the "
                    f"derivation record is gone, so D-1 no longer holds for it"
                )
            self.pending_child = handle
            self.pending_action = handle.action
            self.pending_child_task_id = handle.parent_task_id

        self._trace(
            RECOVERED,
            payload={
                "snapshot_id": snapshot.snapshot_id,
                "reason": snapshot.reason,
                "resumed_at_step": snapshot.step_count,
            },
        )

    # ------------------------------------------------------------ 内部
    def _policy_context(self) -> PolicyContext:
        state = self.state
        assert state is not None
        return PolicyContext(run_id=state.run_id, step_no=self.steps)

    def _suspend_for_approval(self, action: Action, verdict) -> StepOutcome:
        """REQUIRE_APPROVAL → 造审批 Task → Kernel 写 SUSPENDED(HUMAN_APPROVAL)。

        基线 §23 的 HITL 链：

            Action → Policy → REQUIRE_APPROVAL → Checkpoint
                   → SUSPENDED(HUMAN_APPROVAL) → Human → Approved → Wake Up → Execution

        为什么要真的在 Kernel 里造一条 Execution，而不是只在内存里记一笔：
          · 审批要能**活过进程重启**（内存里的一笔会丢，Run 就永远挂着）
          · 审批需要**超时**与**可取消** —— 这两件事 Kernel 已经有了（sweep / lease）
          · 审批需要**审计**：它在 outbox 里留下的事件链和其他执行是同构的

        Harness 只交出 ApprovalRequest；**真正写 SUSPENDED 的是这里**（H-4）。
        """
        state = self.state
        assert state is not None
        assert self.harness is not None
        approval = verdict.approval
        assert approval is not None

        # I-8：HUMAN_APPROVAL 必须带 timeout，否则一个人能把 Run 永久挂住
        gate = Action(
            run_id=state.run_id,
            action_type=ActionType.HUMAN_APPROVAL,
            payload={
                "question": action.rationale or f"approve {action.action_type.value}",
                "approval_id": approval.approval_id,
                "blocked_action_id": action.action_id,
                "blocked_action_type": action.action_type.value,
            },
            # 截止时间的**唯一事实源是那条 ApprovalRequest**：
            # 闸门 Action 的 I-8 timeout 必须与审批的 expires_at 一致，
            # 否则会出现"审批还没过期，闸门 Execution 已经超时"这种自相矛盾。
            timeout=approval.expires_at - approval.requested_at,
            risk_level=action.risk_level,
            rationale="; ".join(verdict.reasons),
        )
        # 闸门挂在**当前 Step** 上：这一步确实是在等审批，
        # 于是 Step → SUSPENDED、Run → SUSPENDED，整条投影链自洽。
        step = self._ensure_step()
        task = self.task_factory.from_action(gate, step_id=step.step_id)
        execution = self.kernel.submit(task)
        step.add_task(task.task_id)

        # §14：进入 SUSPENDED 前**必须**先落 Checkpoint（强制），
        # 否则唤醒后只能重跑整个 Run —— 对已产生外部副作用的 Task 是灾难。
        self._write_run_checkpoint(reason="suspending for approval")

        # 关键：SUSPENDED 只能从 RUNNING 转（基线 §9.2），所以闸门必须先被 Claim。
        #
        # 这不是实现的别扭，而是 SUSPENDED 的**语义**：
        #   它表示"执行权已经授出去过，又被主动挂起了"，而不是"还没开始"。
        #   "还没开始"是 PENDING —— 而 PENDING 的合法去向只有 RUNNING / CANCELLED。
        #
        # 于是闸门 Execution 的 Attempt 序列天然是可解释的：
        #   Attempt #1 = 发出询问（suspend 时被 CANCELLED，E-9 顺带释放 Lease）
        #   Attempt #2 = 人的答复（resume 时新开，带新 fencing_token）
        self.kernel.claim(execution.execution_id, worker_id="harness-gate")
        self.kernel.suspend(
            execution.execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approval_id": approval.approval_id},
        )
        # 审批 ↔ 挂起 双向可追溯
        #
        # ⚠️ X-13：**必须用返回值**。跨 Port 拿到的是**副本**，不是同一个对象 ——
        # `bind()` 改的是存储里那条，手上这个 `approval` 不会自动跟着变。
        #
        # 内存版因为 `store.get()` 返回同一个对象而"碰巧正确"，
        # 换成 PG 版就静默失效：`execution_id` 一直是 None，
        # 于是 `_close_gate()` 提前返回，那条 SUSPENDED 的 Execution 永远醒不过来 ——
        # 而且**没有任何报错**（人明明批过了，Run 就是不动）。
        approval = self.harness.approvals.bind(
            approval.approval_id, execution.execution_id
        )

        self.pending_approval = approval
        self.pending_action = action
        self._trace(
            APPROVAL,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            payload={
                "approval_id": approval.approval_id,
                "blocked_action_type": action.action_type.value,
            },
        )
        self._apply(
            Observation(
                run_id=state.run_id,
                kind=APPROVAL_REQUESTED,
                summary=(
                    f"action {action.action_type.value} suspended for approval "
                    f"{approval.approval_id}"
                ),
                content={
                    "approval_id": approval.approval_id,
                    "execution_id": execution.execution_id,
                    "reasons": list(verdict.reasons),
                },
            )
        )
        # R-1：挂起的最后一步必须落快照。
        #
        # 时序上它**晚于** RunCheckpoint（§14 要求 Checkpoint 在 `kernel.suspend()` 之前写），
        # 这不是疏漏而是必然：快照必须带上 `pending_approval_id` 才接得上人，
        # 而审批与 Execution 的绑定（`approvals.bind`）发生在挂起之后。
        #
        # 于是两个落点各有分工：
        #   RunCheckpoint  挂起**前** —— "从这继续"（指针，不含审批）
        #   RunSnapshot    挂起**后** —— "继续需要的数据"（含审批 id）
        # R-6：快照改到 `_sync_after_execution()` **之后**拍。
        #
        # 之前在它前面拍，于是快照里 `status` 记的是挂起**之前**的那个值
        # （实测是 `created`），而 Run 实际上已经是 SUSPENDED。
        # 快照是给人看的（"这个 Run 现在怎么了"），一份 status 说谎的快照
        # 比没有快照更糟 —— 它是 PR-23 说的那种假证据。
        self._sync_after_execution()      # Step/RUN 投影到 SUSPENDED
        self._capture_snapshot(reason="suspending for approval")
        return self._record(StepOutcome.WAITING_APPROVAL)

    # ------------------------------------------------------------ 子 Run（M25）
    def _suspend_for_child(self, action: Action) -> StepOutcome:
        """`AGENT_DELEGATION` / `SKILL_CALL` → 派生子 Run → SUSPENDED(CHILD_*)。

        ------------------------------------------------------------------
        为什么和审批闸门走同一条路

        两者都是"这一步现在干不完，要等别处"。区别只是等谁：
            审批闸门 → 等**人**（SuspensionReason.HUMAN_APPROVAL）
            子 Run   → 等**另一条 Run**（CHILD_AGENT / CHILD_SKILL）

        同一个机制，不是巧合：Kernel 的 SUSPENDED 语义本来就是
        "执行权授出去过，又主动挂起了"。派生子 Run 恰恰就是这个意思 ——
        执行权已经交给了子 Run，父 Execution 只是还没拿到结果。

        ------------------------------------------------------------------
        顺序（照抄闸门，一个都不能换）

            submit → spawn → Checkpoint → claim → suspend → snapshot

        `submit` 必须在 `spawn` 之前：派生键是 `execution_id`（D-1 / E-21），
        没有 execution_id 就没有"这次派生"的身份，重试会开出第二条子 Run。

        `Checkpoint` 必须在 `suspend` 之前（§14）：
        挂起之后才写，进程在中间崩了就没有恢复点。

        ------------------------------------------------------------------
        spawn 失败时那条 Task 会留在 PENDING

        这是可接受的：它会被 Worker 拿到，而安全网执行器会**点名**说
        "拥有你的 Loop 不在了"。补上 spawner 之后再 resume 即可。
        刻意不在这里把它删掉 —— 删掉等于假装这一步没发生过。
        """
        state = self.state
        assert state is not None
        assert self.spawner is not None

        kind = child_run_kind_of(action.action_type)
        assert kind is not None

        step = self._ensure_step()
        task = self.task_factory.from_action(
            action, step_id=step.step_id, extra_payload=self._context_payload(action)
        )
        execution = self.kernel.submit(task)
        step.add_task(task.task_id)

        request = ChildRunRequest(
            parent_run_id=state.run_id,
            # D-1：派生键 = 父 Execution 的 id。跨 Attempt 稳定，重试拿回同一条。
            parent_execution_id=execution.execution_id,
            kind=kind,
            target=str(
                action.payload.get("agent_id")
                or action.payload.get("skill")
                or action.payload.get("name")
                or ""
            ),
            instruction=str(
                action.payload.get("instruction")
                or action.payload.get("task")
                or action.rationale
            ),
            # S-1：整条 Action 随派生一起落库（含 `compensation`）。
            # 父 Run 是在**恢复之后**才等到子 Run 结果的，那一刻要登记撤销，
            # 而 Action 只在这一刻拿得到。
            action=action,
            parent_task_id=task.task_id,
            payload=action.payload,
            # D-31：这次派生自己声明的等待上限。
            #
            # 值来自 Intelligence 的 payload —— 只有它知道这次派出的是个
            # 两小时的深度研究还是两秒钟的查表。但**批准权在 Harness**：
            # `ChildRunRequest.__post_init__` 与登记处的 `bind()` 各裁决一次，
            # 越界是点名拒绝，不是静默截断（D-32 / D-33）。
            #
            # 刻意只读**一个**键：多一个别名（分钟/秒）就等于"这次派生的上限"
            # 有两个写法，而两种写法撞在一起时没人知道该听谁的（B-7）。
            wait_timeout=action.payload.get("wait_timeout_seconds"),
        )
        handle = self.spawner.spawn(request)
        # D-11：不得挂起在一条**已经有结果**的子 Run 上。
        #
        # D-1 把"父 Execution 重试"和"同一条子 Run"绑死在了一起（派生键 =
        # execution_id，跨 Attempt 稳定）。于是只要委派这一次还允许重试，
        # 第二次 `spawn()` 拿回的就是那条**已经终态**的子 Run ——
        # 而它不会再变，父 Run 挂起等它等于等一个不会再来的答案，
        # 且没有任何报错（D-8 那个"静默挂住"形状的第三个副本）。
        #
        # D-9 已经关掉了委派的重试，所以这里**不可达**。
        # 正因如此它做成响亮的断言而不是恢复路径（PR-26：兜底就该喊出来）：
        # 哪天有人把 FailureClass 改回可重试，这里会立刻变红，
        # 而不是悄悄长出一个"父 Run 永远在等子 Agent"的故障。
        if handle.is_finished:
            raise InvariantViolation(
                f"D-11: spawn() returned child run {handle.child_run_id!r} which is "
                f"already {handle.status!r}; suspending to wait for it would wait for "
                f"an answer that will never change — a delegation execution must not "
                f"be retried (D-9), because D-1 binds the retry to this same child run"
            )

        self._write_run_checkpoint(reason=f"suspending for {kind.value} child run")

        # 与闸门同构：SUSPENDED 只能从 RUNNING 转，所以必须先 claim。
        # Attempt #1 = 发起派生（suspend 时被 CANCELLED，E-9 释放 Lease）
        # Attempt #2 = 子 Run 的结果（resume 时新开，带新 fencing_token）
        self.kernel.claim(execution.execution_id, worker_id=f"parent-{kind.value}")
        self.kernel.suspend(
            execution.execution_id,
            reason=(
                SuspensionReason.CHILD_AGENT
                if kind is ChildRunKind.AGENT
                else SuspensionReason.CHILD_SKILL
            ),
            # 等**谁**必须写在 wait_condition 里 —— 一句"在等子 Agent"没法排查。
            wait_condition={
                "child_run_id": handle.child_run_id,
                "child_kind": kind.value,
                "target": handle.target,
            },
        )

        self.pending_child = handle
        self.pending_child_task_id = task.task_id
        self.pending_action = action
        self._trace(
            SUBMITTED,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            attempt_no=execution.current_attempt_no,
            payload={
                "action_type": action.action_type.value,
                "child_run_id": handle.child_run_id,
                "child_kind": kind.value,
            },
        )
        self._apply(
            Observation(
                run_id=state.run_id,
                kind=CHILD_RUN_SPAWNED,
                summary=(
                    f"action {action.action_type.value} spawned child {kind.value} run "
                    f"{handle.child_run_id}"
                ),
                content={
                    "child_run_id": handle.child_run_id,
                    "child_kind": kind.value,
                    "target": handle.target,
                    "execution_id": execution.execution_id,
                },
            )
        )
        # 同 R-6：快照必须在投影之后拍，否则它记的是一个已经不成立的 status。
        self._sync_after_execution()
        self._capture_snapshot(reason=f"suspending for {kind.value} child run")
        return self._record(StepOutcome.WAITING_CHILD)

    def child_completed(
        self, child_run_id: str, result: Mapping[str, Any] | None = None
    ) -> StepOutcome:
        """子 Run 跑完了：关挂起 → 结果进 State → 记补偿。

        这是子 Run 完成事件唤醒父 Run 之后的**唯一入口** ——
        不允许外部直接 `kernel.complete()` 那条父 Execution，
        否则"结果进没进 State"就没有人保证了，
        父 Agent 会以为这一步没发生过。
        """
        handle = self._must_pending_child(child_run_id)
        # 恢复之后 `pending_action` 是从 `ChildRunHandle.action` 接回来的 ——
        # 它带着 `compensation`，于是这一刻照样登记得了撤销（S-1）。
        action = self.pending_action if self.pending_action is not None else handle.action
        step = self.current_step
        task_id = self.pending_child_task_id or handle.parent_task_id
        execution_id = handle.parent_execution_id

        payload = dict(result or {})
        self._close_child_gate(execution_id, outcome="completed", result=payload)

        assert self.state is not None
        self._apply(
            Observation(
                run_id=self.state.run_id,
                kind=CHILD_RUN_FINISHED,
                summary=f"child {handle.kind.value} run {child_run_id} completed",
                content={
                    "child_run_id": child_run_id,
                    "child_kind": handle.kind.value,
                    "target": handle.target,
                    "execution_id": execution_id,
                },
            )
        )
        # S-1：SKILL_CALL / AGENT_DELEGATION 都在 S-13 的可补偿清单里 ——
        # 子 Run 会在外部世界留下状态（工单、订单、它自己的子 Run），
        # 所以撤销参数必须在这里登记，只有这一刻同时握着 Action 与结果。
        if action is not None and step is not None and task_id:
            self._record_compensation(action, step, task_id, execution_id)
        self._clear_pending_child()
        self.steps += 1
        self._sync_after_execution()
        if self._step_is_done():
            self._write_run_checkpoint(reason="step completed")
        return self._record(StepOutcome.EXECUTED)

    def child_failed(self, child_run_id: str, *, reason: str = "") -> StepOutcome:
        """子 Run 失败了（D-9）：关挂起，且**不可重试**。

        ------------------------------------------------------------------
        为什么这里曾经写成 TRANSIENT，以及为什么那是错的

        旧注释写的是"子 Run 失败对父来说是一次可重试的失败（TRANSIENT），
        重试判据归 Kernel"。听起来很谦逊 —— 判据确实归 Kernel，
        但**这里递给 Kernel 的那个 FailureClass 本身就是判据的一部分**，
        把它一律写成 TRANSIENT 等于替 Kernel 判了"可重试"。

        而委派根本不可重试，理由是 D-1：

            子 Run 是它自己的 Run，有自己的 Kernel、Attempt 与 RetryPolicy。
            它进 FAILED 意味着**那套预算已经判过了**。
            父侧重试拿回的是同一条子 Run（D-1 保证派生键跨 Attempt 稳定），
            于是第二次不会得到不同的答案。

            更要命的是委派 Execution 在 Worker 侧只有一种归宿 ——
            `DeferringExecutor` 拒绝（`DELEGATION_NOT_WORKER_EXECUTABLE`）。
            所以那个 TRANSIENT 重试 100% 以 PERMANENT 收场。

        实测（M31 探针）的完整后果链：

            子 Run 终态 → 父 Execution PENDING(attempt=2) → Worker 领走
              → DeferringExecutor 拒绝 → FAILED
              → 父 Execution 的终态 error 是 DELEGATION_NOT_WORKER_EXECUTABLE

        即：**真因被抹掉**。排障的人读到的是"拥有你的 Loop 不在了"，
        而真实原因是"子 Run 没做成"。这与 PR-19 那次
        （`HUMAN_APPROVAL` 被 `ToolCallExecutor` 以 `BAD_PAYLOAD` 拒掉）
        是同一类错：一句既不对又误导的话。

        ------------------------------------------------------------------
        不可重试 ≠ 父 Agent 无路可走

        `retry.py` 开头就把三件事分开了：
        Retry（这次要不要再来一次）/ Recovery（故障后怎么救回来）/
        **Agent Replanning（策略失败后换条路）**。
        这里关掉的是第一件，第三件完全开放：
        父 Agent 看到"子 Agent 失败"这条 Observation 之后，可以决定
        换一个目标再派一次 —— 那是**新的 Decision → 新的 Task →
        新的 execution_id**，于是 D-1 派生的是一条**新的**子 Run。
        """
        return self._finish_child(child_run_id, outcome="failed", reason=reason)

    def child_cancelled(self, child_run_id: str, *, reason: str = "") -> StepOutcome:
        """子 Run 被取消了（D-10 / S-15）。

        刻意**不**并进 `child_failed`：取消说的是"到此为止"（S-15），
        失败说的是"没做成"。两者在父侧都意味着"没拿到结果"，
        但对**排障**来说是两个方向 —— 一个该去找"谁取消的"，
        一个该去看"子 Run 为什么没做成"。

        并成一个类型之后，这个区别就只能在 payload 里找，
        而没有人会去找（`CHILD_RUN_CANCELLED` 事件类型同理，见 events/event.py）。
        """
        return self._finish_child(child_run_id, outcome="cancelled", reason=reason)

    def child_wait_expired(self, child_run_id: str, *, reason: str = "") -> StepOutcome:
        """D-18/D-19/空洞 229：等到上限也没有结果 —— **不再等**，但不说它失败了。

        ------------------------------------------------------------------
        为什么它是第四个入口，而不是 `child_failed` 的一个分支

        `child_completed` / `child_failed` / `child_cancelled` 三个入口
        说的都是**那条子 Run 的结局**；这一个说的不是。

            它跑完了       → 结果可信，用它
            它失败了       → 它自己判过，不可重试（D-9）
            它被取消了     → 到此为止（S-15）
            等不到任何结局 → **我们不知道**（D-19）

        第四种塞进 `child_failed` 的后果：
        Execution 的终态 error 会写成 `CHILD_RUN_FAILED`，
        于是排障的人去查"子 Run 为什么失败" —— 而它可能压根没失败，
        它可能正在某个 worker 上跑得好好的（PR-19：报错说的 ≠ 真实发生的）。

        ------------------------------------------------------------------
        为什么"不再等"之后**不**替父 Run 写终态

        父 Run 接下来干什么，只有父 Agent 自己知道：换一个目标再派一次
        （新的 Decision → 新的 execution_id → D-1 派生一条**新的**子 Run）
        是正当的，直接放弃也是正当的。
        替它写终态就是把"策略失败之后怎么办"从 Intelligence 手里拿走（D-21）。

        所以这里只做三件事：关闸门（Kernel 那条 Execution 判死）、
        记 UNRESOLVED（D-12 那一档）、清掉挂起 —— 然后**返回**，把下一步
        留给 `step()`。父 Run 从此"可以被推一步"，而不是"永远挂在
        WAITING_CHILD 上谁也推不动"。

        ------------------------------------------------------------------
        谁会来调它

        `ChildRunWaitExpirer`（`child_wait.py`）在兜底扫里扫到超期派生时调。
        它不可能是父 Run 自己 —— 父 Run 正挂在 `WAITING_CHILD` 上，
        它连 `step()` 都不会走到能判时间的地方（D-5）。
        这正是这个洞的形状：**挂着的那一方没有能力自己超时**。
        """
        return self._finish_child(
            child_run_id,
            outcome="unknown",
            reason=reason
            or f"child run {child_run_id} produced no result before its wait deadline",
        )

    def _finish_child(
        self, child_run_id: str, *, outcome: str, reason: str
    ) -> StepOutcome:
        """`child_failed` / `child_cancelled` 的共同那一半（B-7）。

        两条路径唯一的区别是 `outcome` 这个字符串 —— 它决定了
        FailureClass、error code 与 Observation 的措辞。
        把其余部分抄两份，就会出现第二个"子 Run 没收尾时会发生什么"的定义。
        """
        handle = self._must_pending_child(child_run_id)
        # 与 `child_completed` 同款：闸门一关、`_clear_pending_child()` 一跑，
        # Action / Step / Task 这三样就都拿不到了，而补偿记录正要它们（S-1）。
        step = self.current_step
        action = (
            self.pending_action if self.pending_action is not None else handle.action
        )
        task_id = self.pending_child_task_id or handle.parent_task_id
        execution_id = handle.parent_execution_id

        self._close_child_gate(
            execution_id,
            outcome=outcome,
            result={"error": reason or f"child run {outcome}"},
        )
        assert self.state is not None
        # D-19：不是每个 outcome 都是"这条子 Run 完事了"。
        # `unknown` 说的是"我们不再等了"，它**不是**这条子 Run 的结局。
        # 用 `CHILD_RUN_FINISHED` 去写它，State 里就会留下
        # "子 Run 已完成"这么一条事实 —— 而那条事实并不成立（PR-19）。
        self._apply(
            Observation(
                run_id=self.state.run_id,
                kind=CHILD_RUN_UNKNOWN if outcome == "unknown" else CHILD_RUN_FINISHED,
                summary=f"child {handle.kind.value} run {child_run_id} {_child_verb(outcome)}",
                content={
                    "child_run_id": child_run_id,
                    "child_kind": handle.kind.value,
                    "execution_id": handle.parent_execution_id,
                    # 让模型/RCA 不必去解析 summary 文本：终态种类是结构化字段。
                    "outcome": outcome,
                    "error": reason,
                    "agreed_wait_seconds": (
                        (handle.wait_until - handle.spawned_at).total_seconds()
                        if handle.wait_until
                        else None
                    ),
                    # D-31：这次派生**约定**的等待上限。
                    #
                    # 上限可以按次声明之后（空洞 230），"为什么等了 6 小时才报
                    # 等不到"必须能从这笔里读出来 —— 否则排障的人只看到
                    # "等不到"三个字，分不清那是**约定如此**还是**配错了**。
                    "wait_until": (
                        handle.wait_until.isoformat() if handle.wait_until else None
                    ),
                },
            )
        )
        # I-13：委派失败就是**一次执行失败**，它必须进 State。
        #
        # 那条委派 Execution 刚被 `_close_child_gate` 真的判成了 FAILED，
        # 但这条路径不经 Worker，于是没有人给它写 EXECUTION_FAILED
        # observation —— State 上只有 `child_run.finished`，而那条说的是
        # "子 Run 完事了"，不是"这条 execution 失败了"，是两件事。
        #
        # I-11 从 State 读"自上次规划以来有没有失败"，读不到它 →
        # 一个委派失败了的父 Run 照常 FINISH，账本上写 `goal reached`
        # （M81 探针实测）。那是 M77 治掉的那句谎言，
        # 只是从委派这扇门又进来了。
        #
        # I-14：两种 outcome 都要进 State，但**用不同的 kind**。
        #
        #     failed   它真的失败了          → `execution_failed`
        #     unknown  我们不知道（D-19）     → `execution_unresolved`
        #
        # 后者必须有一个自己的 kind，不能借用 `failed`：
        # 那条子 Run 可能正在某个 worker 上跑得好好的，
        # 说它"失败"就是 PR-19 那种错（报错说的 ≠ 真实发生的）。
        # 但它也**必须进 State** —— 否则父 Run 可以带着"这一步什么都没拿到"
        # 宣布目标达成（M82 探针实测，与 M81 同一形状）。
        #
        # `cancelled` 不在此列：S-15 说取消不是失败，它是父侧的主动选择，
        # 父 Run 自己知道（`pending_child` 就此清掉），不构成"被隐瞒的失败"。
        execution_kind = {
            "failed": EXECUTION_FAILED,
            "unknown": EXECUTION_UNRESOLVED,
        }.get(outcome)
        if execution_kind is not None:
            execution = self.kernel.repository.get(execution_id)
            attempt_no = execution.current_attempt_no if execution else 1
            self._apply(
                Observation.from_execution_result(
                    run_id=self.state.run_id,
                    execution_id=execution_id,
                    attempt_no=attempt_no,
                    kind=execution_kind,
                    summary=(
                        f"execution {execution_id} attempt#{attempt_no} "
                        f"{'failed' if outcome == 'failed' else 'unresolved'} "
                        f"({reason or f'child run {outcome}'})"
                    ),
                    content={
                        "status": "failed",
                        "child_run_id": child_run_id,
                        "child_outcome": outcome,
                        "error": reason,
                    },
                )
            )
        # D-12：委派没做成，也要进账本 —— 见 `_record_delegation_unresolved`。
        if action is not None and step is not None and task_id:
            self._record_delegation_unresolved(
                action,
                step,
                task_id=task_id,
                execution_id=execution_id,
                child_run_id=child_run_id,
                outcome=outcome,
            )
        self._clear_pending_child()
        self._sync_after_execution()
        return self._record(StepOutcome.FAILED)

    def _record_delegation_unresolved(
        self,
        action: Action,
        step: Step,
        *,
        task_id: str,
        execution_id: str,
        child_run_id: str,
        outcome: str,
    ) -> None:
        """D-12：委派以 `failed` / `cancelled` 收尾时，账本照样要记一条。

        ------------------------------------------------------------------
        为什么"失败就不记"这条判据对委派不成立

        `SagaCoordinator.record()` 里有一条判据：
        非 COMPLETED 且非 EXTERNAL_UNKNOWN 的失败 → 认为没产生副作用 → 不登记。

        对**单次工具调用**这条判据大致成立：一次调用没做成，多半真的没改到东西。
        但委派派出的是一条**完整的 Run** —— 它在进终态之前可能已经跑了
        任意多步：建了工单、发了邮件、甚至派生了它自己的子 Run。
        于是"失败 = 没副作用"对委派**根本不成立**。

        而它到底留没留下东西，父 Run **无从得知** ——
        子 Run 自己的账本挂在它自己的 `run_id` 下，这里看不到。

        ------------------------------------------------------------------
        所以记的是 UNRESOLVED，不是 PENDING

        UNRESOLVED 的语义正好是这一档：`CompensationStatus` 的注释写的是
        "撤销不了：永久失败 / 缺参数 / 副作用存疑"。
        `claim()` 只认领 PENDING，所以它不会被自动撤销（S-15：取消不自动回滚）；
        而它又留在 `unresolved_for()` 里，于是运维看板**看得见**。
        两种自动行为（撤销 / 当没发生）在这里都是猜。

        `args` 恒为空：撤销参数要从正向结果里取，而这里没有可信结果（S-8）。
        """
        assert self.saga is not None
        # D-19：`unknown` 的尾巴必须比另外两种更啰嗦 ——
        # 它要挡住的是"把不知道读成失败"这一件事。
        # 另外两种都可以说"它 X 了"，而这一种只能说"我们不知道它怎么了，
        # 也不知道它留没留下东西，而且它可能还在跑"。
        # 少说任何一句，看板上的这一行都会被读成"子 Run 失败了"。
        if outcome == "cancelled":
            tail = (
                "S-15: cancellation is 'stop here', not 'undo it' — whatever it "
                "already did stays done"
            )
        elif outcome == "unknown":
            tail = (
                "the wait deadline passed with no result and WE DO NOT KNOW "
                "whether it is still running, so this must not be read as "
                "'failed' — it may still come back and it may still be acting "
                "on the outside world"
            )
        else:
            tail = "whether it left anything behind cannot be known from here"
        self.saga.record_unresolved(
            run_id=self.state.run_id if self.state is not None else "",
            step_id=step.step_id,
            task_id=task_id,
            execution_id=execution_id,
            action=action,
            reason=(
                f"D-12: child run {child_run_id} "
                f"{_child_verb(outcome) if outcome == 'unknown' else f'ended {outcome}'}; "
                f"a child "
                f"run is a whole run that may have executed many steps before its "
                f"terminal state, so 'failed implies no side effect' does not hold "
                f"for delegation — {tail}, and its own ledger lives under its own "
                f"run_id, so this must not be auto-compensated and must not be "
                f"treated as if nothing happened"
            ),
        )

    @property
    def child_handle(self) -> ChildRunHandle | None:
        """`child_identity.handle` 的读法。

        留这个访问器不是图省事：`ChildRunIdentity` 是**写入侧**的概念
        （我有结果要汇报给谁），而绝大多数读的地方只关心"我是不是子 Run、
        我的父 Run 是谁"。让它们去碰 `identity.registry` 会暗示
        "读的人也可以写"，而写只有一处（终态发射）。
        """
        return self.child_identity.handle if self.child_identity is not None else None

    def _emit_child_run_outcome(
        self, status: "AgentRunStatus", *, reason: str
    ) -> None:
        """空洞 209：本 Run 若是一条被登记过的子 Run，向 Outbox 发射终态事件。

        ------------------------------------------------------------------
        D-37：`reason` 是**必填关键字参数**

        B-12 让子 Run 的终态带上了死因，但那个原因原先只落在子 Run 自己的
        trace 上，没有跟着事件走 —— 于是父 Run 听到的是子 Run **最后一句
        自言自语**（`summary`），而不是它的死因。实测（M80 探针）：

            子 Run 死因   step budget exhausted (2/2)
            父 Run 听到   child run failed: execution exec_xxx attempt#1
                          COMPLETED (completed)

        **不是说漏了，是说反了**：父 Run 被告知"一个已完成的执行导致了失败"。

        把 `reason` 做成必填关键字参数，是为了让"发终态事件"这个动作
        在签名上就离不开死因 —— 新增一个发射点时，不给原因就构造不出
        这次调用（§0.8 那条：默认值的诱惑在于它会让"忘了说为什么"
        看起来像"没什么可说的"）。

        ------------------------------------------------------------------
        为什么必须在这里发，而不是让父 Run 去问

        "父 Run 去问子 Run 好了吗"是轮询，而轮询在跨进程时有两个硬伤：
        一是父 Run 每次被推进都要多一次查询；二是**父 Run 不被推进时
        永远没人问** —— 而它恰恰在等子 Run，不会自己往前走（D-5）。

        所以是**子 Run 主动说**：它进入终态的那一刻（本方法是唯一出口，
        B-7）把事件写进 Outbox，由 OutboxPublisher 投到 Kafka，
        唤醒路径再来叫醒父 Run。

        ------------------------------------------------------------------
        为什么走 Outbox 而不是直接发 Kafka（X-3 / §36）

        直接发的话，"子 Run 已终态"这个状态写和"通知父 Run"这个事件写
        不在同一个事务里：进程在中间崩掉会留下一条**永远不会被叫醒**的
        父 Execution，而且不报错。Outbox 让两者一起生效。

        同时 §36 明令禁止把 Kafka 当任务队列 —— 这里发的是**事实**
        （这条子 Run 结束了），不是命令（你去把父 Run 推一步）。

        ------------------------------------------------------------------
        判据：不是子 Run 就什么都不做

        `registry.for_child()` 查不到 = 这条 Run 不是任何人的子 Run
        （它是一条顶层 Run），那就不该发。发一个没有父 Run 的
        `child_run.completed` 会让消费端拿着 `parent_run_id=""` 去恢复，
        报出来的错会和真正的故障混在一起。
        """
        identity = self.child_identity
        if identity is None:
            return
        handle = identity.handle
        registry = identity.registry
        assert self.agent_run is not None

        state = self.state
        result: dict[str, Any] = {
            "status": status.value,
            "steps": self.steps,
            # D-37：死因跟着结果一起走。`summary` 是"最后一条 observation"，
            # 它是**过程**不是**原因** —— 两者不能互相顶替（见 `_reason`）。
            "reason": reason,
            "summary": (
                state.observations[-1].summary
                if state is not None and state.observations
                else ""
            ),
        }
        # X-5：结果**先**落到登记处（PG），再发事件。
        #
        # 两者在同一个事务里（X-3 / PR-30），所以这不是"先后顺序影响原子性"，
        # 而是"写不进去就不许说"：若这条子 Run 已经是另一个终态，
        # `mark_finished` 会抛（B-3），事件也就不会发出去 ——
        # 否则父 Run 会拿着一个 PG 里并不存在的结论继续往前走，
        # 而 PG 是唯一 Truth（X-5），那种"结论"没有第二种查证方式。
        registry.mark_finished(handle.child_run_id, status.value, result)

        if status is AgentRunStatus.COMPLETED:
            event_type = CHILD_RUN_COMPLETED
        elif status is AgentRunStatus.CANCELLED:
            event_type = CHILD_RUN_CANCELLED
        else:
            event_type = CHILD_RUN_FAILED
        self.kernel.outbox.append(
            [
                new_event(
                    aggregate_type="run",
                    aggregate_id=handle.parent_run_id,
                    event_type=event_type,
                    payload={
                        # 唤醒路径手上只有 child_run_id（R-6），
                        # 但恢复父 Run 需要 parent_run_id —— 两个都带上。
                        "child_run_id": handle.child_run_id,
                        "parent_run_id": handle.parent_run_id,
                        "parent_execution_id": handle.parent_execution_id,
                        "child_kind": handle.kind.value,
                        "target": handle.target,
                        "status": status.value,
                        "result": result,
                    },
                )
            ]
        )

    def _close_child_gate(
        self, execution_id: str, *, outcome: str, result: Mapping[str, Any]
    ) -> None:
        """`resume()` → `complete()` / `fail()`。**顺序不能反**（X-11 同款理由）。

        先 complete 再 resume 的话，进程在中间崩了会留下一条永远 SUSPENDED 的
        Execution，Recovery 也救不回来 —— 它等的那条子 Run 已经跑完了，
        再也不会有人来叫醒它，而且**没有任何报错**。

        ------------------------------------------------------------------
        `outcome` 而不是 `completed: bool`

        第二参原本是布尔，于是"失败"和"取消"共用同一个分支，
        FailureClass 只能写死一个值 —— 那就是 M31 修掉的那个洞：
        写死成 TRANSIENT，取消也被当成可重试的失败（S-15）。
        """
        if not execution_id:
            return
        if self.kernel.status_of(execution_id) in TERMINAL_EXECUTION_STATUSES:
            return
        _, lease = self.kernel.resume(execution_id, worker_id="child-loop")
        if outcome == "completed":
            self.kernel.complete(
                execution_id, token=lease.fencing_token, result=dict(result)
            )
            return
        code, failure_class = CHILD_OUTCOME_FAILURE[outcome]
        self.kernel.fail(
            execution_id,
            token=lease.fencing_token,
            error=ErrorInfo(
                code=code,
                message=str(result.get("error") or f"child run {outcome}"),
                failure_class=failure_class,
                # 真因必须挂在 error 上：父 Execution 的终态 error 是排障的
                # 第一入口，而委派 Execution 在 Worker 侧只有"被拒绝"一种归宿，
                # 不写进去就会被那句 DELEGATION_NOT_WORKER_EXECUTABLE 盖掉（PR-19）。
                details={"child_outcome": outcome},
            ),
        )

    def _must_pending_child(self, child_run_id: str) -> ChildRunHandle:
        if self.pending_child is None:
            raise InvariantViolation("X-11: nothing pending a child run")
        if self.pending_child.child_run_id != child_run_id:
            raise InvariantViolation(
                f"D-3: child run {child_run_id!r} is not the one this run is waiting "
                f"for ({self.pending_child.child_run_id!r})"
            )
        return self.pending_child

    def _clear_pending_child(self) -> None:
        self.pending_child = None
        self.pending_child_task_id = ""
        self.pending_action = None

    def _denied(self, action: Action, verdict) -> StepOutcome:
        """DENY → **不产生 Task**，直接回 Observation（基线 §2）。

        这是 DENY 与 REQUIRE_APPROVAL 的关键区别：
          · DENY 的 Action 永远不会执行，所以 Kernel 里不该有它的痕迹
          · 但它必须进 State —— 否则 Agent 不知道自己被拒了，会一直重试同一个动作
        """
        state = self.state
        assert state is not None
        self._apply(
            Observation(
                run_id=state.run_id,
                kind=POLICY_DENIED,
                summary=f"action {action.action_type.value} denied by harness",
                content={
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                    "reasons": list(verdict.reasons),
                },
            )
        )
        # L-7：连续被拒到上限 → 这个 Run 已经证明自己走不下去，判 FAILED。
        # 不这么做的话 Loop 会一直转（被拒不消耗任何预算），
        # 而且**没有任何报错** —— 它只是在每轮多写一条 DENY 的 Observation。
        outcome = self._record(StepOutcome.DENIED)
        if self.consecutive_denials >= self.config.max_consecutive_denials:
            self._declare_terminal(
                AgentRunStatus.FAILED,
                reason=(
                    f"denied {self.consecutive_denials} times in a row "
                    f"(limit {self.config.max_consecutive_denials}); "
                    "the run keeps proposing actions the harness refuses"
                ),
            )
            return self._record(StepOutcome.DENY_LOOP)
        return outcome

    def _close_gate(self, approval: ApprovalRequest, *, approved: bool) -> None:
        """把 Kernel 里那条 SUSPENDED 的审批 Execution 走完（resume → complete）。

        `resume()` = SUSPENDED → PENDING → Claim → RUNNING，拿回新的 fencing_token。
        这条路径与 Worker 的 Race 无关（审批只有一个人会来），
        但**同样受状态机约束**：如果它已经被取消或已终态，这里会抛异常 —— 这是对的。
        """
        execution_id = approval.execution_id
        if execution_id is None:
            return
        if self.kernel.status_of(execution_id) in TERMINAL_EXECUTION_STATUSES:
            return
        _, lease = self.kernel.resume(execution_id, worker_id="human-loop")
        self.kernel.complete(
            execution_id,
            token=lease.fencing_token,
            result={"approved": approved, "approval_id": approval.approval_id},
        )

    def _must_pending(self) -> ApprovalRequest:
        if self.pending_approval is None:
            raise InvariantViolation("X-11: nothing pending approval")
        return self.pending_approval

    def _clear_pending(self) -> None:
        self.pending_approval = None
        self.pending_action = None

    def _plan(self) -> bool:
        """规划。**返回 False** 表示"这不是一份新计划"（I-12）。

        Plan 以 Observation 的形式进 State —— 不允许 Loop 直接赋值（I-3）。

        ------------------------------------------------------------------
        I-12：重规划必须真的换一份计划

        `Planner.plan()` 每次都被调用、每次都返回**一份新的 Plan 对象**
        （`plan_id` 是 `new_id()`，必然不同）。于是从对象上看，
        每一次重规划都"换了一份计划" —— 但探针实测：

            #0: plan_id=plan_53a5…  shape=(('n0','step-0','task'), ('n1','step-1','task'))
            #1: plan_id=plan_7bd2…  shape=(('n0','step-0','task'), ('n1','step-1','task'))

        **形状一模一样。** 也就是说系统在"换一条路"，换来的路和原来那条
        是同一条 —— 它只是把同一份计划重新造了一遍，然后照着它再撞一次墙。

        更糟的是它顺带把 I-11 洗白了：

            失败 → REPLAN → 拿到一份（形状相同的）新计划
                 → I-11 只看"上次规划以来"，此时为 0
                 → FINISH → **completed**

        一个失败过的 Run，多花一轮重规划就又"成功"了。
        I-11 形同虚设。

        所以判据不能只看"有没有重新规划"，要看"**换出来的那份是不是另一条路**"。
        """
        state = self.state
        assert state is not None
        plan = self.planner.plan(state)
        if self._is_same_as_invalidated(plan):
            return False
        self._apply(
            Observation(
                run_id=state.run_id,
                kind=PLAN_CREATED,
                summary=f"plan created with {len(plan.nodes)} nodes",
                content={"plan": plan, "plan_id": plan.plan_id},
            )
        )
        return True

    def _is_same_as_invalidated(self, plan: Plan) -> bool:
        """这份计划与被作废的那份是不是**同一条路**（I-12 的判据）。

        比的是**形状**不是对象：`plan_id` 每次都是新的，比对象等于永远"不同"。

        刻意在**作废之前**那份上取：最近一次 `PLAN_INVALIDATED`
        往前找到的第一个 `PLAN_CREATED`。
        """
        observations = self.state.observations if self.state is not None else []
        invalidated = -1
        for i, obs in enumerate(observations):
            if obs.kind == PLAN_INVALIDATED:
                invalidated = i
        if invalidated < 0:
            return False                      # 还没有作废过 —— 这是第一次规划
        for j in range(invalidated - 1, -1, -1):
            if observations[j].kind == PLAN_CREATED:
                previous = observations[j].content.get("plan")
                if isinstance(previous, Plan):
                    return _plan_shape(previous) == _plan_shape(plan)
                return False
        return False

    def _apply_plan_invalidated(self) -> None:
        state = self.state
        assert state is not None
        self._apply(
            Observation(
                run_id=state.run_id,
                kind=PLAN_INVALIDATED,
                summary="plan invalidated; replanning",
            )
        )

    def _failures_since_last_plan(self) -> int:
        """自**最近一次规划**以来，有过几次执行失败（I-11 的判据）。

        ------------------------------------------------------------------
        为什么按"上次规划"划界，而不是数全部失败

        数全部失败的话，一个"第一步失败、换计划后成功"的 Run
        将**永远**无法完成 —— 失败记录不会消失，它会一直挡在 FINISH 前面，
        直到预算耗尽把它判死。那等于把"可以救回来的失败"也判了死刑。

        按规划划界表达的是 REPLAN 的语义本身：

            这份计划下失败了 → 这份计划行不通 → 换一份 → 新计划下重新算

        于是"换计划后成功"是能完成的，"换了还是失败"最终会走到 FAILED。

        ------------------------------------------------------------------
        为什么从 Observation 推，不用一个计数器

        一个 `self.failures` 计数器在某些恢复路径上会丢（快照重载、
        进程重启），于是"有没有失败过"这个问题在重启前后给出不同的答案。
        从 `state.observations` 推则没有这个问题 —— 它就是事实本身，
        而且它是 I-3 唯一允许进入 State 的那种东西。

        ------------------------------------------------------------------
        I-14：查不出来的失败也算

        原先这里只数 `EXECUTION_FAILED`。但"失败"这个事实有两扇门，
        而 M81/M82 才把第二扇补上：

            执行真的失败了     → observation kind 是 `execution_failed`
            委派等不到回音     → 我们**不知道**它失败没失败（D-19），
                                 → `execution_unresolved`

        后者如果不算，父 Run 就可以带着"这一步什么都没拿到"宣布
        `goal reached` —— 而那正是 I-11 治掉的谎言。

        判据的名字仍然是"失败"，但它的语义是**"这一步没有被证明成功"**：
        证明不了的，和证明失败的，一样不该被人当成功汇报。
        """
        if self.state is None:
            return 0
        observations = self.state.observations
        last_plan = -1
        for i, obs in enumerate(observations):
            if obs.kind == PLAN_CREATED:
                last_plan = i
        return sum(
            1
            for obs in observations[last_plan + 1 :]
            if obs.kind in (EXECUTION_FAILED, EXECUTION_UNRESOLVED)
        )

    def _execute(self, action: Action) -> StepOutcome:
        state = self.state
        assert state is not None

        # B-6：Task 必须挂到当前 Step 上。
        # TaskFactory 默认会 `new_step_id()` —— 那等于每个 Task 开一个 Step，
        # 把 `Step : Task = 1 : N`（基线 §4）悄悄降成 1:1，扇出能力随之消失。
        step = self._ensure_step()
        task = self.task_factory.from_action(
            action, step_id=step.step_id, extra_payload=self._context_payload(action)
        )
        execution = self.kernel.submit(task)             # X-1：交棒
        step.add_task(task.task_id)
        self._trace(
            SUBMITTED,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            attempt_no=execution.current_attempt_no,
            payload={"action_type": action.action_type.value},
        )

        # 走真正的调度路径（Scheduler → Atomic Claim → Executor）
        outcomes = self.worker.run_once(limit=1)
        outcome = outcomes.get(execution.execution_id, WorkerOutcome.FAILED)

        self._observe(execution.execution_id, outcome)

        # S-1：正向执行留下副作用 → 登记一笔"待撤销"。
        # 必须在这里记，因为**只有这一刻**同时握着 Action（含逆操作声明）
        # 与执行结果（撤销参数要从结果里取）。事后再记，这两样都拿不到了。
        self._record_compensation(action, step, task.task_id, execution.execution_id)

        after = self.kernel.repository.get(execution.execution_id)
        attempt_no = after.current_attempt_no if after else 1
        result = self._attempt_result(execution.execution_id, attempt_no)
        self._trace(
            OBSERVED,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            attempt_no=attempt_no,
            # 记**实际服务值**（model / deployment / version），不是请求值
            served=served_from_result(result),
            payload={
                "status": self.kernel.status_of(execution.execution_id).value,
                "outcome": outcome.value,
            },
        )

        self.steps += 1
        self._sync_after_execution()
        # 记账交给 Harness —— 预算是拦截条件，不是事后账单
        self._charge(steps=1, result=result)
        # L-5：Step 完成也要落 Run Checkpoint（§14 第一行表格）
        if self._step_is_done():
            self._write_run_checkpoint(reason="step completed")
        return self._record(
            StepOutcome.EXECUTED if outcome is WorkerOutcome.COMPLETED else StepOutcome.FAILED
        )

    # ------------------------------------------------------------ 补偿（M10）
    def _record_compensation(
        self,
        action: Action,
        step: Step,
        task_id: str,
        execution_id: str,
    ) -> None:
        """正向执行结束后登记补偿（S-1 / S-2 / S-8 / S-11）。

        M25：第四参从 `execution` 对象改成 `execution_id`。
        原来只有 `_execute()` 一个调用点，手上正好有完整对象；
        子 Run 收口那条路径只有 id，于是就会有人去"造一个假 execution 传进去" ——
        那种造假一时的确能跑，代价是**读这段代码的人再也不知道这里真正需要什么**。

        M26：第三参同理，从 `Task` 对象改成 `task_id`。
        恢复出来的父 Run 手上只有子 Run 的 handle，**没有** Task 对象，
        而 `CompensationRecord` 需要的就是那一个 id。
        """
        assert self.saga is not None
        if not task_id:
            # 账本要能说清"这笔副作用是哪条 Task 产生的"。缺了它不是退化，是损坏。
            raise InvariantViolation(
                "S-1: cannot record a compensation without a task_id; the ledger "
                "would not be able to say which task produced the side effect"
            )
        if action.compensation is None:
            return
        state = self.state
        assert state is not None

        status = self.kernel.status_of(execution_id)
        after = self.kernel.repository.get(execution_id)
        attempt_no = after.current_attempt_no if after else 1
        result = self._attempt_result(execution_id, attempt_no)
        failure_class = self._failure_class(execution_id, attempt_no)

        record = self.saga.record(
            run_id=state.run_id,
            step_id=step.step_id,
            task_id=task_id,
            execution_id=execution_id,
            action=action,
            result=result,
            execution_status=status,
            failure_class=failure_class,
        )
        if record is None:
            return
        self._trace(
            COMPENSATION_RECORDED,
            step_id=step.step_id,
            task_id=task_id,
            execution_id=execution_id,
            payload={
                "compensation_id": record.compensation_id,
                "status": record.status.value,
                "tool": record.tool,
                "reason": record.reason,
            },
        )

    def _failure_class(self, execution_id: str, attempt_no: int) -> str | None:
        attempts = getattr(self.kernel, "attempts", None)
        if attempts is None:
            return None
        attempt = attempts.get(execution_id, attempt_no)
        error = getattr(attempt, "error", None) if attempt is not None else None
        cls = getattr(error, "failure_class", None) if error is not None else None
        return cls.value if cls is not None else None

    def _compensation_step(self) -> Step:
        """S-12：补偿挂在**独立的 Step** 上。

        挂在原 Step 上的话，撤销 Task 会把已完成 Step 的派生状态倒推回去
        （Step 状态是从它的 Execution 投影出来的 —— 见 B-5），
        于是"这一步已经完成了"会变成"这一步还在跑"，而且撤销一结束它又变回完成。
        """
        state = self.state
        assert state is not None
        if self._compensation_step_id:
            existing = next(
                (s for s in self.steps_of_run if s.step_id == self._compensation_step_id),
                None,
            )
            if existing is not None:
                return existing
        step = Step(
            run_id=state.run_id,
            plan_node_id="compensation",
            name="compensate",
        )
        self._compensation_step_id = step.step_id
        self.steps_of_run.append(step)
        return step

    def _execute_compensation(self, action: Action) -> bool:
        """S-9：撤销动作**不再过 Harness**。

        这不是绕过策略，而是策略已经在正向动作提出时连同逆操作一起审过了
        （见 `Harness.before_action` 对 `action.compensation` 的处理），
        人的批准也是同时覆盖两者的。

        反过来想就明白为什么必须这样：如果撤销也要等人批准，
        那么"人走了、Run 失败了"这种情况会让副作用**永久留着** ——
        而且没有任何报错，因为系统确实在"等批准"。

        S-10：撤销**不计步数、不计费**。预算耗尽恰恰是最需要清理的时刻，
        让清理去跟正向动作抢同一份预算，等于宣布"烧完钱的那次运行不许打扫"。
        """
        step = self._compensation_step()
        task = self.task_factory.from_action(action, step_id=step.step_id)
        execution = self.kernel.submit(task)
        step.add_task(task.task_id)
        self._trace(
            COMPENSATION_STARTED,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            payload={
                "tool": action.payload.get("tool", ""),
                "compensation_for": action.rationale,
            },
        )
        outcomes = self.worker.run_once(limit=1)
        outcome = outcomes.get(execution.execution_id, WorkerOutcome.FAILED)
        ok = outcome is WorkerOutcome.COMPLETED
        self._trace(
            COMPENSATION_DONE,
            step_id=step.step_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            payload={"ok": ok, "status": self.kernel.status_of(execution.execution_id).value},
        )
        return ok

    def _compensate(self, reason: str) -> None:
        """Run 判定 FAILED 之前，把副作用撤销掉。"""
        assert self.saga is not None
        state = self.state
        assert state is not None
        result = self.saga.compensate(
            state.run_id,
            executor=self._execute_compensation,
            risk_level=RiskLevel.HIGH,
        )
        if result.attempted == 0 and result.skipped == 0:
            return
        self._trace(
            COMPENSATION_FINISHED,
            payload={
                "reason": reason,
                "attempted": result.attempted,
                "compensated": result.compensated,
                "unresolved": result.unresolved,
                "unresolved_ids": list(result.unresolved_ids),
            },
        )
        if not result.clean:
            # S-5：撤销不干净**不能静默**。Run 照样判 FAILED（否则永远结束不了），
            # 但账本里留着 UNRESOLVED，运维能按 idx_compensations_unresolved 扫出来。
            self._trace(
                COMPENSATION_UNRESOLVED,
                payload={
                    "unresolved_ids": list(result.unresolved_ids),
                    "reason": reason,
                },
            )

    def _context_payload(self, action: Action) -> Mapping[str, Any] | None:
        """M17：LLM 调用之前组装 Context，并留下 Snapshot（C-2 / C-5 / C-11）。

        **为什么随 Task 走，而不是塞进 State：**
        Context 是**这一次调用**的输入（C-2）。做成 Run 级单例的话，
        第 5 次调用的 Snapshot 会盖掉第 1 次的，于是
        "它第一次为什么这么答"这个问题永远查不到。

        **为什么是 Runtime 组装而不是 Harness：**
        把内容拼进 Context 是**执行**的一部分；Harness 管的是
        "这些内容能不能进"（权限 / 敏感信息 / 越权检索）—— 那是准入，不是组装（C-11）。
        """
        if self.context_assembler is None:
            return None
        if action.action_type is not ActionType.LLM_CALL:
            return None
        state = self.state
        assert state is not None

        build = self.context_assembler.build(
            ContextRequest(
                run_id=state.run_id,
                model_id=str(action.payload.get("model") or ""),
                system=str(action.payload.get("system") or state.goal.objective),
                messages=(("user", state.goal.objective),),
                runtime_state={"step": self.steps, "completed": len(state.completed_tasks)},
            )
        )
        self._trace(
            CONTEXT,
            step_id=self.current_step.step_id if self.current_step else "",
            payload={
                "snapshot_id": build.snapshot.snapshot_id,
                "total_tokens": build.total_tokens,
                "dropped": len(build.plan.dropped),
            },
        )
        return {
            "context": list(build.render()),
            "context_snapshot_id": build.snapshot.snapshot_id,
        }

    def _charge(self, *, steps: int, result: Mapping[str, Any] | None) -> None:
        """L-3：Token 用量必须回到 CostManager，否则预算这条线是断的。

        后果不是"少一张账单"这么轻。`CostManager` 是 `before_action` 的一环
        （预算耗尽 → DENY），如果 `spent_tokens` 永远是 0，那么
        `Budget.max_tokens` 就是一条接了但没通电的线 ——
        超 token 的 Run 会一直跑到把钱烧完才停。
        """
        assert self.harness is not None
        usage = (result or {}).get("usage") or {}
        self.harness.charge(steps=steps, tokens=int(usage.get("total_tokens", 0)))

    def _trace(self, kind: str, **kwargs: Any) -> None:
        self.trace.append(kind, **kwargs)

    def _observe(self, execution_id: str, outcome: WorkerOutcome) -> None:
        """I-6：来自执行的 Observation 只能由 `from_execution_result` 构造。"""
        state = self.state
        assert state is not None
        execution = self.kernel.repository.get(execution_id)
        attempt_no = execution.current_attempt_no if execution else 1
        status = self.kernel.status_of(execution_id)
        succeeded = status is ExecutionStatus.COMPLETED

        content: dict[str, Any] = {
            "status": status.value,
            "worker_outcome": outcome.value,
            "attempt_no": attempt_no,
        }
        result = self._attempt_result(execution_id, attempt_no)
        if result is not None:
            content["result"] = result

        self._apply(
            Observation.from_execution_result(
                run_id=state.run_id,
                execution_id=execution_id,
                attempt_no=attempt_no,
                kind=EXECUTION_RESULT if succeeded else EXECUTION_FAILED,
                summary=(
                    f"execution {execution_id} attempt#{attempt_no} "
                    f"{status.value} ({outcome.value})"
                ),
                content=content,
            )
        )

    def _attempt_result(self, execution_id: str, attempt_no: int) -> Mapping[str, Any] | None:
        attempts = getattr(self.kernel, "attempts", None)
        if attempts is None:
            return None
        attempt = attempts.get(execution_id, attempt_no)
        return attempt.result if attempt is not None else None

    def _finish(self) -> StepOutcome:
        state = self.state
        assert state is not None
        self._apply(
            Observation(run_id=state.run_id, kind=RUN_FINISHED, summary="goal reached")
        )
        # Goal 达成是 **Runtime 判定的终态**：下层没有任何一条记录会说
        # "这个 Run 完成了" —— Task / Execution 只知道自己的成败，
        # 所有 Step 也可能确实都 COMPLETED 了但 Agent 判断目标没达成。
        # 所以"跑完了"这件事只能从这里显式传下去（否则 Run 会停在一个中间态）。
        self._declare_terminal(AgentRunStatus.COMPLETED, reason="goal reached")
        return self._record(StepOutcome.FINISHED)

    def _declare_terminal(self, status: AgentRunStatus, *, reason: str) -> None:
        """B-7：终态只能由 Runtime 声明，且必须留痕。

        Trace 里的这条 `run.finished` 是"谁宣布了这个 Run 结束"的**唯一证据** ——
        下层没有任何一条记录会说 Run 结束了（Task / Execution 只知道自己的成败）。

        **补偿必须挂在这里**（S-7）：`_declare_terminal` 是 FAILED 的唯一出口，
        挂在各个调用点上迟早会漏掉一个（而漏掉的那个恰好就是一个"失败却不清理"的 Run）。

        ------------------------------------------------------------------
        B-12：终态必须带上**原因**

        把 FAILED 收成一个出口是对的（S-7），代价是：**四种不同的死法
        从这里出去之后长得一模一样** —— 探针实测：

            A 预算耗尽      run.finished | {'status': 'failed'}
            B 换不出新计划   run.finished | {'status': 'failed'}

        而那段 docstring 自己写着这条 trace 是"唯一证据"。
        一条不写原因的唯一证据，等于**唯一证据里没有最关键的那个字**。

        运维拿到的是账本，不是 `loop.history`：
        `history` / `last_outcome` 只在内存里，快照不带它们，进程一死就蒸发。
        所以"这个 Run 为什么失败"在可审计的记录上**无从查证** ——
        这正是不变量 F-1 已经治过一次的那类病（"三种停在页面上是同一副样子"），
        只不过这次是同一个 FAILED 内部的三种成因。

        于是 `reason` 是**必填关键字参数**：新增一个出口时，
        不写原因就构造不出这次调用。默认值的诱惑在于它会让
        "忘了说为什么"看起来像"没什么可说的"。
        """
        if status is AgentRunStatus.FAILED:
            self._compensate(reason="run failed")

        if status is AgentRunStatus.COMPLETED:
            # S-16：成功 = 副作用按预期保留，不需要撤销 —— 把账本结案。
            # 不结案的话，每一次成功运行都会留下一串"待撤销"。
            released = self.saga.release(self.state.run_id) if self.saga else 0
            if released:
                self._trace(COMPENSATION_RELEASED, payload={"released": released})

        if status is AgentRunStatus.CANCELLED:
            # S-15：取消**不自动撤销**。
            #
            # 取消表达的是"到此为止"，不是"撤销重来" —— 自动撤销一个用户主动取消的
            # Run，多数时候不是用户想要的。但也不能悄悄不管：账本里那些 PENDING
            # 必须留在那儿，并在 Trace 里留下"有多少笔待办"，否则取消就变成
            # "系统悄悄留下了一堆外部副作用"。
            pending = [
                r for r in self.saga.open_items(self.state.run_id)
            ] if (self.saga is not None and self.state is not None) else []
            if pending:
                self._trace(
                    COMPENSATION_DEFERRED,
                    payload={
                        "reason": "cancelled: compensation deferred to a human",
                        "unresolved_ids": [r.execution_id for r in pending],
                        "deferred": len(pending),
                    },
                )

        self._sync_run(terminal=status)
        self._trace(FINISHED, payload={"status": status.value, "reason": reason})
        # 空洞 209：子 Run 的终态必须**自己说出来**。
        # 它内部那些 execution.* 事件没有一个会说"这一整条子 Run 结束了"，
        # 而父 Run 等的就是这一句 —— 于是委派的结果永远回不来。
        self._emit_child_run_outcome(status, reason=reason)

    def _apply(self, obs: Observation) -> None:
        state = self.state
        assert state is not None
        # X-10：带 expected_version，冲突即抛 ConcurrentStateError（不允许静默覆盖）
        state.apply(obs, self.reducer, expected_version=state.version)

    def _record(self, outcome: StepOutcome) -> StepOutcome:
        if outcome is StepOutcome.DENIED:
            self.consecutive_denials += 1
        else:
            self.consecutive_denials = 0          # L-7：换了动作就清零
        self.history.append(outcome)
        return outcome
