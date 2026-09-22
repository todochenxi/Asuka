"""真 PostgreSQL 上的取消**等待上限**（M37 / 空洞 226）。

--------------------------------------------------------------------------
这一层存在的理由：014 里有三件事**只有真库执行得到**

1. **两个部分索引的谓词**

   `idx_run_cancellations_pending` 被 014 **重建**过，谓词从
   `settled_at IS NULL` 变成 `settled_at IS NULL AND abandoned_at IS NULL`。

   这是 R-13（放弃过的必须退出队列）**唯一**的落点。
   单测只能断言"SQL 文件里写着这句话" —— 那是**源码**，不是数据库。
   真库要回答的是另一件事：**PG 真的按这句话建出了那个索引吗**。
   `DROP INDEX` + `CREATE INDEX` 少写一句，文件里照样有那句话。

2. **回填写的是 PG 方言**

   `SET abandon_after = requested_at + interval '15 minutes'`
   —— sqlite 不认 `interval`，替身是把这句**翻译**成 `datetime(...)` 再跑的。
   于是"回填以请求那一刻为起点"这件事，替身从来没真正验过 PG 原文。

3. **两条 CHECK 在真 PG 上的名字**

   替身（sqlite）也会报约束名，但那是 sqlite 的实现。
   生产报的是 PG 的约束名 —— 运维拿着报错去查的也是 PG。

--------------------------------------------------------------------------
另外一件：跨进程

取消通道的全部意义就是"写到另一个进程看得到的地方"。
所以这里用**两个连接**写/读同一张表，而不是在一个连接里自问自答。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.agent_runtime.adapters.postgres import PostgresRunCancellationStore
from packages.agent_runtime.cancellation import (
    DEFAULT_CANCELLATION_GRACE,
    RunCancellationService,
)
from packages.agent_runtime.recovery import RunRecovery

from ._pg import MIGRATIONS_DIR, RealPostgresCase, real_pg
from .test_run_cancellation_cross_process_real_pg import CrossProcessWorldPG


def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CancellationGraceOnRealPostgresTest(RealPostgresCase):
    def _indexdef(self, name: str) -> str:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,)
        )
        row = cur.fetchone()
        assert row is not None, f"索引 {name} 不存在"
        return str(row["indexdef"])

    # ---------------------------------------------------------- R-13 的落点
    def test_r13_the_pending_index_really_excludes_abandoned_rows(self) -> None:
        """真库里那个索引的谓词，不是源码里那句话。"""
        self.assertIn("abandoned_at IS NULL", self._indexdef("idx_run_cancellations_pending"))
        self.assertIn("settled_at IS NULL", self._indexdef("idx_run_cancellations_pending"))

    def test_r13_there_is_an_index_for_the_expiring_scan(self) -> None:
        """放弃扫描要能走索引 —— 否则每轮全表扫 `run_cancellations`。"""
        definition = self._indexdef("idx_run_cancellations_expiring")
        self.assertIn("abandon_after", definition)
        self.assertIn("abandoned_at IS NULL", definition)

    def test_the_control_the_old_predicate_would_have_kept_the_zombie(self) -> None:
        """控制组：这句断言在 011 的旧谓词下会**红**。

        旧谓词只有 `settled_at IS NULL`；
        而放弃过的意图 `settled_at` 恰恰还是 NULL（我们不知道它停没停）。
        少了 `abandoned_at IS NULL`，让路根本没发生。
        """
        self.assertNotEqual(
            self._indexdef("idx_run_cancellations_pending").count("IS NULL"), 1,
            "只有一个 IS NULL 就是 011 的旧形状 —— 放弃过的还会留在索引里",
        )

    # ---------------------------------------------------------- R-11 的兜底
    def test_r11_a_request_without_a_deadline_cannot_be_stored(self) -> None:
        cur = self.conn.cursor()
        with self.assertRaises(Exception) as cm:
            cur.execute(
                "INSERT INTO run_cancellations (run_id, reason, requested_by) "
                "VALUES (%s, %s, %s)",
                ("r1", "because", "alice"),
            )
        self.assertIn("run_cancellations_deadline_required", str(cm.exception))

    def test_r11_abandoning_before_the_deadline_cannot_be_stored(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO run_cancellations "
            "(run_id, reason, requested_by, abandon_after) "
            "VALUES (%s, %s, %s, %s)",
            ("r1", "because", "alice", _now() + timedelta(hours=1)),
        )
        with self.assertRaises(Exception) as cm:
            cur.execute(
                "UPDATE run_cancellations SET abandoned_at = %s WHERE run_id = %s",
                (_now() - timedelta(hours=1), "r1"),
            )
        self.assertIn("run_cancellations_abandon_after_deadline", str(cm.exception))

    # ---------------------------------------------------------- 回填（PG 原文）
    def test_the_backfill_measures_from_the_request_not_from_the_migration(self) -> None:
        """历史行的上限以**请求那一刻**为起点，不是以迁移那一刻。

        一条已经 pending 了三天的意图，不该因为这次迁移又获得 15 分钟宽限
        —— 它早就该被放弃了，而且它正是堵住队首的那一批。
        """
        cur = self.conn.cursor()
        # 推平 014 的影响，造出"迁移前"的历史行（下一个用例会重建 schema）
        cur.execute(
            "ALTER TABLE run_cancellations "
            "DROP COLUMN abandon_after, DROP COLUMN abandoned_at"
        )
        requested_at = _now() - timedelta(days=3)
        cur.execute(
            "INSERT INTO run_cancellations (run_id, reason, requested_by, requested_at) "
            "VALUES (%s, %s, %s, %s)",
            ("legacy", "because", "alice", requested_at),
        )

        cur.execute(_sql("014_cancellation_grace.sql"))

        cur.execute("SELECT abandon_after FROM run_cancellations WHERE run_id = %s", ("legacy",))
        row = cur.fetchone()
        assert row is not None
        delta = row["abandon_after"] - requested_at
        self.assertEqual(
            delta.total_seconds(), DEFAULT_CANCELLATION_GRACE.total_seconds()
        )
        # 三天前提交的、加了 15 分钟 —— 早就该到期了
        self.assertTrue(
            PostgresRunCancellationStore(self.conn).expiring(_now()),
            "回填之后这条僵尸立刻就在放弃扫描的窗口里",
        )

    # ---------------------------------------------------------- 跨进程
    def test_the_deadline_is_visible_to_another_connection(self) -> None:
        """取消通道的全部意义：另一个进程看得到。"""
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        writer = PostgresRunCancellationStore(other, grace=timedelta(0))
        reader = PostgresRunCancellationStore(self.conn, grace=DEFAULT_CANCELLATION_GRACE)

        writer.request("run_child", reason="parent cancelled", by="alice")

        seen = reader.for_run("run_child")
        assert seen is not None
        self.assertIsNotNone(seen.abandon_after)
        self.assertEqual([r.run_id for r in reader.expiring(_now())], ["run_child"])

    def test_another_connection_can_abandon_and_the_writer_sees_it(self) -> None:
        """让路也是跨进程的：sweeper 在另一个连接上，写的人看得见结果。"""
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        writer = PostgresRunCancellationStore(other, grace=timedelta(0))
        sweeper = PostgresRunCancellationStore(self.conn, grace=timedelta(0))

        writer.request("run_child", reason="parent cancelled", by="alice")
        self.assertTrue(sweeper.abandon("run_child"))

        seen = writer.for_run("run_child")
        assert seen is not None
        self.assertTrue(seen.is_abandoned)
        self.assertFalse(seen.is_settled, "没看见它停，就不能说它停了")
        self.assertEqual(list(writer.pending(64)), [], "R-13：它已经退出队列")


class AbandonedChildOnRealPostgresTest(CrossProcessWorldPG, RealPostgresCase):
    """R-12 在真库上：那笔孤儿**读得回来**。

    单测验的是"记了没有"；真库要验的是"运维看板读得到"。
    `apps/api` 的补偿视图查的就是这张表 —— 记进去了却查不出来，
    等于什么都没记。
    """

    def setUp(self) -> None:
        super().setUp()
        #: 让意图立刻到期，把"十五分钟后"压缩成"现在"
        self.cancellations = PostgresRunCancellationStore(self.conn, grace=timedelta(0))

    def test_the_orphan_is_readable_from_the_postgres_ledger(self) -> None:
        stack = self._spawned()
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        # 子 Run 的进程彻底没了：stack 不在内存里，
        # `cancel_child` 只能走 D-14（登记请求），而它永远不会再有终态
        del stack.loop.spawner._stacks[child_id]

        stack.loop.cancel(reason="user asked", by="alice")

        service = RunCancellationService(
            store=self.cancellations,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            child_registry=self.registry,
            saga=self.saga,
        )
        result = service.sweep()

        self.assertEqual(result.abandoned, (child_id,))
        rows = self._rows(
            "SELECT status, reason FROM compensations WHERE run_id = %s",
            ("run_parent",),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unresolved")
        self.assertIn("WE DO NOT KNOW", rows[0]["reason"])
        self.assertIn(child_id, rows[0]["reason"])

        # 而它**没有**被写成"已取消" —— 我们不知道它停没停
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
