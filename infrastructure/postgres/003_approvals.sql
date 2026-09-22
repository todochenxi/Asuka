-- ============================================================================
-- AgentOS · 003 · HITL 审批表
-- ----------------------------------------------------------------------------
-- 背景：审批不是一个"待办事项"，它是 Kernel 里那条 SUSPENDED(HUMAN_APPROVAL)
--       Execution 的**唤醒条件**。所以它必须活过进程重启 —— 人第二天来上班，
--       Run 还在等他，审批记录也还得在。
--
-- 因此这里用 PG，不用 Redis：
--   Redis 丢了 → 界面上什么都没有，而系统里全在等（A-10）。
--   这是最难排查的一类故障，因为它**不报错**。
--
-- 本表把三条原本只在 Python 里检查的规则下推到 DB ——
-- 领域里的检查是"善意"，DB 约束才是"兜底"：
--   H-1  审批必须有 reason
--   H-5  expires_at > requested_at（一条没有截止时间的审批 = 允许人永久挂住 Run）
--   A-8  已决定（approved / rejected）必须有 decided_by（匿名审批进不了审计）
--
-- 另外一条反向约束同样重要：
--   PENDING 不能有 decided_by。否则"待批"状态里藏着一个决策者，
--   事后无法区分"还没人批"和"批过了但状态没推进"。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    execution_id    TEXT,                       -- 挂起后回填（H-6：只能绑一次）
    status          TEXT NOT NULL,

    -- H-1：没有 reason 的审批等于让人盲签
    reason          TEXT NOT NULL,

    -- 审批对象本身：人要看的是"我要放行的是什么"
    action          JSONB NOT NULL DEFAULT '{}'::jsonb,

    requested_at    TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,

    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    comment         TEXT NOT NULL DEFAULT '',

    -- E-13：并发写走乐观锁，避免后到的写覆盖先到的决策
    version         INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT approvals_status_known
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),

    -- H-5：截止时间必须晚于发起时间
    CONSTRAINT approvals_ttl_positive
        CHECK (expires_at > requested_at),

    -- A-8：被"人"决定过的审批必须留下决策者（expired 是 timeout，不是人）
    CONSTRAINT approvals_decided_has_actor
        CHECK (status NOT IN ('approved', 'rejected') OR decided_by IS NOT NULL),
    CONSTRAINT approvals_decided_has_time
        CHECK (status NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL),

    -- 反向：待批的审批里不能藏着决策者
    CONSTRAINT approvals_pending_has_no_actor
        CHECK (status <> 'pending' OR decided_by IS NULL)
);

-- 待批列表的热路径（A-10：这个列表服务重启后还得查得到）
CREATE INDEX IF NOT EXISTS idx_approvals_run_pending
    ON approvals (run_id)
    WHERE status = 'pending';

-- 超时扫描的热路径（expire_due() 按 expires_at 扫）
CREATE INDEX IF NOT EXISTS idx_approvals_expiring
    ON approvals (expires_at)
    WHERE status = 'pending';

COMMIT;
