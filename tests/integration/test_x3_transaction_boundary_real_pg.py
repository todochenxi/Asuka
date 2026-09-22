"""X-3：状态变更与事件写入**同一个事务** —— 在真 PG 上被验证（M29 / 空洞 208）。

--------------------------------------------------------------------------
为什么这条必须由集成层来钉

X-3 从 M15 冻结起就写在基线里，`ExecutionKernel._emit` 的注释也写着
"真实实现里是同一个事务"。但一直到 M29 之前：

    · `packages/` 里**没有任何一处 `commit()`**
    · `UnitOfWork` 这个 Port 从 M15 定义起，使用次数为 **0**

也就是说这条不变量此前只有两种成立方式：

    1. 靠 `autocommit=True` —— 那不叫"同一事务"，那叫"各自生效，
       中间没有任何东西能保证它们一起出现"；
    2. 靠内存 Outbox —— 那更糟：它确实原子，但原子在进程内存里，
       `outbox_events` 表一行都没有。

两条都不算成立，而两条都不报错。这是本文件存在的理由。

--------------------------------------------------------------------------
判据（PR-28 / PR-29）

    · 连接必须由 **`apps._bootstrap.pg_connection()`** 造出来 —— 生产用的
      那个函数。用 `RealPostgresCase.conn` 会绕过 M28 那个 `row_factory` 坑。
    · 断言必须从**另一个连接**读。同一个连接读自己未提交的写，
      读到的是自己的脏数据，那条断言什么也证明不了。
    · 事务边界必须由 **`pg_unit_of_work`** 给 —— 也就是进程真正用的那个。

--------------------------------------------------------------------------
三条断言缺一不可

    回滚后两者都不在      → 说明"没一起生效"时不会留下半个
    未提交时外部看不见    → 说明它真的在一个事务里，而不是"压根没写"
    提交后两者同时可见    → 说明"一起生效"确实发生了

少了第二条，第一条和"根本没写进库"分不清 ——
那正是 autocommit=False 之后最常见的假绿。
"""
from __future__ import annotations

import unittest
from typing import Any

from tests.integration import _pg
from tests.unit.helpers import make_task


def _production_connection() -> Any:
    """**生产**用的那个连接（PR-29）—— 不是测试替身。"""
    from apps._bootstrap import RuntimeConfig, pg_connection

    config = RuntimeConfig.from_env(
        {"AGENTOS_PG_DSN": _pg.dsn(), "AGENTOS_HEARTBEAT_SECONDS": "1"}
    )
    return pg_connection(config)


def _production_uow(conn: Any) -> Any:
    """进程真正用的那个事务边界（PR-30 / PR-31）。"""
    from apps._bootstrap import pg_unit_of_work

    return pg_unit_of_work(conn)


def _production_kernel(conn: Any) -> Any:
    """生产装配出来的 Kernel：`build_kernel`，而不是手搓三个适配器。

    手搓会绕过组合根，于是"这三个适配器是不是接的同一个连接"
    这个问题又多了一个答案 —— 而它恰好是 X-3 的全部。
    """
    from apps._bootstrap import RuntimeConfig, build_kernel

    config = RuntimeConfig.from_env(
        {"AGENTOS_PG_DSN": _pg.dsn(), "AGENTOS_HEARTBEAT_SECONDS": "1"}
    )
    return build_kernel(config, conn=conn)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        _pg.real_pg().close()                     # 推平 schema + 跑全部迁移
        self.conn = _production_connection()
        self.addCleanup(self.conn.close)
        # 观察者是**另一个连接**：同连接读自己的未提交写，证明不了任何事。
        self.observer = _pg.real_pg(fresh=False)
        self.addCleanup(self.observer.close)
        self.uow = _production_uow(self.conn)
        self.kernel = _production_kernel(self.conn)

    # ------------------------------------------------------------------ 读
    def _counts(self) -> tuple[int, int]:
        """(executions, outbox_events) —— 全部从观察者连接读。"""
        cur = self.observer.cursor()
        cur.execute("SELECT count(*) AS n FROM executions")
        executions = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM outbox_events")
        events = cur.fetchone()["n"]
        return executions, events


class TheTransactionIsRealTest(_Base):
    """先证明"这里真的有一个事务" —— 后面三条断言才有意义。"""

    def test_the_production_connection_is_not_in_autocommit_mode(self) -> None:
        """autocommit 一开，X-3 就自动不成立，而且不报错。

        开了 autocommit，`repository.add()` 和 `outbox.append()` 各自提交：
        第一条生效之后、第二条生效之前，任何东西都能插进来。
        那时"同一个事务"这句话没有任何物理对应物。
        """
        self.assertFalse(
            getattr(self.conn, "autocommit", True),
            "生产连接在 autocommit 模式下 —— 每次写各自提交，"
            "X-3（状态与事件同一事务）没有物理对应物",
        )

    def test_a_write_outside_any_boundary_is_not_visible_to_others(self) -> None:
        """控制组：不进边界的写，**另一个连接看不见**。

        这条同时是"autocommit 确实是关的"的实证 ——
        如果它是开的，这条会红。上一条断言的是属性，这一条断言的是行为。
        """
        self.kernel.submit(make_task())
        self.assertEqual(self._counts()[0], 0)


class X3TransactionBoundaryTest(_Base):
    """X-3 本体：一起生效，或者一起不发生。"""

    def test_a_committed_write_makes_both_visible_at_once(self) -> None:
        """提交之后，状态与事件**同时**出现在另一个连接里。"""
        with self.uow:
            self.kernel.submit(make_task())
        executions, events = self._counts()
        self.assertEqual(executions, 1)
        self.assertGreaterEqual(
            events, 1, "Execution 写进去了却没有事件 —— X-3 的半边"
        )

    def test_a_rolled_back_write_leaves_neither_the_status_nor_the_event(self) -> None:
        """回滚之后**两者都不在** —— 不许留下"有状态没事件"的半个。

        留下一半的后果比全丢严重：Outbox 消费者会读到一个
        指向不存在的 Execution 的事件，而它无法判断那是"还没写"
        还是"永远不会有" —— 重试预算会在这种事件上烧光。
        """
        try:
            with self.uow:
                self.kernel.submit(make_task())
                raise RuntimeError("boom: something after the status write failed")
        except RuntimeError:
            pass
        self.assertEqual(self._counts(), (0, 0))

    def test_inside_an_open_transaction_the_write_is_invisible_to_others(self) -> None:
        """事务还没提交时，另一个连接**看不见**状态，也看不见事件。

        少这条，上一条"回滚后两者都不在"就分不清是
        "真回滚了"还是"压根没写进库" —— 后者在 autocommit=False
        下极其常见，而且看起来一模一样。
        """
        with self.uow:
            self.kernel.submit(make_task())
            self.assertEqual(
                self._counts(), (0, 0), "事务外的连接看见了未提交的写"
            )

    def test_the_status_and_the_event_appear_together_after_a_second_write(self) -> None:
        """两步写（submit + claim）也在同一个事务里。

        只测 submit 会漏掉 `_persist()` 那条路径 —— 它才是
        "改状态 → 发事件"真正成对出现的地方（`submit` 只 add 一次）。
        """
        with self.uow:
            execution = self.kernel.submit(make_task())
            self.kernel.claim(
                execution.execution_id, worker_id="it"
            )
        executions, events = self._counts()
        self.assertEqual(executions, 1)
        self.assertGreaterEqual(events, 2)

    def test_a_failure_between_the_two_writes_leaves_nothing_behind(self) -> None:
        """状态已经改了、事件还没发时出错 —— 状态也必须一起退回去。

        这是 X-3 真正要防的那一次：不是"什么都没写"，
        而是"写了第一个、没写第二个"这个中间态被别人看见。
        """
        try:
            with self.uow:
                execution = self.kernel.submit(make_task())
                self.kernel.claim(
                    execution.execution_id, worker_id="it"
                )
                raise RuntimeError("boom between the status write and the event")
        except RuntimeError:
            pass
        self.assertEqual(self._counts(), (0, 0))


class TheBoundaryIsSharedTest(_Base):
    """PR-30 / PR-31：边界只有一个，五条进程共用它。"""

    def test_committing_twice_in_a_row_keeps_both_writes(self) -> None:
        """两个事务前后各提交一次 —— 边界不是一次性的。

        `PostgresUnitOfWork` 如果提交后把连接留在"已结束事务"状态，
        第二个事务的写就会静默丢失；而它丢的方式是不报错。
        """
        with self.uow:
            self.kernel.submit(make_task())
        with self.uow:
            self.kernel.submit(make_task())
        executions, events = self._counts()
        self.assertEqual(executions, 2)
        self.assertGreaterEqual(events, 2)

    def test_the_kernel_and_the_boundary_share_one_connection(self) -> None:
        """Kernel 的写必须落在被边界管理的那条连接上。

        两条连接的话，"这个请求的写在哪个事务里"就有两个答案 ——
        而提交其中一个不会让另一个生效。
        """
        self.assertIs(self.kernel.repository.conn, self.conn)


if __name__ == "__main__":
    unittest.main()
