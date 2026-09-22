-- ============================================================================
-- AgentOS · 002 · Outbox Consumer 去重表
-- ----------------------------------------------------------------------------
-- 背景：Outbox → Kafka 是 **至少一次** 投递（publisher.py 先 publish 后 mark，
--       进程崩溃就会重投）。重复不是异常，是常态。
--
-- 所以"按 event_id 去重"不是优化，是**正确性的一部分**。
--
-- 为什么用 PG 而不是 Redis 存去重记录：
--   Redis 丢了 → 整批历史事件被重新处理一遍（Read Model 重复计数、审计重复写入）。
--   去重记录本身必须持久，它属于"正确性"，不属于"快路径"。
--   （对比：Lease 索引可以丢，因为它只是加速器，能从 PG 重建。）
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS processed_events (
    event_id      TEXT PRIMARY KEY,           -- 去重键，来自 Event.event_id
    event_type    TEXT NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 定期清理过期去重记录时用（保留窗口按 Replay 周期定，默认 30 天）
CREATE INDEX IF NOT EXISTS idx_processed_at
    ON processed_events (processed_at);

COMMIT;
