-- ============================================================================
-- AgentOS · Execution Kernel · PostgreSQL Schema
-- ----------------------------------------------------------------------------
-- 设计原则：
--   1. 表结构由 Execution / Attempt / Lease / Checkpoint 的**生命周期反推**出来，
--      不是反过来用表结构决定领域模型。
--   2. PostgreSQL = Current Durable State / Business Source of Truth。
--      Kafka 只是 Durable Event Log（经 Outbox 投递），不是事实来源。
--   3. Lease 内联在 executions 上（1:1，且必须与状态同事务更新）。
--   4. 所有并发写走 version 乐观锁（E-13）。
--
-- 参数占位符风格：DB-API (%s)，适配 psycopg2 / psycopg3。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Task：Scheduler 只认识它（E-12）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    step_id             TEXT NOT NULL,                 -- E-11：可溯源到 Step
    task_type           TEXT NOT NULL,
    executor_type       TEXT NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority            INTEGER NOT NULL DEFAULT 0,
    tenant_id           TEXT,
    resource_requirement JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry_policy        JSONB NOT NULL DEFAULT '{}'::jsonb,
    timeout_seconds     INTEGER NOT NULL DEFAULT 60,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tasks_run        ON tasks (run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_step       ON tasks (step_id);
-- Scheduler 的热路径：按状态无关，全靠 executions 的 status 索引驱动

-- ---------------------------------------------------------------------------
-- Execution：Kernel 生命周期实体
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS executions (
    execution_id           TEXT PRIMARY KEY,
    task_id                TEXT NOT NULL,
    idempotency_key        TEXT NOT NULL,              -- E-21：= execution_id，跨 Attempt 稳定
    execution_mode         TEXT NOT NULL DEFAULT 'task',
    status                 TEXT NOT NULL,
    parent_id              TEXT,
    current_attempt_no     INTEGER NOT NULL DEFAULT 0,

    -- Lease（E-20：挂在 Execution；1:1 所以内联，保证与状态同事务）
    lease_worker_id        TEXT,
    lease_fencing_token    BIGINT,                     -- E-22：单调递增，写回校验
    lease_acquired_at      TIMESTAMPTZ,
    lease_expires_at       TIMESTAMPTZ,
    lease_heartbeat_at     TIMESTAMPTZ,

    -- Suspension（等待原因是 reason 字段，不是顶层状态）
    suspension_reason      TEXT,
    suspension_wait_cond   JSONB,
    suspended_at           TIMESTAMPTZ,

    -- Cancellation：**意图**字段，不是状态（真正的 CANCELLED 才是状态）
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,

    version                INTEGER NOT NULL DEFAULT 1,  -- E-13 Optimistic Lock
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_executions_task UNIQUE (task_id),              -- E-19：Task : Execution = 1 : 1
    CONSTRAINT uq_executions_idem UNIQUE (idempotency_key),      -- E-21
    CONSTRAINT ck_executions_status CHECK (
        status IN ('PENDING', 'RUNNING', 'STALE', 'SUSPENDED',
                   'COMPLETED', 'FAILED', 'CANCELLED')
    ),
    CONSTRAINT ck_executions_suspension CHECK (
        (status = 'SUSPENDED' AND suspension_reason IS NOT NULL)  -- E-8
        OR (status <> 'SUSPENDED' AND suspension_reason IS NULL)
    )
);

-- Scheduler：扫描 Runnable（PENDING）
CREATE INDEX IF NOT EXISTS idx_exec_status ON executions (status);

-- Recovery Controller：只关心"持有 Lease 且已过期"的执行
CREATE INDEX IF NOT EXISTS idx_exec_lease_expiry
    ON executions (lease_expires_at)
    WHERE status = 'RUNNING' AND lease_expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_exec_task ON executions (task_id);
CREATE INDEX IF NOT EXISTS idx_exec_run  ON executions (parent_id);

-- ---------------------------------------------------------------------------
-- Attempt：一次具体尝试（E-4：Retry = 新 Attempt，不是状态回退）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id      TEXT PRIMARY KEY,
    execution_id    TEXT NOT NULL REFERENCES executions (execution_id) ON DELETE CASCADE,
    attempt_no      INTEGER NOT NULL,                  -- E-6：单调递增，不可复用
    status          TEXT NOT NULL,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    error_code      TEXT,
    error_message   TEXT,
    failure_class   TEXT,                              -- Kernel Failure Class，决定能否重试
    result          JSONB,
    checkpoint_id   TEXT,
    version         INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT uq_attempts_no UNIQUE (execution_id, attempt_no),   -- E-6
    CONSTRAINT ck_attempts_status CHECK (
        status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED')
    )
);

CREATE INDEX IF NOT EXISTS idx_attempts_exec ON attempts (execution_id);

-- ---------------------------------------------------------------------------
-- Checkpoint：分两层（E-24）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kernel_checkpoints (
    checkpoint_id     TEXT PRIMARY KEY,
    execution_id      TEXT NOT NULL,
    attempt_no        INTEGER NOT NULL,
    seq               INTEGER NOT NULL,
    execution_state   JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key   TEXT NOT NULL,
    fencing_token     BIGINT NOT NULL,
    artifact_refs     JSONB NOT NULL DEFAULT '[]'::jsonb,
    recovery_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_kckpt_seq UNIQUE (execution_id, attempt_no, seq)
);

CREATE TABLE IF NOT EXISTS run_checkpoints (
    checkpoint_id       TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    current_step        TEXT NOT NULL,
    completed_tasks     JSONB NOT NULL DEFAULT '[]'::jsonb,
    variables           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_snapshot_id TEXT,                          -- 引用，不内嵌
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runckpt_run ON run_checkpoints (run_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Outbox：X-3，状态变更与事件写入同一事务
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id          TEXT PRIMARY KEY,
    aggregate_type    TEXT NOT NULL,
    aggregate_id      TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    aggregate_version INTEGER NOT NULL DEFAULT 1,
    published_at      TIMESTAMPTZ                       -- NULL = 待投递
);

-- Outbox Publisher 的热路径
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events (occurred_at)
    WHERE published_at IS NULL;

COMMIT;
