-- ============================================================================
-- AgentOS · 020 · Memory（M96）
-- ----------------------------------------------------------------------------
-- 记忆的**事实源**（C-7）。此前只有 InMemoryMemoryStore —— 进程一重启，
-- "它记得什么"就没了，而记忆的全部意义就是跨 Run。
--
-- Qdrant ≠ Truth（§41）：向量索引是派生数据，重建的依据就是本表。
-- 所以这里存的是**原文与元数据**，不是向量。
-- attributes 存 JSONB：记忆的类型会随版本演进（同 004 / 019 的理由）。
-- layer / subject / created_at 提成列 —— 它们是查询路径（按层 + 主体取记忆）。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS memories (
    memory_id      TEXT PRIMARY KEY,
    layer          TEXT NOT NULL,
    -- 这条记忆是**关于谁的**（user / agent / tenant）：没有它就无法做多租户隔离
    subject        TEXT NOT NULL,
    content        TEXT NOT NULL,
    -- C-8：事件记忆必须能溯源到 source_run_id
    source_run_id  TEXT NOT NULL DEFAULT '',
    source_step_id TEXT NOT NULL DEFAULT '',
    attributes     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 检索热路径：按（层, 主体）取最近若干条
CREATE INDEX IF NOT EXISTS idx_memories_layer_subject
    ON memories (layer, subject, created_at DESC);

COMMIT;
