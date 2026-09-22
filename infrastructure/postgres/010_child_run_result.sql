-- ============================================================================
-- AgentOS · 010 · 子 Run 的**结果**与**交付**
-- ----------------------------------------------------------------------------
-- 背景：008 只记了"派生"这一件事 —— 谁派出的、派出给谁、什么时候、逆操作是什么。
--       子 Run 跑完之后**结果去了哪里**，表上没有答案。
--
--       M30 之前答案是"结果跟着 `child_run.completed` 事件走"。这听上去够用
--       （Outbox 也在 PG 里），但它有两个硬伤：
--
--       1. Kafka 有 retention，PG 没有。
--          父 Run 挂着等子 Run，而消费者三天没跑 —— 事件早过期了，
--          那条子 Run 干了什么、返回了什么，从此**无从查证**。
--          X-5 说 PG 是唯一 Truth，可这条 Truth 只活在 Event Log 里。
--
--       2. 结果是"子 Run 干了什么"的**事实**，不是"通知父 Run"的**消息**。
--          事实属于 Truth（PG），消息属于 Log（Kafka）。把事实只放在 Log 里，
--          等于让 Truth 少一块 —— 而缺的这块正是补偿账本与审计要用的
--          （S-1：撤销参数取自子 Run 的结果）。
--
--       所以：结果在**它成为事实的那一刻**写进这张表
--       （与"子 Run 声明终态"同一个事务，X-3），事件只负责"去叫醒父 Run"。
--
-- 第二列 `delivered_at` 是 A-12 的落点。
--       唤醒路径如果只有 Kafka 一条，那么"Kafka 丢了"="父 Run 永远挂起"，
--       那是**变错**。有了这一列，`apps/child_run_consumer` 就能像
--       `recovery_controller` 那样每隔几轮扫一次 PG 兜底：
--       Kafka 丢了只是**变慢**（下一次扫到），不会变成永远没有人叫醒。
--       这与"Redis 全丢只允许变慢不允许变错"是同一条判据的第二个副本。
--
-- 下推到 DB 的约束：
--   D-6  `child_runs_outcome_consistent`：终态与 completed_at 必须同进同退。
--        "说完了却没有说完的时刻"、"没说完却有说完的时刻"都是说谎，
--        而说谎比缺失更难排查（与 007 的 `value <> '{}'` 同一条判据）。
--        终态集合取自领域对象 `TERMINAL_RUN_STATUSES`（B-3：终态不可变），
--        这里复制一份而不是引用 —— SQL 引用不了 Python，
--        于是两边不一致由测试钉住（PR-32：绿着不代表还在说真话）。
--
--   D-7  `child_runs_delivery_after_completion`：结果不可能在产生之前被交付。
--        只判 NULL 关系、不判 `delivered_at >= completed_at`：
--        后者要依赖时间格式的可比性，而那是替身（sqlite 文本时间戳）
--        给不了的承诺 —— 一条只在真库上成立的约束，不能在替身上假装成立。
--
--   幂等  `mark_finished` 用 `WHERE completed_at IS NULL` + rowcount 判胜负，
--        与 D-1 的 `ON CONFLICT DO NOTHING` 同一形状：
--        at-least-once 投递下重复处理是常态，判据必须靠 rowcount，不靠先读。
--        `mark_delivered` 同款。
-- ============================================================================

ALTER TABLE child_runs ADD COLUMN result JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE child_runs ADD COLUMN completed_at TIMESTAMPTZ;
ALTER TABLE child_runs ADD COLUMN delivered_at TIMESTAMPTZ;

ALTER TABLE child_runs ADD CONSTRAINT child_runs_outcome_consistent CHECK (
    (status IN ('completed', 'failed', 'cancelled')) = (completed_at IS NOT NULL)
);

ALTER TABLE child_runs ADD CONSTRAINT child_runs_delivery_after_completion CHECK (
    delivered_at IS NULL OR completed_at IS NOT NULL
);

-- 兜底扫的入口：哪些子 Run 已经跑完、但结果还没交回父 Run。
-- 部分索引 —— 交付完成之后这一行就退出索引，扫的代价不随历史增长。
CREATE INDEX IF NOT EXISTS idx_child_runs_undelivered
    ON child_runs (completed_at)
    WHERE completed_at IS NOT NULL AND delivered_at IS NULL;
