"""Wake-up Controller：把挂起的 Execution 重新变成 Runnable Task。

    SUSPENDED + Wait Condition
        ↓  Event / Timer / Approval 到达
    Wake-up（属 Kernel）
        ↓
    PENDING（Runnable Task）
        ↓
    Scheduler → Worker → RUNNING

注意：Wake-up 的产物是 **Runnable Task**，不是直接把状态改成 RUNNING。
执行权始终由 Scheduler / Worker 的 Claim 决定，Kernel 不代劳。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Mapping, Any

from packages.agent_domain.execution import ExecutionStatus, Suspension, SuspensionReason

if TYPE_CHECKING:  # pragma: no cover
    from .kernel import ExecutionKernel


def timer_satisfied(now) -> Callable[[Suspension, Mapping[str, Any]], bool]:
    """TIMER：到达指定时间点即满足。"""

    def _check(suspension: Suspension, ctx: Mapping[str, Any]) -> bool:
        if suspension.reason is not SuspensionReason.TIMER:
            return False
        return now() >= suspension.wait_condition.get("at", now())

    return _check


def approval_satisfied(approvals: Mapping[str, str]) -> Callable[[Suspension, Mapping[str, Any]], bool]:
    """HUMAN_APPROVAL：人工审批结果已到达即满足。"""

    def _check(suspension: Suspension, ctx: Mapping[str, Any]) -> bool:
        if suspension.reason is not SuspensionReason.HUMAN_APPROVAL:
            return False
        key = suspension.wait_condition.get("approver")
        return key is not None and key in approvals

    return _check


@dataclass
class WakeupController:
    kernel: "ExecutionKernel"
    last_woken: list[str] = field(default_factory=list)

    def run_once(
        self,
        is_satisfied: Callable[[Suspension, Mapping[str, Any]], bool],
        *,
        ctx: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """扫描所有 SUSPENDED，条件满足的唤醒为 PENDING。"""
        woken: list[str] = []
        for execution in self.kernel.repository.list_by_status(ExecutionStatus.SUSPENDED):
            suspension = execution.suspension
            if suspension is None:
                continue
            if is_satisfied(suspension, ctx or {}):
                self.kernel.wakeup(execution.execution_id)
                woken.append(execution.execution_id)
        self.last_woken = woken
        return woken
