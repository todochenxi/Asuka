"""AgentOS · execution_kernel（M15 阶段 3）

Execution Kernel = 可靠地把 Task 执行完。Runtime 交棒之后，生命周期唯一归这里管。

    kernel.py               ExecutionKernel：submit / claim / complete / fail / recover / cancel
    scheduler.py            Scheduler：决定哪个 Task 现在该跑（只认识 Task）
    recovery_controller.py  Lease 过期 → STALE → 新 Attempt
    wakeup_controller.py    Wait Condition 满足 → Runnable Task
    cancellation.py         取消三段式：Durable Intent / Fast Signal / Runtime Propagation
    idempotency.py          外部副作用去重 + EXTERNAL_UNKNOWN 回查
    ports.py                抽象接口（阶段 4：不绑定任何数据库）
    inmemory.py             端口的内存实现（阶段 3 用，阶段 5/6 换成 PG / Redis / Kafka）
    adapters/postgres.py    PostgreSQL 实现（阶段 5，不引 ORM）
    adapters/redis.py       Redis 实现（阶段 6：快路径，可丢，可从 PG 重建）
    adapters/kafka.py       Kafka 实现（阶段 7：至少一次投递 + 消费者去重）
    publisher.py            OutboxPublisher：Outbox → Kafka（先投递后标记）
    outbox_delivery.py      投递领地：多副本 publisher 的原子认领（M21）
    consumers.py            IdempotentConsumer：按 event_id 去重
    worker.py               Worker：Claim → 执行 → 回写（fencing / 心跳 / 协作取消）
"""
from __future__ import annotations

from .adapters.postgres import (  # noqa: F401
    InMemoryUnitOfWork,
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxDeliveryStore,
    PostgresOutboxStore,
    PostgresUnitOfWork,
)
from .adapters.kafka import KafkaEventPublisher, decode_event, encode_event  # noqa: F401
from .adapters.postgres import (  # noqa: F401
    PostgresIdempotencyStore,
    PostgresProcessedEventStore,
)
from .adapters.redis import (  # noqa: F401
    RedisCancelSignalStore,
    RedisIdempotencyStore,
    RedisLeaseIndex,
    rebuild_lease_index,
)
from .consumers import (  # noqa: F401
    IdempotentConsumer,
    InMemoryProcessedEventStore,
    ProcessedEventStore,
)
from .cancellation import CancellationService, CancellationToken  # noqa: F401
from .idempotency import IdempotencyGuard  # noqa: F401
from .inmemory import (  # noqa: F401
    InMemoryAttemptRepository,
    InMemoryCancelSignals,
    InMemoryExecutionRepository,
    InMemoryIdempotencyStore,
    InMemoryOutbox,
    ManualClock,
    RecordingEventPublisher,
    SystemClock,
)
from .kernel import ExecutionKernel, KernelConfig  # noqa: F401
from .outbox_delivery import (  # noqa: F401
    DeliveryRecord,
    InMemoryOutboxDeliveryStore,
    OutboxDeliveryStore,
)
from .ports import (  # noqa: F401
    AttemptRepository,
    CancelSignalStore,
    Clock,
    EventPublisher,
    ExecutionRepository,
    IdempotencyStore,
    LeaseIndex,
    OutboxStore,
    PartialPublishError,
    UnitOfWork,
)
from .recovery_controller import RecoveryController  # noqa: F401
from .scheduler import Scheduler, SchedulingPolicy, WorkerCapability  # noqa: F401
from .publisher import OutboxPublisher  # noqa: F401
from .wakeup_controller import (  # noqa: F401
    WakeupController,
    approval_satisfied,
    timer_satisfied,
)
from .worker import (  # noqa: F401
    Executor,
    ExecutorError,
    ExecutionContext,
    Worker,
    WorkerConfig,
    WorkerOutcome,
)

__version__ = "0.1.0"
