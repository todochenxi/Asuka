"""Scheduler：决定哪个 Task 现在应该执行。

E-12  Scheduler 只认识 Task（以及它的 Execution），不认识 Agent / Step / Goal
X-1   Runtime 交棒之后，调度权完全归 Kernel

调度考虑：Priority / Tenant Fairness / Aging / Resource Requirement /
Concurrency / Quota / Worker Capability。

Scheduler **不做** Agent 决策，也不执行任务 —— 它只把 Execution 交给 Worker（Claim）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from packages.agent_domain.execution import Attempt, Execution, ExecutionStatus, Lease, Task

if TYPE_CHECKING:  # pragma: no cover
    from .kernel import ExecutionKernel


@dataclass
class WorkerCapability:
    """Worker 自报的能力，用于 Resource Matching。

    PR-20：能力声明有**两个**维度，不是一个。

        executors   传输/形态：这个 worker 能接 native / http / mcp …
        task_types  语义：    在这些传输上，它能干 llm_call / tool_call …

    只有 `executors` 会发生什么（`_matches` 只过滤 executor_type 的年代）：
    一个只装了 tool 执行器的 worker 声明 `executors={native}`，于是
    `native:skill` 和 `native:human_approval` 的 Task 都会被派给它，
    然后以 `BAD_PAYLOAD`（PERMANENT，不重试）炸掉 ——
    **能力不匹配在运行期才暴露，而且是伪装成载荷错误的**。

    带上 `task_types` 之后，声明干不了的活儿**根本不会被派过来**：
    它留在 PENDING 里等一个真能干的 worker，而不是被派下来炸掉一次。

    `task_types` 为空 = "不限"，保持向后兼容（测试与单形态部署）。
    """

    executors: frozenset[str] = frozenset({"native"})
    labels: frozenset[str] = frozenset()
    free_slots: int = 1
    task_types: frozenset[str] = frozenset()


@dataclass
class SchedulingPolicy:
    max_concurrency_per_tenant: int | None = None
    tenant_fairness: bool = True
    aging_weight: float = 1.0            # 等待越久优先级越高，防止饿死
    priority_weight: float = 10.0
    default_quota_per_run: int | None = None


@dataclass
class Scheduler:
    kernel: "ExecutionKernel"
    policy: SchedulingPolicy = field(default_factory=SchedulingPolicy)
    now_provider: Callable[[], "object"] | None = None

    # ------------------------------------------------------------ 选择
    def _now(self):
        return self.kernel.clock.now()

    def candidates(self, limit: int | None = None) -> Sequence[Execution]:
        """只挑 PENDING（Runnable）的 Execution —— 休眠中的不会被选中。

        带**取消意图**的一律跳过：明知道马上要取消，就没必要再开工，
        否则白占一个 Attempt，还会触发 E-17（claim 拒绝）。
        """
        batch = limit or self.kernel.config.max_runnable_batch
        return [
            e
            for e in self.kernel.repository.list_by_status(ExecutionStatus.PENDING, limit=batch)
            if not e.cancellation_requested
        ]

    def _matches(self, task: Task, capability: WorkerCapability | None) -> bool:
        if capability is None:
            return True
        if task.executor_type.value not in capability.executors:
            return False
        # PR-20：第二个维度。空集 = 不限（向后兼容）。
        if capability.task_types and task.task_type.value not in capability.task_types:
            return False
        required = set(task.resource_requirement.labels)
        return required.issubset(capability.labels) if required else True

    def _score(self, task: Task, execution: Execution) -> float:
        """分数越高越先跑。

        priority 为主；重试次数越多略微让位（防止反复失败的任务霸占队列）。
        Aging 需要 Execution 的创建时间，阶段 5 落 PG 后补上 `created_at` 索引。
        """
        return (
            task.priority * self.policy.priority_weight
            - execution.current_attempt_no * self.policy.aging_weight
        )

    def select(
        self,
        limit: int = 10,
        *,
        capability: WorkerCapability | None = None,
    ) -> Sequence[Execution]:
        batch = list(self.candidates())
        if not batch:
            return []
        # E-26：Task 落库之后逐个 `task_of()` 就是 N 次往返
        # （`_running_for_tenant` 还要再来一轮）。一次批量取回。
        tasks = self.kernel.tasks_for([e.execution_id for e in batch])
        running_tasks: dict[str, Task] | None = None

        scored: list[tuple[float, Execution]] = []
        for execution in batch:
            task = tasks[execution.execution_id]
            if not self._matches(task, capability):
                continue
            if self.policy.max_concurrency_per_tenant is not None and task.tenant_id:
                if running_tasks is None:
                    running_tasks = self._running_tasks()
                running = sum(
                    1 for t in running_tasks.values() if t.tenant_id == task.tenant_id
                )
                if running >= self.policy.max_concurrency_per_tenant:
                    continue
            scored.append((self._score(task, execution), execution))

        scored.sort(key=lambda item: (-item[0], item[1].execution_id))
        ordered = [e for _, e in scored]

        if self.policy.tenant_fairness:
            ordered = self._round_robin_by_tenant(ordered, tasks)
        return ordered[:limit]

    def _running_tasks(self) -> dict[str, Task]:
        running = self.kernel.repository.list_by_status(ExecutionStatus.RUNNING, limit=10_000)
        return dict(self.kernel.tasks_for([e.execution_id for e in running]))

    def _round_robin_by_tenant(
        self,
        executions: Iterable[Execution],
        tasks: Mapping[str, Task] | None = None,
    ) -> list[Execution]:
        known = tasks if tasks is not None else self.kernel.tasks_for(
            [e.execution_id for e in executions]
        )
        buckets: dict[str, list[Execution]] = {}
        for e in executions:
            tenant = known[e.execution_id].tenant_id or "_default"
            buckets.setdefault(tenant, []).append(e)
        out: list[Execution] = []
        while any(buckets.values()):
            for tenant in list(buckets):
                if buckets[tenant]:
                    out.append(buckets[tenant].pop(0))
        return out

    # ------------------------------------------------------------ 派发
    def dispatch(
        self,
        *,
        worker_id: str,
        capability: WorkerCapability | None = None,
        limit: int = 1,
        ttl: timedelta | None = None,
    ) -> list[tuple[Execution, Attempt, Lease]]:
        """选中 → Claim（Atomic：Lease + fencing_token + 新 Attempt）。"""
        slots = capability.free_slots if capability else limit
        take = min(limit, slots)
        dispatched = []
        for execution in self.select(limit=take, capability=capability):
            attempt, lease = self.kernel.claim(
                execution.execution_id, worker_id=worker_id, ttl=ttl
            )
            dispatched.append((execution, attempt, lease))
        return dispatched
