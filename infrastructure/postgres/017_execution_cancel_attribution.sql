-- ============================================================================
-- AgentOS · 017 · Execution 级取消的**归因**
-- ----------------------------------------------------------------------------
-- 背景：空洞 228 —— Execution 级的取消意图只有布尔位，没有"谁叫停、为什么"。
--
--       空洞登记原本写的是"没有调用方，加归因就是空转"。那句话在写下它的
--       当时是对的：确实没有人调 Kernel 的 `request()`。
--
--       但 M48 实证发现，**绕过本身就是一种调用关系**：
--       `AgentLoop._cancel_gate()` 天天在把挂起的 Execution 判死，
--       只是它跳过了"先请求"那一步 —— 直接 `kernel.cancel()`。
--
--       于是三件事同时成立：
--
--           1. `executions.cancellation_requested` 是一列**死列**（永远是 False）
--           2. `EXECUTION_CANCEL_REQUESTED` 事件永远发不出来
--           3. B-8 的归因（谁叫停、为什么）在这条链路上无处可写
--
--       第 3 条最要紧，因为它是同一条规矩在不同层上的缺口：
--
--           Run 级      `run_cancellations(reason, requested_by)`    有
--           子 Run 级   `child_runs(cancel_reason, cancel_requested_by)`  有（012 / D-14）
--           Execution 级  只有 `cancellation_requested` 一个布尔位      **没有**
--
--       而 Execution 恰恰是这条链路上最贴近"真正干活那一刀"的那一层 ——
--       一条 Run 被叫停时，说不出是谁让手上那一刀停的。
--
-- ----------------------------------------------------------------------------
-- 为什么归因跟着**请求**走，不跟着**判死**走
--
-- 判死（`cancel()`）是 Kernel 的动作，它可能由 Sweeper 发起 —— 那时"为什么"
-- 已经不是调用方那一句了。而"谁要求停它、因为什么"是**请求那一刻**的事实，
-- 只有请求的人知道。所以归因落在请求上，判死之后**不**抹掉。
--
-- ============================================================================

ALTER TABLE executions ADD COLUMN cancellation_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE executions ADD COLUMN cancellation_by    TEXT NOT NULL DEFAULT '';

-- ----------------------------------------------------------------------------
-- B-8 / A-8 的物理保证：归因与意图**同在**
--
-- 约定层面"请求取消必须带理由"是守不住的 —— 少传一个参数不会报错，
-- 而下游（审计 / 看板）拿到的是一条说不出缘由的取消。
-- 约束层面守得住：意图位为真却没归因，就是插入失败。
--
-- 与 `child_runs_cancel_attributed`（012）同一条判据，只是换了张表。
-- ----------------------------------------------------------------------------
ALTER TABLE executions ADD CONSTRAINT executions_cancel_attributed CHECK (
    (NOT cancellation_requested)
    OR (cancellation_reason <> '' AND cancellation_by <> '')
);

-- 注释刻意**不**写成 `COMMENT ON COLUMN`：那是 PG 方言，
-- 而单测跑在 sqlite 替身上（`tests/unit/sqlite_shim.py`），
-- 它直接吃迁移文件原文 —— 一句 `COMMENT ON` 会让整个建库失败。
-- 来龙去脉在上面那段注释里，库里不需要再存一份。
