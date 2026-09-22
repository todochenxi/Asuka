"""生产迁移器（M67 / M11 部署编排第一块）。

--------------------------------------------------------------------------
它补的是什么空洞

`infrastructure/postgres/*.sql` 此前**只有一个消费者**：集成测试。
它的用法是 `DROP SCHEMA public CASCADE` 之后把全部文件重放一遍 ——
那是"每次从零来一遍"，不是迁移。

于是生产环境没有一个能安全执行的入口：

    · 第一次上线靠什么建库？  没人管，只能手工 psql 粘贴
    · 第二次上线怎么升级？     再粘贴一遍 —— 而 ALTER TABLE 不幂等，
                               `017` 是一句裸 `ALTER TABLE ADD COLUMN`，
                               重放第二遍就会 `column already exists`
    · 怎么知道线上到底跑到了哪一版？  不知道，只能连上去看有没有某张表

第三条的危害最大：**"线上 schema 是什么版本"没有答案**。
而它一旦没有答案，下面两类事故就都只能靠人肉回忆来排除：

    1. 代码比 schema 新（跑了一段还没上线的 SQL）—— 报"列不存在"
    2. schema 比代码新（回滚了代码没回滚 schema）—— 静默多出几列

--------------------------------------------------------------------------
为什么是"记录已应用"而不是"让 SQL 幂等"

两条路都能让第二遍跑得通：

    A. 把 17 个文件全改写成 `ADD COLUMN IF NOT EXISTS` 的形式
    B. 记下"哪些已经跑过"，跑过的不再跑

选 B。理由是 A 会**篡改历史**：

    已经上线的迁移是不可变的事实 —— 它在某一天被真的执行过，
    产出的列定义就是那一句 SQL 写的那样。把它改写成幂等形式，
    等于宣称"它一直都是这样"，于是**线上库与文件首次分叉**。

更实际的：A 治不了"部分失败"。一句 ALTER 跑到一半失败时，
`IF NOT EXISTS` 帮不上忙，仍然需要知道"它到底跑没跑成"。

--------------------------------------------------------------------------
校验原则（PR-19）

已应用的迁移内容被改动 → **点名拒绝**，不静默接受、不重放。
改动一个已经上线过的迁移文件，等同于篡改账本里的旧页；
这在有 Outbox 与审批账本的系统里不是"小改动"。
"""
from __future__ import annotations

from .app import (
    BOOKKEEPING_TABLE,
    DEFAULT_MIGRATIONS_DIR,
    MIGRATION_LOCK_KEY,
    Migration,
    MigrationError,
    Plan,
    apply_pending,
    checksum,
    connect,
    discover,
    ensure_bookkeeping,
    plan,
    read_applied,
    release_lock,
    report,
    unwrap_transaction,
    with_lock,
)

__all__ = [
    "BOOKKEEPING_TABLE",
    "DEFAULT_MIGRATIONS_DIR",
    "MIGRATION_LOCK_KEY",
    "Migration",
    "MigrationError",
    "Plan",
    "apply_pending",
    "checksum",
    "connect",
    "discover",
    "ensure_bookkeeping",
    "plan",
    "read_applied",
    "release_lock",
    "report",
    "unwrap_transaction",
    "with_lock",
]
