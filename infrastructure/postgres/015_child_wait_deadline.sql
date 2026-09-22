-- ============================================================================
-- AgentOS · 015 · 一次派生的**等待上限**
-- ----------------------------------------------------------------------------
-- 起因（空洞 229，M37 登记）
--
--   M37 治掉的是**取消**那一侧的等待：一条被叫停的子 Run 再也没有回音时，
--   `run_cancellations` 那条意图会在 `abandon_after` 到点后放弃等待（R-11），
--   让出 `pending()` 的队首（R-13）。
--
--   但那一轮在 `_book_abandonment()` 里留了一句注释就走了：
--
--       parent not terminal → 不是孤儿，是另一件事
--
--   那"另一件事"就是本文件要治的洞：
--
--       父 Run 挂在 WAITING_CHILD 上，等一条**永远不会再产生结果**的子 Run
--         → 没有任何事件会来（它自己都没有终态）
--         → `ChildRunWaker.sweep()` 只扫 `completed_at IS NOT NULL`（没有）
--         → `undelivered()` 也扫不到（它没终态）
--         → 父 Run 永远挂着，界面显示"在等子 Agent"，一切正常
--
--   它比 M37 那一侧更静：取消那一侧至少有一条每 tick 被捞起、被 `continue`
--   掉的请求（看得见的空转）；这一侧**连空转都没有** —— 没有任何一行代码
--   每 tick 会去看它一眼。
--
-- ----------------------------------------------------------------------------
-- 为什么**另开**一列，而不是复用 014 的 `abandon_after`（B-7）
--
--   两列都叫"等到什么时候为止"，但它们回答的是两个问题：
--
--     run_cancellations.abandon_after  我要求它停，等多久确认它停了
--     child_runs.wait_until            我等它的结果，等多久算等不到
--
--   前者是**取消**语义（R-11），后者是**挂起**语义（D-18）。
--   合成一列的后果不是"少一列"，而是"一次派生有两个等待上限" ——
--   一个等待会有两个答案，那正是 B-7 不许出现的状态。
--
--   顺带：两侧的默认时长也刻意不同（15 分钟 vs 30 分钟）。
--   "确认停了"要快，因为它在占取消通道的队首；
--   "等结果"可以慢，因为子 Run 跑几个小时是正常的。
--
-- ----------------------------------------------------------------------------
-- 为什么 `wait_expired_at` 不是"终态"
--
--   到期只说明**我们不再等了**，不说明那条子 Run 发生了什么。
--   它可能还在跑、可能早就死了、可能马上就要交回结果。
--   所以这里**不写** `completed_at`、不写 `delivered_at`、不写 `status` ——
--   那三格属于"这条子 Run 的结局"，而结局只有那条子 Run 自己有资格写
--   （D-14 的同一条理由：取消请求不是终态，等待到期也不是终态）。
--
--   于是到期之后如果那条子 Run **真的**回来了，唤醒路径照样能认它：
--   `undelivered()` 的谓词是 `delivered_at IS NULL`，不含 `wait_expired_at`，
--   所以它不会被"已经到期"挡在门外 ——
--   父 Run 已终态 → 走 D-13 孤儿；父 Run 还活着 → 结果照常交回。
--
-- ----------------------------------------------------------------------------
-- 为什么必须有 `idx_child_runs_overdue`，而且谓词就是判据本身（R-13 的同款）
--
--   到期扫和 M37 的 `pending()` 是同一个形状：
--   `ORDER BY wait_until LIMIT %s`。一条已经到期、却没有退出队列的行，
--   它的 `wait_until` 永远是最小的 —— 于是它**永久占据队首**，
--   攒够 LIMIT 条之后，真正需要处理的新到期一个也进不来。
--
--   所以"已经处理过"这件事的**唯一落点是索引谓词**，不是 Python 侧的
--   `if handle.is_wait_expired: continue`。后者在真 PG 上一样堵：
--   Python 过滤发生在**捞出来之后**，队首还是被占着。
-- ============================================================================

ALTER TABLE child_runs ADD COLUMN wait_until TIMESTAMPTZ;
ALTER TABLE child_runs ADD COLUMN wait_expired_at TIMESTAMPTZ;

-- 历史行回填：上限从**派生那一刻**起算（`spawned_at`），不是从迁移那一刻起算。
-- 后者会让"三天前派出、早该到期"的派生再白等 30 分钟，
-- 而它等的那条子 Run 多半连进程都没了。
UPDATE child_runs
   SET wait_until = spawned_at + interval '30 minutes'
 WHERE wait_until IS NULL;

-- 没有上限的派生 = 一次"等到世界末日"的派生。
-- 这一格由登记处在第一次 `bind()` 时固化，不允许后来补 ——
-- 后来补的上限会把"当时约定等多久"改成一个新的数（B-7：一个事实一处定义）。
ALTER TABLE child_runs ADD CONSTRAINT child_runs_wait_deadline_required
    CHECK (wait_until IS NOT NULL);

-- 上限必须在派生之后。倒过来（上限早于 spawn）会让这条派生
-- 在产生的瞬间就已经逾期 —— 于是它一进队列就该被"到期处理"，
-- 而它其实连一次被等的机会都没有。
ALTER TABLE child_runs ADD CONSTRAINT child_runs_wait_deadline_after_spawn
    CHECK (wait_until > spawned_at);

-- 到期不早于上限。与 014 的 `run_cancellations_abandon_after_deadline`
-- 是同一条判据的第二个副本：先有约定，后有处置。
ALTER TABLE child_runs ADD CONSTRAINT child_runs_wait_expired_after_deadline
    CHECK (wait_expired_at IS NULL OR wait_expired_at >= wait_until);

-- 到期扫的队首（见文件头最后一段）。
-- 三个条件缺一不可：
--   delivered_at IS NULL    结果还没交回 —— 交回过就不再是"等不到"
--   completed_at IS NULL    还没有终态 —— 有终态该走唤醒路径，不该走到期路径
--   wait_expired_at IS NULL 还没到过期处置过 —— **让出队首的那一格**
CREATE INDEX IF NOT EXISTS idx_child_runs_overdue
    ON child_runs (wait_until)
    WHERE delivered_at IS NULL
      AND completed_at IS NULL
      AND wait_expired_at IS NULL;
