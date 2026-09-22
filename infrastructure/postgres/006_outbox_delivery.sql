-- ============================================================================
-- AgentOS · 006 · Outbox 投递状态（outbox_publisher 进程的领地表）
-- ----------------------------------------------------------------------------
-- 为什么不直接给 001 的 outbox_events 加几列：
--   outbox_events 是**业务事务**写下的（X-3：状态变更与事件写入同一事务），
--   它回答的是"发生了什么"。
--   本表回答的是"我们把它发出去这件事，做到哪一步了" —— 那是**投递进程**的私有状态。
--
--   混在一张表里会发生什么：投递重试会去写业务表；清理投递状态会去动事件行；
--   两个所有者、两条生命周期挤在一行上，谁都改不动。
--   所以：两张表，两个所有者。这里不加 FK —— 分库 / 独立清理时要能各自删。
--
-- 为什么非有这张表不可：
--   多副本部署时，两个实例都会 `SELECT ... WHERE published_at IS NULL` 拿到
--   **同一批**（谁都还没 mark），于是同一条事件被投两次。
--   at-least-once 语义下这"不算错"（消费者按 event_id 去重），
--   但 N 副本 = N 倍放大，而且是在**没有任何故障**的情况下放大 —— 纯浪费。
--
--   同理，一条永远投不出去的事件（topic 不存在 / payload 超限）会反复回到候选集，
--   把重试预算烧在同一个地方 —— 必须记 attempts 并让它让位（PR-5 / PR-6）。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS outbox_delivery (
    event_id       TEXT PRIMARY KEY,          -- 对应 outbox_events.event_id
    attempts       INTEGER NOT NULL DEFAULT 0,
    claimed_by     TEXT,                      -- 当前持有者（实例 id）
    claimed_until  TIMESTAMPTZ,               -- PR-4：领地租约，过期即可被接手
    last_error     TEXT,
    dead_at        TIMESTAMPTZ,               -- PR-5：让位（不再是候选）
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- PR-6：进了死信就必须说清楚为什么。
    -- 静默丢弃比停摆更坏 —— 停摆至少有队列深度指标在报警，静默丢弃什么都不剩。
    CONSTRAINT outbox_delivery_dead_has_reason
        CHECK (dead_at IS NULL OR (last_error IS NOT NULL AND last_error <> '')),
    CONSTRAINT outbox_delivery_attempts_non_negative
        CHECK (attempts >= 0)
);

-- 认领扫描的热路径：找"没人持有 / 租约已过期"的
CREATE INDEX IF NOT EXISTS idx_outbox_delivery_claimable
    ON outbox_delivery (claimed_until)
    WHERE dead_at IS NULL;

-- PR-6：死信必须查得出来（运维要看 last_error 才知道该修什么）
CREATE INDEX IF NOT EXISTS idx_outbox_delivery_dead
    ON outbox_delivery (dead_at)
    WHERE dead_at IS NOT NULL;

COMMIT;
