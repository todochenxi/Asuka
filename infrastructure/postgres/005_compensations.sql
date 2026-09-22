-- 005 · 补偿账本（M10 Saga / Compensation）
--
-- 一张表，回答一个问题：**这个 Run 在外部世界留下过什么，撤销掉了没有。**
--
-- 为什么必须落库（A-12 的判据）：
--   账本丢了 → 副作用还在，但没人知道要撤销 → **变错**，不是变慢。
--
-- 为什么不用 Redis：
--   "待撤销"是一份要被**扫**的清单（运维看板、补偿重试），不是热路径上的一次读。

CREATE TABLE IF NOT EXISTS compensations (
    compensation_id     TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    step_id             TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    -- S-2：一条 Execution 最多一条补偿记录。
    -- 这不是约定，是物理约束：否则两个 Coordinator 各自记一条，
    -- 同一笔副作用会被撤销两次（而"撤销的撤销"多数情况下是另一笔真实副作用）。
    execution_id        TEXT NOT NULL UNIQUE,
    action_type         TEXT NOT NULL,
    tool                TEXT NOT NULL,
    args                JSONB NOT NULL DEFAULT '{}'::jsonb,
    description         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    reason              TEXT NOT NULL DEFAULT '',
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    -- 值域：状态只能是这四个（S-16 加了 not_needed —— 成功运行的副作用不需要撤销）
    CONSTRAINT compensations_status_known CHECK (
        status IN ('pending', 'running', 'compensated', 'unresolved', 'not_needed')
    ),
    -- S-5：撤销不了必须留下原因，否则下一个人无从下手
    CONSTRAINT compensations_unresolved_has_reason CHECK (
        status <> 'unresolved' OR reason <> ''
    ),
    CONSTRAINT compensations_version_positive CHECK (version > 0),
    CONSTRAINT compensations_attempts_non_negative CHECK (attempts >= 0)
);

-- 热路径 1：一个 Run 失败时，按**产生顺序倒序**取出待撤销项（S-3 LIFO）
CREATE INDEX IF NOT EXISTS idx_compensations_run_open
    ON compensations (run_id, created_at DESC)
    WHERE status IN ('pending', 'running');

-- 热路径 2：运维看板 —— 哪些副作用是撤销不掉的（S-5 的落点）
CREATE INDEX IF NOT EXISTS idx_compensations_unresolved
    ON compensations (run_id)
    WHERE status = 'unresolved';

-- 说明：为什么没有 idx_compensations_by_execution
--   `execution_id` 上已经有 UNIQUE 约束自带的唯一索引，够用了。
--
-- 说明：为什么不把 args 拆成列
--   撤销参数是工具相关的（创建订单要 order_id，发消息要 message_id），
--   拆列等于让表结构去钉死工具契约。只把**会被查询的**提成列
--   （run_id / status / execution_id），其余原样存 JSONB（与 004 同一条判据）。
