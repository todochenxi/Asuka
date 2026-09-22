"""状态派生：把 Kernel 的执行状态投影成 Business 的业务状态（基线 §9.2）。

    Execution.status ──► Step.status ──► AgentRun.status
      （Kernel）          （Business）      （Business）

**为什么叫"派生"而不是"状态机"？**

基线 §9.2 钉死了机制与策略分离：

    Kernel     提供机制：状态定义 / 转换合法性 / 原子写入
    Runtime    决定策略：什么时候发起转换、转换到哪个状态

而 AgentRun / Step 更进一步 —— 它们连"被转换"都没有，
**状态是从下层投影上来的**。这带来一个直接好处：

> 不存在"Run 说 COMPLETED 了但还有 Task 在跑"这种不一致，
> 因为压根没有第二个地方能记这个状态。

`AgentRunStateMachine` 因此**不是**转换器，它只回答"这个投影结果合不合法"。
投影出一个非法跳转（比如 COMPLETED → RUNNING）说明下层语义出了问题，必须炸掉而不是默默接受。
"""
from __future__ import annotations

from typing import Sequence

from ..errors import IllegalTransition
from ..execution.execution import ExecutionStatus

from .run import AgentRunStatus
from .step import StepStatus


# ============================================================ Step


def derive_step_status(statuses: Sequence[ExecutionStatus]) -> StepStatus:
    """Step.status ← 所属 Task 的 Execution 状态。

    判定顺序（**活跃态优先于终态**）：

        1  还没有 Task              → PENDING
        2  有 RUNNING               → RUNNING
        3  有 SUSPENDED             → SUSPENDED
        4  有 PENDING / STALE       → PENDING   （还有活没干完）
        5  有 CANCELLED             → CANCELLED
        6  有 FAILED                → FAILED
        7  否则                     → COMPLETED

    第 4 条是关键：Execution 可重试失败时会回到 PENDING（§9.2），
    此时 Step 必须跟着回到 PENDING，不能因为"有个 Task 曾经 RUNNING 过"就判 FAILED。

    注意 Step **没有 STALE** 这个状态 —— Lease 过期是基础设施细节，
    Step 是业务视角，它只看到"这一步还没做完"。
    """
    if not statuses:
        return StepStatus.PENDING
    s = set(statuses)

    if ExecutionStatus.RUNNING in s:
        return StepStatus.RUNNING
    if ExecutionStatus.SUSPENDED in s:
        return StepStatus.SUSPENDED
    if ExecutionStatus.PENDING in s or ExecutionStatus.STALE in s:
        return StepStatus.PENDING
    if ExecutionStatus.CANCELLED in s:
        return StepStatus.CANCELLED
    if ExecutionStatus.FAILED in s:
        return StepStatus.FAILED
    return StepStatus.COMPLETED


# ============================================================ AgentRun


def derive_run_status(
    step_statuses: Sequence[StepStatus],
    *,
    runtime_terminal: AgentRunStatus | None = None,
) -> AgentRunStatus:
    """AgentRun.status ← 所有 Step 的状态。

    `runtime_terminal` 是 Runtime 判定的终态：
        · Goal 达成       → COMPLETED
        · 主动放弃 / 预算耗尽 → FAILED
        · 取消            → CANCELLED

    **B-7：派生只产出活跃态；三个终态全部由 Runtime 声明。**

    这一条是被实现逼出来的，不是拍脑袋。最初版本会从 Step 派生
    COMPLETED / FAILED，结果在 `test_budget_exhausted_stops_the_loop` 里直接崩了：

        Run 已经 COMPLETED，预算耗尽想标 FAILED → B-3 拒绝

    崩得对，因为"所有 Step 都 COMPLETED"根本不代表"Run 完成了"：

      · Agent 下一步可能还要 REPLAN（Step 全绿但目标没达成）
      · Agent 可能还在**思考** —— 思考不产生 Task，所以它在派生里是看不见的
      · 一个 Step 永久失败后，Runtime 可以选择重规划而不是放弃

    换句话说：**"手上的活干完了" ≠ "这个 Run 结束了"**。
    前者是 Kernel 的事实，后者是 Runtime（乃至人）的判断。
    把判断降格成聚合，就等于让 Kernel 替 Runtime 决定什么时候收工。

    所以"全部 Step 已终态但 Runtime 还没说话"时，Run 是 RUNNING ——
    它确实还活着，Loop 正握着它。
    """
    if runtime_terminal is not None:
        return runtime_terminal
    if not step_statuses:
        return AgentRunStatus.CREATED          # 一个 Step 都没产生

    s = set(step_statuses)

    if StepStatus.RUNNING in s:
        return AgentRunStatus.RUNNING
    if StepStatus.SUSPENDED in s:
        return AgentRunStatus.SUSPENDED
    if StepStatus.PENDING in s:
        return AgentRunStatus.QUEUED           # 有活排队
    # B-7：剩下的情况（COMPLETED / FAILED / CANCELLED 的混合）一律 RUNNING ——
    # Run 死没死这件事只有 Runtime 说了算。
    return AgentRunStatus.RUNNING


# ============================================================ 机制（只校验，不发起）


_LEGAL: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.CREATED: frozenset(
        {
            AgentRunStatus.QUEUED,
            AgentRunStatus.RUNNING,
            # 基线 §10 的生命周期图没画这条边，但它是真实存在的：
            # Run 创建出来后的**第一个动作**就被 Policy 判成 REQUIRE_APPROVAL，
            # 于是第一个 Step 直接 SUSPENDED，Run 从未 RUNNING 过就挂住了。
            #
            # 之所以不能"先走一遍 RUNNING 再 SUSPENDED"来绕开它：
            # 投影必须是**顺序无关的纯函数**（同一份下层状态必须投影出同一个值），
            # 不能依赖"恰好在中间采样过一次"。否则从 Checkpoint 恢复时
            # 采不到那一帧，同一个状态会投影出两种结果。
            AgentRunStatus.SUSPENDED,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.QUEUED: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.SUSPENDED,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.QUEUED,            # 可重试失败：Task 回到 PENDING 等重新调度
            AgentRunStatus.SUSPENDED,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.SUSPENDED: frozenset(
        {
            AgentRunStatus.QUEUED,            # Wake-up：先变 Runnable，再被 Claim
            AgentRunStatus.RUNNING,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.COMPLETED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
}


class AgentRunStateMachine:
    """AgentRun 的状态机**机制**（基线 §9.2）。

    它只回答"能不能转"，**不发起任何转换** —— 转换全是派生出来的。
    所以这里没有 `transition()`，只有 `can_transition()`。
    """

    def can_transition(self, current: AgentRunStatus, target: AgentRunStatus) -> bool:
        if current is target:
            return True
        allowed = _LEGAL.get(current, frozenset())
        if target not in allowed:
            raise IllegalTransition(
                f"illegal AgentRun transition: {current.value} → {target.value}"
            )
        return True

    def is_terminal(self, status: AgentRunStatus) -> bool:
        return not _LEGAL.get(status, frozenset())
