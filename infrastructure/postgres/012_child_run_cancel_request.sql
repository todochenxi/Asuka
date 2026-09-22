-- ============================================================================
-- AgentOS · 012 · 子 Run 的**叫停痕迹**
-- ----------------------------------------------------------------------------
-- 背景：空洞 224 —— 取消与完成**赛跑**。
--
--       M33 给了 Run 级取消入口，M34 给了跨进程取消通道（011）。
--       两条都建立在同一个**没写出来**的假设上：
--
--           "父 Run 可以把那条子 Run 在登记处判成 `cancelled`。"
--
--       这个假设在**进程内**成立：父 Run 手里握着子 Run 的对象，
--       叫停之后是子 Run **自己**写终态，父侧读回来的是同一份事实。
--
--       在**跨进程**下它是假的。父 Run 看不见那条子 Run 跑到第几步，
--       却替它宣告了终态。于是有两件事同时为真：
--
--           登记处：这条子 Run 是 cancelled（父写的）
--           现实  ：这条子 Run 还在跑，而且马上就要跑完
--
--       跑完那一刻它调 `mark_finished(..., 'completed')`，
--       撞上 B-3：already 'cancelled'; it cannot become 'completed'。
--       子 Run 进程抛异常，它的**真实结果**（含 S-1 补偿要用的撤销参数）
--       从此丢失。这是**脸 A**（取消赢）—— 响，但它是个真 bug。
--
--       **脸 B**（完成赢）更坏，因为它**不响**：
--           子 Run 先跑完 → 父 Run 才取消
--             → `cancel_child` 见它已终态，原样返回
--             → `_cancel_pending_child` 见 `status != 'cancelled'`，跳过 D-12
--             → 但它**照样** `mark_delivered()`（原 B-9 第三半）
--             → 于是这条"已经产生、却从未被任何人看过"的结果被结掉了
--             → 唤醒路径再见它时是 ALREADY_DELIVERED，D-13 孤儿永不登记
--             → 子 Run 留在外部世界的副作用从账本里**静默消失**。
--
--       按 A-12（丢了之后是变慢还是变错），脸 B 比脸 A 严重：
--       脸 A 至少会喊，脸 B 什么都不喊。
--
-- ----------------------------------------------------------------------------
-- 修法：把"叫停"从**宣告**降级成**请求**
--
--    D-14  取消请求**不是终态**。父侧级联只登记请求，绝不替子 Run 写终态 ——
--          父 Run 看不见子 Run 跑到哪一步，替它写 `cancelled` 是在为一个
--          它看不见的对象作证（PR-19：说的和发生的必须是同一件事）。
--          终态只有子 Run 自己能写（`AgentLoop._emit_child_run_outcome`）。
--
--    D-15  终态一旦写下就是事实，取消请求**改不动**它。
--          取消只能拦住"还没产生的结果"；已经产生的结果必须进账本（D-13），
--          不能因为"叫停过"就当成没发生过。
--          所以这两列**不**参与 `child_runs_outcome_consistent`：
--          "有请求"同时"有终态"是合法而且**常见**的 —— 那正是赛跑的正常结局。
--
-- ----------------------------------------------------------------------------
-- 这两列与 011 的 `run_cancellations` 是什么关系（B-7：一个事实一处定义）
--
--    011  `run_cancellations`
--          回答"**谁要求停这条 Run**"。它是**动作侧**的：会被 `settle()` 结掉，
--          结掉之后只剩审计价值。
--    012  `child_runs.cancel_requested_at`
--          回答"**这次派生的结局，是不是发生在被叫停之后**"。
--          它是**事实侧**的：永不清除。
--
--    两个问题不同，生命周期也不同：一条 Run 可以被叫停十次（011 十条记录），
--    而"这条派生曾经被叫停过"只有一个是非（012 一格）。
--    把 012 也做成可结算的，赛跑的结果就永远查不到了 ——
--    而"哪些委派撞上了赛跑"正是运维要看的第一张清单。
--
-- 下推到 DB 的约束：
--    B-8  归因：一条查不出是谁要求的"叫停"，等于取消这件事没发生过（A-8 同款）。
--    D-17  因果序：请求必须早于终态。
--          真正的保证是**条件写**（两个 UPDATE 各带 `WHERE completed_at IS NULL`
--          / `WHERE cancel_requested_at IS NULL`），这条 CHECK 是 PR-26 的兜底 ——
--          兜底不能是主要保证，但主要保证漏了它必须喊出来。
-- ============================================================================

ALTER TABLE child_runs ADD COLUMN cancel_requested_at TIMESTAMPTZ;
ALTER TABLE child_runs ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE child_runs ADD COLUMN cancel_requested_by TEXT NOT NULL DEFAULT '';

-- B-8 下推：叫停必须说清"为什么"和"谁叫的"。
ALTER TABLE child_runs ADD CONSTRAINT child_runs_cancel_attributed CHECK (
    cancel_requested_at IS NULL
    OR (cancel_reason <> '' AND cancel_requested_by <> '')
);

-- D-17 下推：不可能"先终态、后被叫停"。
-- 替身（sqlite）把时间存成定长 UTC 字符串，字典序等价于时间先后，
-- 所以这条约束在真库与替身上是同一条约束，不是"只在真库上成立"的假承诺。
ALTER TABLE child_runs ADD CONSTRAINT child_runs_cancel_before_outcome CHECK (
    cancel_requested_at IS NULL
    OR completed_at IS NULL
    OR cancel_requested_at <= completed_at
);

-- "叫停过、但它还没终态" —— 赛跑**进行中**的那批。
-- 部分索引：子 Run 一终态就退出索引，扫的代价不随历史增长。
CREATE INDEX IF NOT EXISTS idx_child_runs_cancel_pending
    ON child_runs (cancel_requested_at)
    WHERE cancel_requested_at IS NOT NULL AND completed_at IS NULL;
