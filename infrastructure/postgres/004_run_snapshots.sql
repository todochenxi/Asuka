-- ============================================================================
-- AgentOS · 004 · Run 恢复快照
-- ----------------------------------------------------------------------------
-- 背景：§14 要求"进入 SUSPENDED 前强制写 Checkpoint"，但**只写 Checkpoint
--       恢复不了** —— 它记的是指针（current_step / completed_tasks），不是数据。
--       唤醒之后仍然不知道 Agent 当时认为世界是什么样、走了几步、花了多少钱，
--       于是只能重跑整个 Run，而重跑对已经产生过外部副作用的 Task 是灾难。
--
-- 所以 R-1：挂起前必须**同时**落 Snapshot。本表就是那个 Snapshot 的落点。
--
-- 为什么整块状态存 JSONB 而不是拆成列：
--   State / Plan / Observation 是 Intelligence 层的对象，形状会随版本演进。
--   拆成列等于让 DB schema 去钉死领域模型 —— 那正是 §14 想避免的
--   "用表结构决定领域模型"。这里 DB 只负责"原样存下来、原样读回去"。
--
-- 但 run_id / status / step_count 这些**会被查询**的字段必须提成列：
--   恢复时按 run_id 查最新一条；运维要看"有哪些 Run 挂在等审批"。
--   全塞 JSONB 里就只能全表扫 JSON，那是把热路径建在了最慢的地方。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS run_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,

    -- 会被查询 / 会被展示的字段提成列（见文件头）
    agent_id            TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    step_count          INTEGER NOT NULL DEFAULT 0,       -- 预算计数，不是 Step 个数
    consecutive_denials INTEGER NOT NULL DEFAULT 0,       -- L-7，不恢复就能绕开 DENY_LOOP
    pending_approval_id TEXT,                             -- 挂着等审批时的"接得上人"的钥匙
    current_step_id     TEXT NOT NULL DEFAULT '',

    -- 整块状态：State（含 Goal / Plan / Observations）
    state               JSONB NOT NULL,
    -- Step 列表（每个含 step_id / plan_node_id / task_ids / status）
    steps               JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- 已花费：cost / tokens / steps（R-2：不恢复就变成重置预算的后门）
    spent               JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- R-4：审计账本本身也要带走，否则重启把一个 Run 的账切成两截
    trace               JSONB NOT NULL DEFAULT '[]'::jsonb,

    reason              TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT run_snapshots_state_present CHECK (state <> '{}'::jsonb)
);

-- 恢复的热路径：按 run 取最新一条
CREATE INDEX IF NOT EXISTS idx_run_snapshots_latest
    ON run_snapshots (run_id, created_at DESC);

-- 运维视角：现在有哪些 Run 挂在等审批（A-10 的列表也能从这里出）
CREATE INDEX IF NOT EXISTS idx_run_snapshots_gated
    ON run_snapshots (pending_approval_id)
    WHERE pending_approval_id IS NOT NULL;

COMMIT;
