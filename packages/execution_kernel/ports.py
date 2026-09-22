"""Kernel 的端口（抽象接口）。

阶段 4 的原则：**接口先行，不绑定任何数据库**。
PG / Redis / Kafka 只是这些接口的 Adapter，Domain 与 Kernel 都不 import 它们。

    ExecutionRepository   当前持久状态（PG）
    TaskRepository        Task 的交棒输入（PG，E-26 的前提）
    AttemptRepository     Attempt 历史（PG，E-4 的前提）
    OutboxStore           事务性发件箱（PG，与状态写入同事务）
    LeaseIndex            Lease 快速面（Redis，可丢、可从 PG 重建）
    CancelSignalStore     低延迟取消信号（Redis）—— 只是通知，不是事实来源
    IdempotencyStore      幂等结果（Redis / PG）
    EventPublisher        Outbox → Kafka（至少一次，消费者去重）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

from packages.agent_domain.events.event import Event
from packages.agent_domain.execution import Attempt, Execution, ExecutionStatus, Task


class Clock(Protocol):
    def now(self) -> datetime: ...


class ExecutionRepository(Protocol):
    """当前持久状态。E-13：save 必须做 Optimistic Lock。"""

    def add(self, execution: Execution) -> None: ...

    def get(self, execution_id: str) -> Execution | None: ...

    def get_by_task(self, task_id: str) -> Execution | None:
        """E-19：Task : Execution = 1 : 1 的唯一索引。"""

    def save(self, execution: Execution, expected_version: int | None = None) -> None: ...

    def list_by_status(self, status: ExecutionStatus, limit: int = 100) -> Sequence[Execution]: ...

    def list_with_expired_lease(self, now: datetime, limit: int = 100) -> Sequence[Execution]: ...


class TaskRepository(Protocol):
    """Task 的持久面（E-26）。

    ------------------------------------------------------------------
    Task 与 Execution 是**作者不同**的两件事，所以不能塞一张表（B-7）

        Task       Runtime 在交棒那一刻造出来的**输入**（X-1 / X-2）
        Execution  Kernel 给它开的**生命周期**

    Task 写一次之后不再改（E-28）；Execution 每次状态迁移都改（E-13）。
    把一个"不可变的输入"和一个"乐观锁管着的状态"放同一行，
    等于让"谁有资格写哪一列"这个问题消失。

    ------------------------------------------------------------------
    没有这个端口会发生什么（空洞 215）

    `001_kernel.sql` 里 `tasks` 表从 M15 就建好了：13 个列、2 个索引、
    注释还写着"Scheduler 只认识它（E-12）"。但**没有一行代码写它**。
    Task 只活在 `ExecutionKernel._tasks` 这个进程内字典里，
    于是进程一重启，Scheduler 与 Worker 就再也拿不到它。

    而它们拿不到的不是"快一点慢一点"的信息，是四个**正确性**输入：

        priority              决定谁先跑
        tenant_id             决定配额 —— 多租户隔离边界，不是性能旋钮
        resource_requirement  决定能不能派给这个 worker
        payload               决定到底要干什么

    编造一个默认 Task 把 KeyError 消掉，是这里最容易犯的错：
    需要 GPU 的活会被派给只有 CPU 的 worker，
    然后以 `EXECUTOR_NOT_FOUND`（PERMANENT，不重试）终态 ——
    **一个调度错误伪装成一个载荷错误**（PR-20 那次修过的病，在持久层复发），
    而租户配额静默失效。两者都不报错。
    按 A-12 判：丢了之后是**变错**，不是变慢。
    """

    def add(self, task: Task) -> None:
        """只在 `kernel.submit()` 里被调用一次（E-28）。

        同一 `task_id` 重复插入**不是冲突，是重演**：
        `task_id` 是 Task 的身份，同 id 意味着同一件活；
        事务没生效而调用方重试时，这一行就是那次重演的落点。
        所以实现方用 `ON CONFLICT (task_id) DO NOTHING`，不要报错。
        """

    def get(self, task_id: str) -> Task | None: ...

    def get_many(self, task_ids: Sequence[str]) -> Mapping[str, Task]:
        """Scheduler 的热路径：一次取回一批。

        为什么要批量：`Scheduler.select()` 对每个候选都要看
        priority / tenant / resource，而候选一批最多 100 个。
        逐个 `get()` 就是 100 次往返，`_running_for_tenant` 还要再来一轮 ——
        这不是"优化"，是让"Task 落库"这件事在生产上真的能用
        （001 的注释自己写着"Scheduler 的热路径"）。
        """


class AttemptRepository(Protocol):
    """Attempt 历史（E-4：Retry = 新 Attempt，所以历史必须留得下来）。

    没有这个端口，Kernel 进程重启后就"忘记"自己尝试过几次 ——
    Retry 预算、审计、Replay 全都无从谈起。
    """

    def save(self, attempt: Attempt) -> None:
        """按 (execution_id, attempt_no) 幂等 upsert。"""

    def get(self, execution_id: str, attempt_no: int) -> Attempt | None: ...

    def list_by_execution(self, execution_id: str) -> Sequence[Attempt]: ...


class OutboxStore(Protocol):
    """X-3：状态变更与事件写入必须同一事务（这里用 outbox 表达）。"""

    def append(self, events: Iterable[Event]) -> None: ...

    def pending(
        self, limit: int = 100, exclude: Sequence[str] = ()
    ) -> Sequence[Event]:
        """待投事件，按 occurred_at 排序（PR-9：投递顺序不由认领竞争决定）。

        `exclude` 由投递进程用来把死信挤出候选集（PR-5）——
        死信留在候选集里会一直吃掉 limit 的位置。
        """

    def mark_published(self, event_ids: Sequence[str]) -> None: ...


class LeaseIndex(Protocol):
    """Lease 的**快速面**（Redis ZSET，按 expires_at 排序）。

    它只是 Recovery Controller 的加速器：避免每轮全表扫 PG。
    **丢失不影响正确性** —— 可以从 PG 的 `executions` 表重建
    （见 `adapters/redis.rebuild_lease_index`）。

    因此这里没有 `acquire()`：Lease 的归属与 fencing 永远以 PG 为准，
    索引只回答"谁的 Lease 快到期了"。
    """

    def track(self, lease: Any) -> None:
        """登记 / 续期（按 execution_id 幂等覆盖）。"""

    def forget(self, execution_id: str) -> None: ...

    def due(self, now: datetime, limit: int = 100) -> Sequence[str]:
        """返回 expires_at <= now 的 execution_id。"""

    def clear(self) -> None:
        """整索引清空（重建前用）。"""


class CancelSignalStore(Protocol):
    """快速取消信号（Redis）。

    只是低延迟通知，不是取消的事实来源 —— 事实来源永远是 PG 里的
    `Execution.cancellation_requested`。
    """

    def set(self, execution_id: str, ttl_seconds: int = 3600) -> None: ...

    def get(self, execution_id: str) -> bool: ...

    def clear(self, execution_id: str) -> None: ...


class IdempotencyStore(Protocol):
    """外部副作用去重的**缓存**：key → 首次结果。

    ⚠️ `get()` 返回 None 的含义是 **UNKNOWN**，不是 **"没执行过"**。
    缓存可能过期/被驱逐/Redis 重启丢失 —— 此时必须回查下游
    （见 `idempotency.IdempotencyGuard.resolve_unknown`），**绝不能盲重试**。

    真正的幂等保证在下游（携带 `idempotency_key` 调用），这里只是省一次往返。
    """

    def get(self, key: str) -> Mapping[str, Any] | None: ...

    def put(self, key: str, value: Mapping[str, Any]) -> None: ...


class EventPublisher(Protocol):
    """Outbox → Kafka。至少一次投递，消费者按 event_id 去重。

    失败有两种，语义完全不同，实现方必须区分：

        1. broker 收下了批次，但**指名道姓**拒了其中几条
           （topic 不存在 / 记录超限 / schema 不合规）
           → 抛 `PartialPublishError(failures={event_id: 原因})`
           → 这是**毒消息**：broker 是好的，坏的是数据

        2. broker 根本没收到（不可达 / 鉴权失败 / flush 超时）
           → 抛别的任何异常
           → 这是**系统性故障**：publisher 无从指认是哪一条的锅
    """

    def publish(self, events: Sequence[Event]) -> int: ...


class PartialPublishError(Exception):
    """broker 拒了这批里的**特定几条**（PR-5 的判死依据）。

    `failures` 是 `event_id -> 原因`。能指认 ⇒ 是毒消息，可以就地判死，
    不必惊动进程；指认不了就必须当系统性故障抛上去（PR-8 要靠它退出）。

    这不是为了好看：混淆这两种失败会让 publisher 在"broker 挂了"的时候
    把**整批好事件**逐条判死，或者在"只有一条坏事件"的时候把进程拖去重启。
    """

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures: dict[str, str] = dict(failures)
        super().__init__(
            f"{len(self.failures)} record(s) rejected: "
            + ", ".join(sorted(self.failures))
        )


class UnitOfWork(Protocol):
    """把"状态写入 + Outbox 写入"收敛成一个原子边界（**X-3**）。

    ------------------------------------------------------------------
    它从 M15 起就是**死代码**

    基线 §"Kernel 的持久化端口"里明写着 `UnitOfWork 事务边界`，
    Worker 五件事的第 5 件也写着"outbox 同一事务内写事件"。
    但代码里它**只有定义、零处使用** —— 于是整个仓库没有任何一处
    `commit()`，`pg_connection()` 只能靠 `autocommit=True` 让写不丢。

    后果不是"事务没启用"，是 **X-3 从未成立**：
    `UPDATE executions` 与 `INSERT outbox_events` 之间进程一崩，
    状态变了而事件没写出去（或反之），下游永远看不到这件事发生过。
    而它**不报错** —— 这正是 Outbox 模式存在的唯一理由被静默取消。

    这是 `CompensationSpec.to_dict`（PR-27）、`default_model_id`（G-8）
    之后的又一次同一类病：**冻结在文档里，但没有任何一条测试要求它成立。**

    ------------------------------------------------------------------
    边界由**进程层**驱动，不由 Kernel 驱动

    Kernel 每次状态变更都自己提交的话，"一个 Run 走一步"就会被切成
    好几个事务，而它们之间失败时留下的中间态没人负责清理。
    所以事务边界放在 `apps/`：

        一个 HTTP 请求 = 一个事务（PR-31）
        一个 tick      = 一个事务（PR-30，与 PR-1 的"tick 原子"对齐）

    Kernel 只是这条边界里最主要的那一个写者。
    """

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool: ...
