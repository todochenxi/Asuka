-- ============================================================================
-- AgentOS · 019 · Context 快照（M95）
-- ----------------------------------------------------------------------------
-- 每次 LLM 调用一份 Context 快照（C-2）：它回答的是"模型**当时看到了什么**"。
-- 此前它只活在内存里（InMemoryContextSnapshotStore）——进程一重启就没了，
-- 而"它第一次为什么这么答"恰恰是重启之后最常被问的问题。
--
-- items / dropped / attributes 存 JSONB：ContextItem 是 Context 层的对象，
-- 形状会随版本演进，拆成列等于让 DB schema 钉死领域模型（同 004 的理由）。
-- run_id / created_at 提成列 —— 它们是查询路径（按 Run 列出它的每次装配）。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    execution_id  TEXT NOT NULL DEFAULT '',
    -- 实际调用的模型 / Deployment（不是"想调哪个"）—— C-5 强调记实际值
    model_id      TEXT NOT NULL DEFAULT '',
    deployment_id TEXT NOT NULL DEFAULT '',
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    -- 装进 Context 的条目（ContextItem）与被预算丢掉的（DroppedItem，C-4）
    items         JSONB NOT NULL DEFAULT '[]'::jsonb,
    dropped       JSONB NOT NULL DEFAULT '[]'::jsonb,
    attributes    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 审计热路径：按 Run 列出它的每一次装配（时间序）
CREATE INDEX IF NOT EXISTS idx_context_snapshots_run
    ON context_snapshots (run_id, created_at DESC);

COMMIT;
