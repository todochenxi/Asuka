-- ============================================================================
-- AgentOS · 014 · 取消意图的**等待上限**
-- ----------------------------------------------------------------------------
-- 背景：空洞 226 —— 一条永远无人认领的取消意图会**堵死整条通道**
--
--       M34 立下 R-10 的时候，算的是"一条"僵尸意图的代价：
--
--           `LookupError`（没有快照）→ `continue` → 它继续留在 pending 里
--           代价是一条永远没人认领的意图会一直留在 pending 里 ——
--           那是"变慢且看得见"，比"变错且没人知道"好（A-12）。
--
--       这个论证**漏了一件事**：`pending()` 是
--
--           WHERE settled_at IS NULL ORDER BY requested_at LIMIT %s
--
--       —— 它有 `ORDER BY`，也有 `LIMIT`。
--
--       一条僵尸意图的 `requested_at` 是最老的那一个，于是它**永久占据队首**：
--       每一轮都被捞出来、撞 R-10、`continue`、下一轮再被捞出来。
--       攒够 `LIMIT`（默认 64）条这样的僵尸之后，
--       **新提交的取消请求一条都进不了 pending 的窗口**。
--
--       那一刻 `sweep()` 每轮返回 0。
--       运维看到的是"没有待处理的取消"，而真实情况是
--       **跨进程取消通道停止服务**：用户按了停止，系统装作没听见。
--
--       按 A-12 重判：这不再是"变慢"，是"变错"；
--       而且它**什么都不喊** —— 脸 B 那一类（012 里写过的判据）。
--       R-10 那句"变慢且看得见"到此为止不再成立。
--
-- ----------------------------------------------------------------------------
-- 为什么"等不到回音"只能靠**上限**，不能靠"检测死亡"
--
--       一个很自然的想法是：查一下那条 Run 的进程还在不在，
--       不在就判它死了。系统里确实有类似的东西 ——
--       Kernel 的**租约**（`leases.lease_expires_at`）。
--
--       但租约过期在 Kernel 的语义里是 `LEASE_EXPIRED`，而它**可重试** ——
--       它的意思是"这个 worker 不续约了，换一个 worker 接着来"，
--       不是"这条 Run 没了"。拿它当死亡证明，
--       会误杀一条正在被 Recovery 救活的 Run。
--
--       除此之外，一条 **Run** 没有任何活性证据：
--       它没有心跳列，快照只在挂起时拍（R-1），
--       一条正在往前跑的 Run 在 PG 里什么都不写。
--
--       所以：
--
--           **没有死亡检测器。只有等待上限。**
--
--       这不是凑合 —— 这是唯一诚实的答案，前提是**上限到期后的动作
--       必须被记成"我们不知道"，而不是"它停了"**。见下面 R-11。
--
-- ----------------------------------------------------------------------------
-- 新增的两列回答两个**不同**的问题（B-7）
--
--       `settled_at`   那条 Run **确实停了**（R-10 的证据：事实侧）
--       `abandoned_at` **我们**不再等了（决策侧）
--       `abandon_after` 等到什么时候为止（策略侧，在 request 那一刻固化）
--
--   为什么不能用 `settled_at` 兼任"放弃"
--       R-10 的原话：`settled_at` 是"这条 Run 确实停了"的证据，
--       不是"取消被请求过"的证据。结掉一条还没停的 Run 的意图，
--       等于在系统里写下"我取消了它"而它还活着（PR-19）。
--       放弃等待时我们**恰恰不知道**它停没停 ——
--       把它记成 settled 就是在为一个我们看不见的对象作证。
--
--   为什么不删掉 `settled_at` 换成 `abandoned_at`
--       "它停了"与"我们不等了"是两件独立的事，而且**可以同时成立**：
--       我们先等累了（abandoned），它后来撞上安全点真的停了（settled）。
--       所以这里**刻意不加** `settled_at IS NULL OR abandoned_at IS NULL`
--       那条互斥约束 —— 见 R-14。
--
-- 下推到 DB 的约束：
--   R-11  每一条意图都必须带上等待上限。没有上限的意图 = 可能永远堵住队首的
--         意图。CHECK 是 PR-26 的兜底：主要保证在代码层（`request()` 必写）。
--   R-11  放弃不可能发生在上限之前 —— "还没到点就放弃"是不可信的记录。
-- ============================================================================

ALTER TABLE run_cancellations ADD COLUMN abandon_after TIMESTAMPTZ;
ALTER TABLE run_cancellations ADD COLUMN abandoned_at TIMESTAMPTZ;

-- 历史行：以**请求那一刻**为起点补上限，而不是以迁移那一刻 ——
-- 一条已经 pending 了三天的意图，不该因为迁移又获得 15 分钟的宽限。
UPDATE run_cancellations
   SET abandon_after = requested_at + interval '15 minutes'
 WHERE abandon_after IS NULL;

-- R-11：每一条意图都必须有上限。
-- 这条 CHECK 在两件事上都成立：新写入的（代码保证带值）与历史行（上面补过）。
ALTER TABLE run_cancellations ADD CONSTRAINT run_cancellations_deadline_required
    CHECK (abandon_after IS NOT NULL);

-- R-11：放弃不可能早于上限。
ALTER TABLE run_cancellations ADD CONSTRAINT run_cancellations_abandon_after_deadline
    CHECK (abandoned_at IS NULL OR abandoned_at >= abandon_after);

-- ---------------------------------------------------------------------------
-- R-13：放弃过的意图必须**退出 pending 队列**
--
-- 011 那个部分索引的谓词是 `settled_at IS NULL`。
-- 放弃过的意图 `settled_at` 仍然是 NULL（我们不知道它停没停），
-- 于是它照样留在索引里、照样占据队首 —— 那一列加了等于没加。
--
-- 所以索引必须重建，谓词加上 `AND abandoned_at IS NULL`。
-- 这是"让路"这件事**唯一**的落点：别的地方都不决定谁进扫描窗口。
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS idx_run_cancellations_pending;
CREATE INDEX idx_run_cancellations_pending
    ON run_cancellations (requested_at)
    WHERE settled_at IS NULL AND abandoned_at IS NULL;

-- 到期扫描的入口：按"什么时候到期"排序，不按"什么时候请求" ——
-- 最该被放弃的是**最早到点**的那条，不是最早请求的那条。
-- 同样只收还没结局的；一有结局就退出索引，扫的代价不随历史增长。
CREATE INDEX IF NOT EXISTS idx_run_cancellations_expiring
    ON run_cancellations (abandon_after)
    WHERE settled_at IS NULL AND abandoned_at IS NULL;
