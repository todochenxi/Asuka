-- ============================================================================
-- AgentOS · 007 · 幂等键表
-- ----------------------------------------------------------------------------
-- 背景：A-3 写着"POST /runs 必须幂等，且幂等键不能放 Redis"。
--
--       `IdempotencyStore` 这个 Port 的语义是：
--           `get()` 返回 None 的含义是 **UNKNOWN**，不是"没执行过"。
--       这对**外部副作用**是对的 —— 缓存丢了就回查下游，绝不盲重试。
--
--       但 "创建 Run" 没有下游可以回查。Redis 里的键丢了，
--       我们无从知道这个请求跑过没有，结果就是**开出第二个 Run**：
--       同一个用户请求跑两遍、花两份钱，而且两个 Run 都可能产生外部副作用。
--
--       所以 Run 创建的幂等键必须与"创建 Run"本身在**同一个事务**里落 PG。
--
-- 本表同时服务两类键：
--   run:<key>         Run 创建（A-3：必须 PG）
--   exec:<id>         外部副作用去重（这个是**缓存**，Redis 丢了可以回查下游）
--
-- 两类键放在同一张表里不是偷懒 —— 是为了让"哪些键允许丢"这件事
-- 在**存储层**就有答案，而不是靠调用方记得传哪个 store。
-- 用 `durable` 列区分：durable = TRUE 的键丢失即事故，FALSE 的键丢失只变慢。
--
-- 下推到 DB 的约束：
--   A-3   幂等键必须能唯一定位到一次结果（PRIMARY KEY）
--   审计   每条键都要知道是谁、什么时候写下的（created_at 非空）
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,

    -- 首次结果。JSONB 而不是 TEXT：结果要能被查询，不只是回显。
    value           JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- durable = TRUE：这个键丢了就是事故（Run 创建，A-3）
    -- durable = FALSE：这个键只是缓存，丢了回查下游即可（外部副作用）
    durable         BOOLEAN NOT NULL DEFAULT TRUE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT idempotency_key_not_empty CHECK (key <> ''),
    -- 空结果没有意义：一次"什么都没记下"的幂等命中会让调用方拿到一个
    -- 无法解释的空响应，比没有命中更难排查。
    CONSTRAINT idempotency_value_not_empty CHECK (value <> '{}'::jsonb)
);

-- 按 durable + 时间清理：缓存型键可以过期，持久型键不清理。
-- 这是把"哪些键允许丢"做成可执行的策略，而不是一句注释。
CREATE INDEX IF NOT EXISTS idx_idempotency_cleanup
    ON idempotency_keys (durable, created_at);

COMMIT;
