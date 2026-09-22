"""PostgreSQL Adapter（阶段 5）。

只做一件事：把 `ports.py` 的接口用 SQL 实现。

规则：
- 不引 ORM 魔法：显式 SQL + 显式参数，行 → 领域对象的映射写清楚。
- 参数占位符用 DB-API 的 `%s`（psycopg2 / psycopg3）。
- E-13：所有 UPDATE 带 `WHERE version = ?`，rowcount = 0 即并发冲突。
- 表结构见 `infrastructure/postgres/001_kernel.sql`。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from packages.agent_domain.errors import ConcurrentStateError
from packages.agent_domain.events.event import Event
from packages.execution_kernel.outbox_delivery import DeliveryRecord
from packages.agent_domain.execution import (
    Attempt,
    AttemptStatus,
    ErrorInfo,
    Execution,
    ExecutionStatus,
    FailureClass,
    Lease,
    Suspension,
    SuspensionReason,
    Task,
)
from packages.agent_domain.execution.task import ExecutorType, ResourceReq, TaskType
from packages.agent_domain.execution.retry import RetryPolicy

INSERT_EXECUTION = """
INSERT INTO executions (
    execution_id, task_id, idempotency_key, execution_mode, status,
    parent_id, current_attempt_no, version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

SELECT_EXECUTION = """
SELECT execution_id, task_id, idempotency_key, execution_mode, status,
       parent_id, current_attempt_no,
       lease_worker_id, lease_fencing_token, lease_acquired_at,
       lease_expires_at, lease_heartbeat_at,
       suspension_reason, suspension_wait_cond, suspended_at,
       cancellation_requested,
       -- M48 / 空洞 228：归因（017 迁移）。与意图位同在
       -- （`executions_cancel_attributed`）—— 少了这两列，
       -- "谁叫停、为什么"在 PG 里照样查不到。
       cancellation_reason, cancellation_by, version
  FROM executions
"""

UPDATE_EXECUTION = """
UPDATE executions
   SET status = %s,
       current_attempt_no = %s,
       lease_worker_id = %s,
       lease_fencing_token = %s,
       lease_acquired_at = %s,
       lease_expires_at = %s,
       lease_heartbeat_at = %s,
       suspension_reason = %s,
       suspension_wait_cond = %s,
       suspended_at = %s,
       cancellation_requested = %s,
       cancellation_reason = %s,
       cancellation_by = %s,
       version = %s,
       updated_at = now()
 WHERE execution_id = %s
   AND version = %s
"""

SELECT_EXPIRED_LEASES = """
SELECT execution_id
  FROM executions
 WHERE status = 'RUNNING'
   AND lease_expires_at IS NOT NULL
   AND lease_expires_at < %s
 ORDER BY lease_expires_at
 LIMIT %s
"""

INSERT_ATTEMPT = """
INSERT INTO attempts (
    attempt_id, execution_id, attempt_no, status,
    started_at, finished_at,
    error_code, error_message, failure_class,
    result, checkpoint_id, version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (execution_id, attempt_no) DO UPDATE
   SET status = EXCLUDED.status,
       finished_at = EXCLUDED.finished_at,
       error_code = EXCLUDED.error_code,
       error_message = EXCLUDED.error_message,
       failure_class = EXCLUDED.failure_class,
       result = EXCLUDED.result,
       checkpoint_id = EXCLUDED.checkpoint_id,
       version = attempts.version + 1
"""

INSERT_OUTBOX = """
INSERT INTO outbox_events (
    event_id, aggregate_type, aggregate_id, event_type,
    payload, occurred_at, aggregate_version
) VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

SELECT_PENDING_OUTBOX = """
SELECT event_id, aggregate_type, aggregate_id, event_type,
       payload, occurred_at, aggregate_version
  FROM outbox_events
 WHERE published_at IS NULL"""

MARK_PUBLISHED = "UPDATE outbox_events SET published_at = now() WHERE event_id = ANY(%s)"

# --- Outbox 投递领地（M21 / 006_outbox_delivery.sql） ------------------------
#
# PR-3：认领的原子性由**单条 SQL** 提供（UPDATE ... WHERE / INSERT ON CONFLICT）。
# 进程里"先 SELECT 看看有没有人占着，再决定要不要写"永远有窗口。

CLAIM_DELIVERY = """
UPDATE outbox_delivery
   SET claimed_by = %s, claimed_until = %s, updated_at = now()
 WHERE event_id = %s
   AND dead_at IS NULL
   AND (claimed_until IS NULL OR claimed_until <= %s)
"""

INSERT_DELIVERY = """
INSERT INTO outbox_delivery (event_id, claimed_by, claimed_until, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (event_id) DO NOTHING
"""

DELETE_DELIVERY = "DELETE FROM outbox_delivery WHERE event_id = ANY(%s)"

# PR-5 / PR-6：attempts+1，够了就判死并写 dead_at。
# SET 里的 `attempts` 是**旧值**（SQL 语义），所以 `attempts + 1 >= %s` 判的是新次数。
FAIL_DELIVERY = """
UPDATE outbox_delivery
   SET attempts = attempts + 1,
       last_error = %s,
       dead_at = CASE WHEN attempts + 1 >= %s THEN %s ELSE NULL END,
       claimed_by = NULL,
       claimed_until = NULL,
       updated_at = now()
 WHERE event_id = %s AND dead_at IS NULL
"""

RELEASE_DELIVERY_IDS = """
UPDATE outbox_delivery
   SET claimed_by = NULL, claimed_until = NULL, updated_at = now()
 WHERE claimed_by = %s AND dead_at IS NULL AND event_id = ANY(%s)
"""

RELEASE_DELIVERY_OWNER = """
UPDATE outbox_delivery
   SET claimed_by = NULL, claimed_until = NULL, updated_at = now()
 WHERE claimed_by = %s AND dead_at IS NULL
"""

SELECT_DELIVERY = """
SELECT event_id, attempts, claimed_by, claimed_until, last_error, dead_at
  FROM outbox_delivery WHERE event_id = %s"""

SELECT_DEAD_IDS = """
SELECT event_id FROM outbox_delivery
 WHERE dead_at IS NOT NULL ORDER BY dead_at LIMIT %s"""

SELECT_DEAD = """
SELECT event_id, attempts, claimed_by, claimed_until, last_error, dead_at
  FROM outbox_delivery
 WHERE dead_at IS NOT NULL ORDER BY dead_at LIMIT %s"""

SELECT_HELD = """
SELECT event_id FROM outbox_delivery
 WHERE claimed_by = %s AND claimed_until > %s AND dead_at IS NULL"""

# PR-10：唯一的复活通道，且只能由人发起
REOPEN_DELIVERY = """
UPDATE outbox_delivery
   SET attempts = 0, last_error = NULL, dead_at = NULL, updated_at = now()
 WHERE event_id = %s AND dead_at IS NOT NULL"""


def _exclude_clause(exclude: Sequence[str]) -> tuple[str, list[str]]:
    """PR-5：把死信挤出候选集。空列表不加条件 —— 热路径不付这份代价。"""
    if not exclude:
        return "", []
    placeholders = ", ".join(["%s"] * len(exclude))
    return f" AND event_id NOT IN ({placeholders})", list(exclude)

# 消费者去重（阶段 7）：ON CONFLICT DO NOTHING + rowcount 判定"是不是首次登记"
INSERT_PROCESSED = """
INSERT INTO processed_events (event_id, event_type, processed_at)
VALUES (%s, %s, %s)
ON CONFLICT (event_id) DO NOTHING
"""

SELECT_PROCESSED = "SELECT 1 FROM processed_events WHERE event_id = %s"


# 幂等键（M24 / 007_idempotency.sql）
#
# A-3：Run 创建的幂等键必须落 PG —— "创建 Run"没有下游可以回查，
# 键丢了就会开出第二个 Run。ON CONFLICT DO NOTHING + rowcount 判定首次写入，
# 与消费者去重（INSERT_PROCESSED）是同一个形状。
INSERT_IDEMPOTENCY = """
INSERT INTO idempotency_keys (key, value, durable)
VALUES (%s, %s::jsonb, %s)
ON CONFLICT (key) DO NOTHING
"""

SELECT_IDEMPOTENCY = "SELECT value FROM idempotency_keys WHERE key = %s"


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _load_mapping(raw: Any) -> dict[str, Any]:
    """JSONB 读回来可能是 dict（驱动自己转了）也可能是 str（没转）。

    两种都得认 —— 否则这段代码只在"恰好配了 loader 的那种连法"下正确。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


INSERT_TASK = """
INSERT INTO tasks (
    task_id, run_id, step_id, task_type, executor_type, payload,
    priority, tenant_id, resource_requirement, retry_policy,
    timeout_seconds, version
) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
ON CONFLICT (task_id) DO NOTHING
"""
# ↑ E-28：同一 task_id 再插一次是**重演**，不是冲突。
#   `submit()` 先写 Task 后写 Execution，两者在同一事务里；
#   事务没生效而调用方重试时，Task 行可能已经在了 —— 此时应当继续，
#   让 Execution 补上，而不是抛一个谁也没法处理的唯一键冲突。
#   反之 `DO UPDATE` 会让一个改过的 Task 覆盖掉原件，那才是真错。

SELECT_TASK = """
SELECT task_id, run_id, step_id, task_type, executor_type, payload,
       priority, tenant_id, resource_requirement, retry_policy,
       timeout_seconds, version
  FROM tasks
"""

def _row_to_task(row: Mapping[str, Any]) -> Task:
    resource = _load_mapping(row.get("resource_requirement"))
    labels = resource.get("labels") or ()
    return Task(
        task_id=row["task_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],                       # E-11
        task_type=TaskType(row["task_type"]),
        executor_type=ExecutorType(row["executor_type"]),
        payload=_load_mapping(row.get("payload")),
        priority=int(row["priority"] or 0),
        tenant_id=row.get("tenant_id"),
        resource_requirement=ResourceReq(
            cpu_millis=int(resource.get("cpu_millis") or 100),
            memory_mb=int(resource.get("memory_mb") or 256),
            gpu=int(resource.get("gpu") or 0),
            labels=tuple(labels),
        ),
        retry_policy=RetryPolicy(**_load_mapping(row.get("retry_policy"))),
        timeout=timedelta(seconds=int(row["timeout_seconds"] or 60)),
        version=int(row["version"] or 1),
    )


def _row_to_execution(row: Mapping[str, Any]) -> Execution:
    lease = None
    if row.get("lease_worker_id") and row.get("lease_expires_at"):
        lease = Lease(
            execution_id=row["execution_id"],
            attempt_no=row["current_attempt_no"] or 1,
            worker_id=row["lease_worker_id"],
            fencing_token=int(row["lease_fencing_token"] or 1),
            acquired_at=row["lease_acquired_at"],
            expires_at=row["lease_expires_at"],
            heartbeat_at=row["lease_heartbeat_at"] or row["lease_acquired_at"],
        )

    suspension = None
    if row.get("suspension_reason"):
        suspension = Suspension(
            reason=SuspensionReason(row["suspension_reason"]),
            wait_condition=row.get("suspension_wait_cond") or {},
            suspended_at=row.get("suspended_at") or datetime.now(),
        )

    execution = Execution(
        execution_id=row["execution_id"],
        task_id=row["task_id"],
        idempotency_key=row["idempotency_key"],
        execution_mode=row["execution_mode"],
        status=ExecutionStatus(row["status"]),
        parent_id=row.get("parent_id"),
        current_attempt_no=row["current_attempt_no"],
        lease=lease,
        suspension=suspension,
        cancellation_requested=bool(row["cancellation_requested"]),
        # M48 / 空洞 228：017 之前没有这两列，老库读回来就是空串
        # —— 那正是"查不到归因"的老样子，不是错误。
        cancellation_reason=row.get("cancellation_reason") or "",
        cancellation_by=row.get("cancellation_by") or "",
        version=row["version"],
    )
    # E-25：读到什么版本，就该拿什么版本去比。
    object.__setattr__(execution, "_store_version", row["version"])
    object.__setattr__(execution, "_previous_version", row["version"])
    return execution


class PostgresExecutionRepository:
    """ports.ExecutionRepository 的 PG 实现。

    `conn` 是任意 DB-API 连接（psycopg2/3），本模块不 import 任何驱动。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 读
    def get(self, execution_id: str) -> Execution | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_EXECUTION + " WHERE execution_id = %s", (execution_id,))
        row = cur.fetchone()
        return _row_to_execution(row) if row else None

    def get_by_task(self, task_id: str) -> Execution | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_EXECUTION + " WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
        return _row_to_execution(row) if row else None

    def list_by_status(self, status: ExecutionStatus, limit: int = 100) -> Sequence[Execution]:
        cur = self.conn.cursor()
        cur.execute(
            SELECT_EXECUTION + " WHERE status = %s ORDER BY version LIMIT %s",
            (status.value, limit),
        )
        return [_row_to_execution(r) for r in cur.fetchall()]

    def list_with_expired_lease(self, now: datetime, limit: int = 100) -> Sequence[Execution]:
        """只扫 RUNNING 且 Lease 已过期的（部分索引 idx_exec_lease_expiry）。"""
        cur = self.conn.cursor()
        cur.execute(SELECT_EXPIRED_LEASES, (now, limit))
        ids = [r["execution_id"] for r in cur.fetchall()]
        out = []
        for execution_id in ids:
            execution = self.get(execution_id)
            if execution is not None:
                out.append(execution)
        return out

    # ------------------------------------------------------------------ 写
    def add(self, execution: Execution) -> None:
        cur = self.conn.cursor()
        cur.execute(
            INSERT_EXECUTION,
            (
                execution.execution_id,
                execution.task_id,
                execution.idempotency_key,
                execution.execution_mode,
                execution.status.value,
                execution.parent_id,
                execution.current_attempt_no,
                execution.version,
            ),
        )
        object.__setattr__(execution, "_store_version", execution.version)
        object.__setattr__(execution, "_previous_version", execution.version)

    def save(self, execution: Execution, expected_version: int | None = None) -> None:
        """E-13：乐观锁。rowcount = 0 说明有人抢先写了。

        E-25：默认比的是 `store_version`（**上一次存储边界**），
        不是 `previous_version`（上一次内存自增前）。

        一次 Kernel 操作可以包含多次状态跃迁（`resume` 是 SUSPENDED→PENDING→RUNNING，
        `recover` 同理），而它只落一次库 —— 拿内存自增次数当落库次数，
        乐观锁会在**没有并发**的情况下自己报错。
        """
        expected = (
            expected_version if expected_version is not None else execution.store_version
        )
        lease = execution.lease
        suspension = execution.suspension
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_EXECUTION,
            (
                execution.status.value,
                execution.current_attempt_no,
                lease.worker_id if lease else None,
                lease.fencing_token if lease else None,
                lease.acquired_at if lease else None,
                lease.expires_at if lease else None,
                lease.heartbeat_at if lease else None,
                suspension.reason.value if suspension else None,
                _json(suspension.wait_condition) if suspension else None,
                suspension.suspended_at if suspension else None,
                execution.cancellation_requested,
                # M48 / 空洞 228：归因随意图一起落库（B-8 / A-8）
                execution.cancellation_reason,
                execution.cancellation_by,
                execution.version,
                execution.execution_id,
                expected,
            ),
        )
        if cur.rowcount == 0:
            raise ConcurrentStateError(
                f"E-13: concurrent update on execution {execution.execution_id} "
                f"(expected version {expected})"
            )
        # 写成功了：这条对象眼里，"存储里的版本"现在就是当前版本
        object.__setattr__(execution, "_store_version", execution.version)
        object.__setattr__(execution, "_previous_version", execution.version)


class PostgresTaskRepository:
    """ports.TaskRepository 的 PG 实现（E-26）。

    Task 在 `submit()` 里写一次，之后只读不写 —— 所以这里**没有 UPDATE**。
    那不是还没实现，是 E-28 的要求：Task 是交棒那一刻的输入，
    交棒之后它就不再属于 Runtime 了（X-1）。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def add(self, task: Task) -> None:
        cur = self.conn.cursor()
        cur.execute(
            INSERT_TASK,
            (
                task.task_id,
                task.run_id,
                task.step_id,
                task.task_type.value,
                task.executor_type.value,
                _json(dict(task.payload)),
                task.priority,
                task.tenant_id,
                _json(
                    {
                        "cpu_millis": task.resource_requirement.cpu_millis,
                        "memory_mb": task.resource_requirement.memory_mb,
                        "gpu": task.resource_requirement.gpu,
                        "labels": list(task.resource_requirement.labels),
                    }
                ),
                _json(
                    {
                        "max_attempts": task.retry_policy.max_attempts,
                        "backoff_base_seconds": task.retry_policy.backoff_base_seconds,
                        "backoff_max_seconds": task.retry_policy.backoff_max_seconds,
                        "retry_budget": task.retry_policy.retry_budget,
                    }
                ),
                int(task.timeout.total_seconds()),
                task.version,
            ),
        )

    def get(self, task_id: str) -> Task | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_TASK + " WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
        return _row_to_task(row) if row else None

    def get_many(self, task_ids: Sequence[str]) -> Mapping[str, Task]:
        ids = [t for t in task_ids if t]
        if not ids:
            return {}
        cur = self.conn.cursor()
        cur.execute(SELECT_TASK + " WHERE task_id = ANY(%s)", (list(ids),))
        return {row["task_id"]: _row_to_task(row) for row in cur.fetchall()}


class PostgresAttemptRepository:
    """Attempt 落库（E-6：UNIQUE(execution_id, attempt_no) 由数据库兜底）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, attempt: Attempt) -> None:
        error = attempt.error
        cur = self.conn.cursor()
        cur.execute(
            INSERT_ATTEMPT,
            (
                attempt.attempt_id,
                attempt.execution_id,
                attempt.attempt_no,
                attempt.status.value,
                attempt.started_at,
                attempt.finished_at,
                error.code if error else None,
                error.message if error else None,
                error.failure_class.value if error else None,
                _json(attempt.result) if attempt.result else None,
                attempt.checkpoint_id,
                attempt.version,
            ),
        )

    ATTEMPT_COLUMNS = """
            SELECT attempt_id, execution_id, attempt_no, status,
                   started_at, finished_at,
                   error_code, error_message, failure_class,
                   result, checkpoint_id, version
              FROM attempts
    """

    def _row_to_attempt(self, row: Mapping[str, Any]) -> Attempt:
        error = None
        if row.get("error_code"):
            error = ErrorInfo(
                code=row["error_code"],
                message=row["error_message"] or "",
                failure_class=FailureClass(row["failure_class"]),
            )
        return Attempt(
            attempt_id=row["attempt_id"],
            execution_id=row["execution_id"],
            attempt_no=row["attempt_no"],
            status=AttemptStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=error,
            result=row["result"] if isinstance(row["result"], dict) else None,
            checkpoint_id=row["checkpoint_id"],
            version=row["version"],
        )

    def get(self, execution_id: str, attempt_no: int) -> Attempt | None:
        cur = self.conn.cursor()
        cur.execute(
            self.ATTEMPT_COLUMNS + " WHERE execution_id = %s AND attempt_no = %s",
            (execution_id, attempt_no),
        )
        row = cur.fetchone()
        return self._row_to_attempt(row) if row is not None else None

    def list_by_execution(self, execution_id: str) -> Sequence[Attempt]:
        cur = self.conn.cursor()
        cur.execute(
            self.ATTEMPT_COLUMNS + " WHERE execution_id = %s ORDER BY attempt_no",
            (execution_id,),
        )
        return [self._row_to_attempt(r) for r in cur.fetchall()]


class PostgresOutboxStore:
    """X-3：事件与状态写入同一事务（这里由调用方控制事务边界）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def append(self, events: Iterable[Event]) -> None:
        cur = self.conn.cursor()
        for e in events:
            cur.execute(
                INSERT_OUTBOX,
                (
                    e.event_id,
                    e.aggregate_type,
                    e.aggregate_id,
                    e.event_type,
                    _json(e.payload),
                    e.occurred_at,
                    e.aggregate_version,
                ),
            )

    def pending(
        self, limit: int = 100, exclude: Sequence[str] = ()
    ) -> Sequence[Event]:
        clause, params = _exclude_clause(exclude)
        cur = self.conn.cursor()
        # PR-9：ORDER BY occurred_at —— 投递顺序不由认领竞争决定
        cur.execute(
            SELECT_PENDING_OUTBOX + clause + " ORDER BY occurred_at LIMIT %s",
            (*params, limit),
        )
        return [
            Event(
                event_id=r["event_id"],
                aggregate_type=r["aggregate_type"],
                aggregate_id=r["aggregate_id"],
                event_type=r["event_type"],
                payload=r["payload"] if isinstance(r["payload"], dict) else {},
                occurred_at=r["occurred_at"],
                aggregate_version=r["aggregate_version"],
            )
            for r in cur.fetchall()
        ]

    def mark_published(self, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        cur = self.conn.cursor()
        cur.execute(MARK_PUBLISHED, (list(event_ids),))


class PostgresOutboxDeliveryStore:
    """投递领地（M21 / 006_outbox_delivery.sql）。

    PR-3 的判定权在这里，不在进程内存里：
    两条 SQL（UPDATE 抢租约 / INSERT 占位），每条都是原子的，
    谁抢到由存储层说了算。这与 A-11（审批）和 S-4（补偿认领）是同一个形状。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def claim(self, event_id: str, owner: str, ttl: timedelta, now: datetime) -> bool:
        until = now + ttl
        cur = self.conn.cursor()
        cur.execute(CLAIM_DELIVERY, (owner, until, event_id, now))
        if cur.rowcount == 1:
            return True
        # 没有行（首次投递）→ 占位；已存在但拿不到 → ON CONFLICT DO NOTHING，拿不到
        cur.execute(INSERT_DELIVERY, (event_id, owner, until))
        return cur.rowcount == 1

    def mark_sent(self, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        cur = self.conn.cursor()
        cur.execute(DELETE_DELIVERY, (list(event_ids),))

    def mark_failed(
        self, event_id: str, error: str, *, max_attempts: int, now: datetime
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(FAIL_DELIVERY, (error, max_attempts, now, event_id))
        if cur.rowcount != 1:
            return False
        cur.execute(SELECT_DELIVERY, (event_id,))
        row = cur.fetchone()
        return row is not None and row["dead_at"] is not None

    def release(self, owner: str, event_ids: Sequence[str] | None = None) -> int:
        cur = self.conn.cursor()
        if event_ids is None:
            cur.execute(RELEASE_DELIVERY_OWNER, (owner,))
        else:
            if not event_ids:
                return 0
            cur.execute(RELEASE_DELIVERY_IDS, (owner, list(event_ids)))
        return cur.rowcount or 0

    def dead_ids(self, limit: int = 1000) -> Sequence[str]:
        cur = self.conn.cursor()
        cur.execute(SELECT_DEAD_IDS, (limit,))
        return [r["event_id"] for r in cur.fetchall()]

    def dead(self, limit: int = 100) -> Sequence[DeliveryRecord]:
        cur = self.conn.cursor()
        cur.execute(SELECT_DEAD, (limit,))
        return [_row_to_delivery(r) for r in cur.fetchall()]

    def held(self, owner: str, now: datetime) -> Sequence[str]:
        cur = self.conn.cursor()
        cur.execute(SELECT_HELD, (owner, now))
        return [r["event_id"] for r in cur.fetchall()]

    def reopen(self, event_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(REOPEN_DELIVERY, (event_id,))
        return cur.rowcount == 1

    def get(self, event_id: str) -> DeliveryRecord | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_DELIVERY, (event_id,))
        row = cur.fetchone()
        return _row_to_delivery(row) if row is not None else None


def _row_to_delivery(row: Mapping[str, Any]) -> DeliveryRecord:
    return DeliveryRecord(
        event_id=row["event_id"],
        attempts=row["attempts"],
        claimed_by=row["claimed_by"],
        claimed_until=row["claimed_until"],
        last_error=row["last_error"],
        dead_at=row["dead_at"],
    )


class PostgresProcessedEventStore:
    """消费者去重登记表（阶段 7）。

    用 PG 而不是 Redis：去重记录本身必须是**持久**的 ——
    Redis 丢了会导致整批历史事件被重新处理一遍。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def seen(self, event_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(SELECT_PROCESSED, (event_id,))
        return cur.fetchone() is not None

    def mark(
        self,
        event_id: str,
        event_type: str,
        processed_at: datetime | None = None,
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(INSERT_PROCESSED, (event_id, event_type, processed_at or datetime.now()))
        return cur.rowcount == 1


class PostgresIdempotencyStore:
    """幂等键的 PG 实现（M24 / A-3）。

    ------------------------------------------------------------------
    为什么必须有它

    A-3 写着"POST /runs 必须幂等，且幂等键**不能放 Redis**"。
    但 M24 之前仓库里只有 `RedisIdempotencyStore` 和 `InMemoryIdempotencyStore` ——
    组合根实际接的是 Redis。也就是说 **A-3 被违反了，而且没人发现**，
    因为它在内存实现下测试全绿。

    `IdempotencyStore` 的 Port 语义是：`get()` 返回 None = **UNKNOWN**，
    不是"没执行过"。这对外部副作用是对的（缓存丢了回查下游，绝不盲重试）。
    但"创建 Run"没有下游可以回查 —— Redis 里的键一丢，
    我们无从知道这个请求跑过没有，于是**开出第二个 Run**：
    同一个用户请求跑两遍、花两份钱，两个 Run 都可能产生外部副作用。

    ------------------------------------------------------------------
    `durable` 这一列

    两类键共用一张表，用 `durable` 区分：
        durable=TRUE   丢了就是事故（Run 创建）
        durable=FALSE  只是缓存，丢了回查下游即可（外部副作用）
    把"哪些键允许丢"做成存储层的一个可查询属性，
    而不是靠调用方记得往哪个 store 里写。
    """

    def __init__(self, conn: Any, *, durable: bool = True) -> None:
        self.conn = conn
        self.durable = durable

    def get(self, key: str) -> Mapping[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_IDEMPOTENCY, (key,))
        row = cur.fetchone()
        return dict(row["value"]) if row is not None else None

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        # 重复 put 是**静默**的：第二次写入同一键不会覆盖第一次的结果。
        # 幂等的全部意义就是"第二次不算数"，覆盖就等于抹掉了这个保证。
        cur = self.conn.cursor()
        cur.execute(INSERT_IDEMPOTENCY, (key, _json(value), self.durable))


# ---------------------------------------------------------------------------
# UnitOfWork（M29 / X-3）
# ---------------------------------------------------------------------------


class PostgresUnitOfWork:
    """一个 PG 连接 = 一个事务边界。

    ------------------------------------------------------------------
    为什么它必须存在

    `packages/` 里没有任何一处 `commit()`，适配器也不持有事务边界 ——
    它们只管发 SQL。于是"什么时候算一个事务"这个问题**没有人回答**，
    `pg_connection()` 只能开 `autocommit=True` 让写不丢。

    而 X-3 要的恰恰是：

        BEGIN
        UPDATE executions            ← 状态
        INSERT outbox_events         ← 事件
        COMMIT

    中间任何一步之后进程崩了，两条必须**一起**消失。
    autocommit 下发出去的第一条已经落库，第二条永远写不进来 ——
    下游再也不会知道这件事发生过，而系统里**没有任何报错**。

    ------------------------------------------------------------------
    为什么它是上下文管理器

    显式 `commit()` 的问题不是写起来麻烦，是**忘了不报错**：
    少写一行 commit，所有写在进程退出时静默回滚 ——
    这正是本轮一开始那个 P0（"PG = Truth" 变成空话）的形态。

    用 `with uow:` 之后，"提交"这件事从**要记得做**变成**不做就结构不完整**。
    """

    #: 它能不能真的让一批写"一起消失"。PG 上这是真的（与内存版对照，PR-28）。
    atomic = True

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        #: 提交/回滚的次数。它存在的唯一理由是让"到底提交了没有"
        #: 变成**可断言的事实**，而不是一段需要人去读代码才能确认的逻辑。
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.conn.commit()
        self.commits += 1

    def rollback(self) -> None:
        self.conn.rollback()
        self.rollbacks += 1

    def __enter__(self) -> "PostgresUnitOfWork":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.commit()
        else:
            # 回滚同样**不许静默**：异常照样往外抛（返回 False），
            # 这里只负责让这一批写一起消失。
            self.rollback()
        return False


class InMemoryUnitOfWork:
    """没有事务可言的存储上的 UnitOfWork。

    它**不是**"假装支持事务" —— 它明确记录在案：提交次数是真的记了，
    但"一起消失"这件事**没有发生**。

    为什么还要有它：进程层要用 `with uow:`，总得给它一个对象。
    而给它一个会**声称**自己支持事务的假实现，才是真正的危险 ——
    那等于在替身里把 X-3 判成通过（PR-28）。

    所以它的 `atomic` 属性永远是 `False`，测试可以据此断言：
    "这条不变量在内存实现下**不被验证**"。
    """

    #: 它能不能真的让一批写"一起消失"。内存实现永远 False。
    atomic = False

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self) -> "InMemoryUnitOfWork":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False
