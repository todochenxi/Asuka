-- ============================================================================
-- AgentOS · 011 · **Run 级**取消意图
-- ----------------------------------------------------------------------------
-- 背景：M33 补了 Run 级取消入口（`AgentLoop.cancel()`），但只走通了一半。
--
--       进程内：父 Run 拿到子 Run 的 stack，直接调它自己的 `cancel()` ——
--               子 Run 会关自己的闸门、记自己的账、再往下传给孙 Run。
--       跨进程：父 Run 手里只有 `child_run_id`，没有子 Run 的对象。
--               它能做的只是把登记处那一行判成 `cancelled` ——
--               而跑在**另一个进程里**的那条 Run，没有任何人告诉它。
--
--       于是"取消"在跨进程下变成了两件不同的事：
--         父侧事实：这条子 Run 的结果我不要了（登记处写着 cancelled）
--         子侧事实：我还活着，我还在跑
--       两件都是真的，而它们说的是相反的东西。
--
--       Kernel 现有的取消是 **Execution 级**的（`cancellation_requested`
--       挂在 Execution 上）。一条子 Run 有它自己的一堆 Execution，
--       而且它在等孙 Run 的时候**手上根本没有活的 Execution** ——
--       Execution 级的信号没有地方挂。所以必须有 Run 级这一层。
--
-- 三段式，与 Kernel 的 Execution 级取消同构（`packages/execution_kernel/cancellation.py`）：
--
--       Durable Intent   PostgreSQL：本表（唯一事实来源）
--       Safe Point       `AgentLoop._step()` 顶部：协作式，读到就自己停
--       Sweeper          系统级：兜住所有"根本走不到安全点"的 Run
--
--       Safe Point 兜不住的那些才是这张表存在的理由：
--       一条停在 `WAITING_CHILD` 的 Run 不会调 `step()` —— 它在等别人。
--       它这一辈子可能再没有第二个安全点，于是只有 Sweeper 能叫醒它。
--
-- 下推到 DB 的约束：
--   B-8  `reason` / `requested_by` 都非空（CHECK）。
--        匿名取消进不了审计，说不出原因的取消同样进不了 ——
--        这两条在 Python 侧已经由 `AgentLoop.cancel()` 守着，
--        这里再守一次不是重复：DB 是**最后一个**能拦住它们的地方
--        （与 007 的 `value <> '{}'` 同一条判据）。
--
--   R-7  意图先于宣告。`settled_at` 为空表示"还没被认领"。
--        CHECK 只判 `settled_at >= requested_at` ——
--        一条意图不可能在它被写下之前就被处理掉（与 010 的 D-7 同款形状）。
--
--   幂等  `run_id` 是主键：一条 Run 最多一个取消请求。
--        重复请求是 upsert（不是插入第二条），
--        于是"父 Run 取消了两次"不会变成两条意图、两次记账。
--
--   扫的代价  部分索引 `WHERE settled_at IS NULL`：
--        处理完的请求退出索引，于是 sweep 的代价不随历史增长
--        （与 010 的 `idx_child_runs_undelivered` 同款）。
-- ============================================================================

CREATE TABLE IF NOT EXISTS run_cancellations (
    run_id        TEXT PRIMARY KEY,
    reason        TEXT        NOT NULL,
    requested_by  TEXT        NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at    TIMESTAMPTZ
);

-- B-8 下推：说不出是谁、说不出为什么的取消，进不了这张表。
ALTER TABLE run_cancellations ADD CONSTRAINT run_cancellations_attributed CHECK (
    length(reason) > 0 AND length(requested_by) > 0
);

-- R-7：一条意图不可能在它被写下之前就被处理掉。
ALTER TABLE run_cancellations ADD CONSTRAINT run_cancellations_settled_after_request CHECK (
    settled_at IS NULL OR settled_at >= requested_at
);

-- 兜底扫的入口：哪些 Run 被请求了取消、但还没有人认领。
CREATE INDEX IF NOT EXISTS idx_run_cancellations_pending
    ON run_cancellations (requested_at)
    WHERE settled_at IS NULL;
