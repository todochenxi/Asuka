-- ============================================================================
-- AgentOS · 008 · 子 Run 派生表
-- ----------------------------------------------------------------------------
-- 背景：M25 冻结了 D-1「重试不得开出第二条子 Run」，但它的落点
--       `ChildRunRegistry` 当时是**进程内存**里的一个 dict。
--
--       M26 的探针实测了它到底漏在哪：
--
--           父 Run 派生子 Run → SUSPENDED → 落快照 → 进程重启
--             → 恢复 → 再走一步 → 开出**第二条**子 Run
--
--       而且第二条更难发现：重走 `step()` 会 `submit` 一个**新的** Task，
--       于是派生键 `parent_execution_id` 本身都变了 ——
--       `UNIQUE` 这种物理约束在"键会变"的前提下也拦不住。
--       真正拦住它的是 §64 的 D-5/D-6：挂起必须带着"我在等谁"，
--       于是恢复出来的父 Run **根本不会再走一次派生**。
--
-- 下推到 DB 的约束：
--   D-1  `UNIQUE(parent_execution_id)` —— 一条父 Execution 最多派生一条子 Run。
--        这是**物理**保证而不是约定：
--        "先 SELECT 查一下再 INSERT" 在两个进程同时派生时两边都会通过
--        （A-11 认领、S-4 补偿认领，同款陷阱）。
--        所以绑定必须是
--            `INSERT ... ON CONFLICT (parent_execution_id) DO NOTHING` + 读 rowcount
--        rowcount = 0 就是"我没赢这场竞争"，回读那条已经存在的。
--
--   S-1  `action` 整条存下来，**含 compensation**。
--        父 Run 恢复之后才等到子 Run 的结果，那一刻要登记撤销（S-1），
--        而没有 Action 就没有逆操作声明 → `SagaCoordinator` 静默跳过 →
--        补偿账本缺一条，缺的正是子 Run 留在外部世界的副作用。
--
--   审计  每条派生都要说清"谁派出的、派出给谁、什么时候"（四个 NOT NULL）
-- ============================================================================

CREATE TABLE IF NOT EXISTS child_runs (
    child_run_id         TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL,
    parent_run_id        TEXT NOT NULL,
    -- D-1 的物理保证。见上面"为什么不能用先查再写"。
    parent_execution_id  TEXT NOT NULL UNIQUE,
    -- 父侧那条 Task：补偿记录要它（`CompensationRecord.task_id`）
    parent_task_id       TEXT NOT NULL,
    target               TEXT NOT NULL,
    -- S-1：逆操作声明住在这里
    action               JSONB NOT NULL DEFAULT '{}'::jsonb,
    status               TEXT NOT NULL DEFAULT 'created',
    spawned_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT child_runs_kind_known CHECK (kind IN ('agent', 'skill')),
    CONSTRAINT child_runs_ids_not_empty CHECK (
        child_run_id <> ''
        AND parent_run_id <> ''
        AND parent_execution_id <> ''
        AND parent_task_id <> ''
    ),
    -- 没有 action 的派生是"说不清为什么会派出这条子 Run"的派生。
    -- 与 007 的 `value <> '{}'` 同一条判据：
    -- 一条什么都记不下的记录，比没有记录更难排查。
    CONSTRAINT child_runs_action_not_empty CHECK (action <> '{}'::jsonb)
);

-- 热路径：父 Run 恢复时问"我有哪些子 Run / 我在等谁"（R-6）
CREATE INDEX IF NOT EXISTS idx_child_runs_parent_run
    ON child_runs (parent_run_id, spawned_at);

-- 说明：为什么没有 idx_child_runs_by_parent_execution
--   `parent_execution_id` 上已经有 UNIQUE 约束自带的唯一索引，够用了
--   （与 005 的同一条说明）。
