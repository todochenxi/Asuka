"""M35 / 空洞 224：取消与完成**赛跑**。

--------------------------------------------------------------------------
这个洞长什么样

M33 给了 Run 级取消入口，M34 给了跨进程取消通道。两条都建立在同一个
**没写出来**的假设上：

    "父 Run 可以把那条子 Run 在登记处判成 `cancelled`。"

进程内这句话成立（父 Run 握着子 Run 的对象，叫停之后是**子 Run 自己**
写终态，父侧读回来的是同一份事实）。跨进程下它是假的 ——
父 Run 看不见那条子 Run 跑到第几步，却替它宣告了终态。

于是有两件事同时为真，而且它们说的是相反的东西：

    登记处：这条子 Run 是 cancelled（父写的）
    现实  ：这条子 Run 还在跑，而且马上就要跑完

**脸 A（取消赢）** —— 它跑完那一刻调 `mark_finished('completed')`，
撞 B-3 `InvariantViolation` 抛异常，真实结果（含 S-1 的撤销参数）丢失。
响，但它是个真 bug。

**脸 B（完成赢）** —— 更坏，因为它**不响**：

    子 Run 先跑完 → 父 Run 才取消
      → `cancel_child` 见它已终态，原样返回
      → `_cancel_pending_child` 见 `status != 'cancelled'`，跳过 D-12
      → 但它**照样** `mark_delivered()`
      → 于是这条"已经产生、却从未被任何人看过"的结果被记为已交付
      → 唤醒路径再见它时是 ALREADY_DELIVERED，D-13 孤儿永不登记
      → 子 Run 留在外部世界的副作用从账本里**静默消失**

按 A-12（丢了之后是变慢还是变错），脸 B 比脸 A 严重：
脸 A 至少会喊，脸 B 什么都不喊。

--------------------------------------------------------------------------
修法：把"叫停"从**宣告**降级成**请求**

    D-14  取消请求**不是终态**。父侧只登记请求，绝不替子 Run 写终态。
          终态只有一个作者：那条子 Run 自己。
    D-15  终态一旦写下就是事实，取消请求改不动它。
          取消只能拦住"还没产生的结果"。
    D-16  赛跑的两种结局**负责人不同**：
            它读到请求后自己停了 → `cancelled` → 父侧 D-12
            它没读到、先跑完了   → `completed` → 唤醒路径 D-13 孤儿
    D-17  因果序：请求必须早于终态（`cancel_requested_at <= completed_at`）。

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_runtime.child_wake import ChildWakeOutcome
from packages.agent_runtime.delegation import ChildRunRegistry, InProcessChildRunSpawner

from .sqlite_shim import connect, load_schema_sql
from .test_child_run_wake import _handle
from .test_run_cancellation_cross_process import CrossProcessWorld


# ---------------------------------------------------------------- 脸 A


class FaceATheChildOutrunsTheRequestTest(CrossProcessWorld):
    """父先叫停，子**后**跑完 —— 旧实现在这里撞 B-3 抛异常。"""

    def _cross_process(self) -> tuple[Any, str, Any]:
        parent, child_id, child_loop = self._spawned_with_child()
        spawner = parent.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        spawner._stacks.clear()          # 子 Run 在别的进程里
        return parent, child_id, child_loop

    def test_the_parent_does_not_declare_the_outcome(self) -> None:
        """D-14：跨进程叫停之后，登记处**没有**终态。"""
        parent, child_id, _child = self._cross_process()

        parent.cancel(reason="not needed", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished)
        self.assertNotEqual(handle.status, "cancelled")
        self.assertTrue(handle.is_cancel_requested)

    def test_the_child_still_finishes_without_blowing_up(self) -> None:
        """回归测试：旧实现在这一步抛 `InvariantViolation`（B-3）。

        抛的后果不是"多一条错误日志"：子 Run 进程挂在这一步，
        它的**真实结果**（含 S-1 补偿要用的撤销参数）从此丢失，
        而父 Run 那边显示的是"已取消"，看起来一切正常。

        ------------------------------------------------------------------
        为什么这里用 `_declare_terminal` 而不是 `run()`

        脸 A 的时间窗是"它已经跨过最后一个安全点、正在写终态"。
        真实世界里那一步在另一个进程里是一个事务：写终态 + append 事件（X-3），
        中间**没有**安全点 —— 所以它读不到那条意图。
        用 `run()` 会让它先在安全点读到意图并自己停下，那是另一条路（脸 C），
        不是赛跑。
        """
        parent, child_id, child_loop = self._cross_process()
        parent.cancel(reason="not needed", by="alice")

        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")      # 不许抛

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "completed")

    def test_the_outcome_keeps_the_trace_of_the_request(self) -> None:
        """D-16：它是在被叫停之后才跑完的 —— 这件事必须查得到。

        这是运维要看的**第一张清单**：这些子 Run 的副作用已经落进外部世界，
        而父 Run 早就把它们判成"不必再要了"。
        """
        parent, child_id, child_loop = self._cross_process()
        parent.cancel(reason="not needed", by="alice")
        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.finished_after_cancel_request)
        self.assertEqual(handle.cancel_requested_by, "alice")
        self.assertIn("not needed", handle.cancel_reason)

    def test_the_orphan_says_it_was_a_race(self) -> None:
        """D-16：账本那条孤儿必须点名"赛跑"。

        赛跑留下来的孤儿，责任方是**取消的人**；
        普通孤儿的责任方是**崩掉的那条 Run**。排障要找的人不一样，
        而账本理由里不写，运维只能靠猜（PR-19）。
        """
        parent, child_id, child_loop = self._cross_process()
        parent.cancel(reason="not needed", by="alice")
        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.PARENT_TERMINAL)

        unresolved = self.compensations.unresolved_for("run_parent")
        self.assertTrue(unresolved, "父已终态，这笔副作用必须有主人")
        self.assertTrue(
            any("D-16" in u.reason for u in unresolved),
            f"孤儿理由里没说这是赛跑：{[u.reason for u in unresolved]}",
        )

    def test_the_control_no_request_no_race_trace(self) -> None:
        """控制组：从没被叫停过的子 Run，`finished_after_cancel_request` 是假。

        判据不能空过 —— 若上面那条把"赛跑"写成了无条件前缀，这条就是红的。
        """
        _parent, child_id, child_loop = self._cross_process()
        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished)
        self.assertFalse(handle.is_cancel_requested)
        self.assertFalse(handle.finished_after_cancel_request)


# ---------------------------------------------------------------- 脸 B


class FaceBTheRequestArrivesTooLateTest(CrossProcessWorld):
    """子**先**跑完，父才叫停 —— 旧实现把那条没人看过的结果结掉了。"""

    def test_a_result_that_nobody_ever_saw_is_not_closed_out(self) -> None:
        """回归测试：旧实现在这一步照样 `mark_delivered()`。

        那等于把"已经产生、却从未被任何人看过"的结果记成已交付 ——
        唤醒路径再见它只会说 ALREADY_DELIVERED，D-13 孤儿永不登记，
        子 Run 留在外部世界的副作用**静默消失**。
        """
        parent, child_id, child_loop = self._spawned_with_child()

        child_loop.run()                  # 子先跑完（事件丢了，父还没被唤醒）
        before = self.registry.for_child(child_id)
        assert before is not None
        self.assertTrue(before.is_finished)

        parent.cancel(reason="not needed", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertEqual(handle.status, "completed")
        self.assertFalse(
            handle.is_delivered, "结果还没被任何人看过，不许被结掉（D-7）"
        )

    def test_the_sweep_still_finds_it_and_records_the_orphan(self) -> None:
        """A-12：事件丢了只是变慢 —— 兜底扫必须接住它并记一笔 D-13。"""
        parent, child_id, child_loop = self._spawned_with_child()
        child_loop.run()
        parent.cancel(reason="not needed", by="alice")

        self.waker.sweep()

        unresolved = self.compensations.unresolved_for("run_parent")
        self.assertTrue(unresolved, "这笔副作用必须有主人")
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_delivered, "记完孤儿就该结掉")

    def test_the_orphan_does_not_claim_a_race_it_did_not_run(self) -> None:
        """控制组：它是**跑完之后**才被叫停的，不是赛跑。

        D-15 的另一半：终态改不动，连"请求"都不该记 ——
        012 那两列回答的是"结局是不是发生在叫停**之后**"，
        这里的答案是"不是"。
        """
        parent, child_id, child_loop = self._spawned_with_child()
        child_loop.run()
        parent.cancel(reason="not needed", by="alice")
        self.waker.sweep()

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_cancel_requested)
        self.assertFalse(handle.finished_after_cancel_request)
        self.assertFalse(
            any(
                "D-16" in u.reason
                for u in self.compensations.unresolved_for("run_parent")
            )
        )

    def test_the_control_the_wake_does_not_record_it_twice(self) -> None:
        """控制组：D-7 —— 不可能交付两次，也就不该记两笔孤儿。"""
        parent, child_id, child_loop = self._spawned_with_child()
        child_loop.run()
        parent.cancel(reason="not needed", by="alice")

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.PARENT_TERMINAL)
        after_first = len(self.compensations.unresolved_for("run_parent"))

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(
            len(self.compensations.unresolved_for("run_parent")), after_first
        )


# ---------------------------------------------------------------- 进程内


class InProcessTheChildStopsItselfTest(CrossProcessWorld):
    """控制组：进程内那条路没被改坏 —— 它还是"它自己停的"。"""

    def test_the_child_writes_its_own_outcome(self) -> None:
        parent, child_id, _child = self._spawned_with_child()

        parent.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "cancelled")

    def test_it_is_closed_out_so_the_sweep_does_not_pick_it_up_forever(self) -> None:
        """B-9 第三半（重述后）：判据从"我取消过它"改成"**它**说自己停了"。

        不结的后果是一条慢慢长大的尾巴：兜底扫每一轮都会把它捞出来一次，
        每一轮记一条 D-13 孤儿。取消的次数越多 sweep 越慢。
        """
        parent, child_id, _child = self._spawned_with_child()
        parent.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_delivered)
        self.assertEqual(self.waker.sweep().total, 0)

    def test_d12_still_records_exactly_one_entry(self) -> None:
        """D-12 只记一条：进程内由父侧记，唤醒路径不再补第二条。"""
        parent, child_id, _child = self._spawned_with_child()
        parent.cancel(reason="because", by="alice")

        unresolved = self.compensations.unresolved_for("run_parent")
        self.assertEqual(len(unresolved), 1)
        self.assertIn("D-12", unresolved[0].reason)


# ---------------------------------------------------------------- request_cancel


class RequestCancelTest(unittest.TestCase):
    """`ChildRunRegistry.request_cancel`：三个"什么都不做"，理由各不相同。"""

    def setUp(self) -> None:
        self.registry = ChildRunRegistry()
        self.registry.bind(_handle())

    def test_it_records_the_request(self) -> None:
        handle = self.registry.request_cancel("child_1", reason="because", by="alice")
        self.assertTrue(handle.is_cancel_requested)
        self.assertIsNotNone(handle.cancel_requested_at)
        self.assertEqual(handle.cancel_requested_by, "alice")

    def test_it_does_not_write_a_terminal_status(self) -> None:
        """D-14 的核心：请求不是终态。"""
        handle = self.registry.request_cancel("child_1", reason="because", by="alice")
        self.assertFalse(handle.is_finished)
        self.assertEqual(handle.status, "created")

    def test_a_second_request_does_not_overwrite_the_first_reason(self) -> None:
        """第二次叫停通常来自**另一个人**。

        把原因改写成第二次那条，审计就回答不了"最初是谁叫的"（A-8）。
        """
        self.registry.request_cancel("child_1", reason="first", by="alice")
        second = self.registry.request_cancel("child_1", reason="second", by="bob")
        self.assertEqual(second.cancel_reason, "first")
        self.assertEqual(second.cancel_requested_by, "alice")

    def test_a_finished_child_run_keeps_its_outcome(self) -> None:
        """D-15：终态改不动。这是脸 B 的物理判据。"""
        self.registry.mark_finished("child_1", "completed", {"n": 1})
        handle = self.registry.request_cancel("child_1", reason="too late", by="alice")
        self.assertEqual(handle.status, "completed")
        self.assertFalse(handle.is_cancel_requested)

    def test_reason_is_required(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.request_cancel("child_1", reason="", by="alice")
        self.assertIn("B-8", str(cm.exception))

    def test_by_is_required(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.request_cancel("child_1", reason="because", by="")
        self.assertIn("B-8", str(cm.exception))

    def test_an_unregistered_child_run_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.request_cancel("nobody", reason="because", by="alice")
        self.assertIn("D-2", str(cm.exception))


class PostgresRequestCancelTest(unittest.TestCase):
    """同一批语义走**PG 适配器**（`012` 的表）。

    PR-23 的判据：换掉测试替身（内存 → PG）测试还得说真话。
    内存版可以靠"先读一下"，PG 版只能靠 rowcount —— 两条路各测一遍，
    才知道那条不变量是被**写**保证的，还是被**读**运气保证的。
    """

    SCHEMA = (
        "008_child_runs.sql",
        "010_child_run_result.sql",
        "012_child_run_cancel_request.sql",
        "015_child_wait_deadline.sql",
    )

    def setUp(self) -> None:
        from packages.agent_runtime.adapters.postgres import (
            PostgresChildRunRegistry,
        )

        self.conn = connect(schema_sql=load_schema_sql(*self.SCHEMA))
        self.addCleanup(self.conn.close)
        self.registry = PostgresChildRunRegistry(self.conn)
        self.registry.bind(_handle())

    def test_the_request_lands_in_pg(self) -> None:
        handle = self.registry.request_cancel("child_1", reason="because", by="alice")
        self.assertTrue(handle.is_cancel_requested)
        self.assertEqual(handle.cancel_requested_by, "alice")
        self.assertFalse(handle.is_finished)

        again = self.registry.for_child("child_1")
        assert again is not None
        self.assertTrue(again.is_cancel_requested, "换一个连接也要看得到")

    def test_a_second_request_does_not_overwrite_the_first(self) -> None:
        self.registry.request_cancel("child_1", reason="first", by="alice")
        second = self.registry.request_cancel("child_1", reason="second", by="bob")
        self.assertEqual(second.cancel_reason, "first")
        self.assertEqual(second.cancel_requested_by, "alice")

    def test_a_finished_child_run_keeps_its_outcome(self) -> None:
        self.registry.mark_finished("child_1", "completed", {"n": 1})
        handle = self.registry.request_cancel("child_1", reason="too late", by="alice")
        self.assertEqual(handle.status, "completed")
        self.assertFalse(handle.is_cancel_requested)

    def test_the_conditional_write_is_what_refuses_it_not_the_check(self) -> None:
        """D-15 的判据必须钉在**条件写**上，不是钉在 CHECK 上。

        故意让 CHECK 站不住：把 `completed_at` 设到未来，
        再把请求的时刻设到过去 —— `cancel_requested_at <= completed_at` 成立，
        `child_runs_cancel_before_outcome` 不会拦。
        这时还能拦住它的只有 `UPDATE ... WHERE completed_at IS NULL`。

        若这一条绿着只因为 CHECK 在拦，那它说的就不是"条件写"这件事，
        而 CHECK 只是兜底（PR-26）—— 兜底不能冒充主要保证。
        """
        future = datetime(2030, 1, 2, tzinfo=timezone.utc)
        past = datetime(2029, 1, 1, tzinfo=timezone.utc)
        self.registry.mark_finished(
            "child_1", "completed", {"n": 1}, completed_at=future
        )

        handle = self.registry.request_cancel(
            "child_1", reason="too late", by="alice", requested_at=past
        )

        self.assertEqual(handle.status, "completed")
        self.assertIsNone(handle.cancel_requested_at)

    def test_an_unregistered_child_run_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.request_cancel("nobody", reason="because", by="alice")
        self.assertIn("D-2", str(cm.exception))


# ---------------------------------------------------------------- 012 schema


class CancelRequestSchemaTest(unittest.TestCase):
    """`012_child_run_cancel_request.sql`：下推到 DB 的那两条约束。

    Python 侧已经守住了还要在 DB 再守一次的理由与 007 同款：
    DB 是**最后一个**能拦住它们的地方。
    """

    SCHEMA = (
        "008_child_runs.sql",
        "010_child_run_result.sql",
        "012_child_run_cancel_request.sql",
        "015_child_wait_deadline.sql",
    )

    def _conn(self):
        conn = connect(schema_sql=load_schema_sql(*self.SCHEMA))
        self.addCleanup(conn.close)
        return conn

    def _insert(self, cur, child_run_id: str = "child_1") -> None:
        #: `wait_until` 必须自己给：这句 INSERT 绕过了 `bind()`，
        #: 而 `015` 的 `child_runs_wait_deadline_required` 拒绝"没有等待上限的
        #: 派生" —— 那正是 D-18 本身（一次没有上限的派生 = 等到世界末日）。
        cur.execute(
            "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
            "parent_execution_id, parent_task_id, target, action, wait_until) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                child_run_id,
                "agent",
                "run_parent",
                f"exec_{child_run_id}",
                "task_1",
                "researcher",
                '{"action_type": "AGENT_DELEGATION"}',
                "2099-01-01 00:00:00.000000",
            ),
        )

    def test_b8_an_anonymous_cancellation_cannot_be_stored(self) -> None:
        conn = self._conn()
        cur = conn.cursor()
        for reason, by in (("", "alice"), ("because", "")):
            self._insert(cur)
            with self.assertRaises(Exception):
                cur.execute(
                    "UPDATE child_runs SET cancel_requested_at = %s, "
                    "cancel_reason = %s, cancel_requested_by = %s "
                    "WHERE child_run_id = %s",
                    (datetime.now(timezone.utc), reason, by, "child_1"),
                )
            cur.execute("DELETE FROM child_runs")

    def test_d17_a_request_cannot_come_after_the_outcome(self) -> None:
        """`cancel_requested_at <= completed_at`：不可能"先终态、后被叫停"。

        真正的保证是**条件写**（`UPDATE ... WHERE completed_at IS NULL`），
        这条 CHECK 是 PR-26 的兜底 —— 兜底不是主要保证，但它必须喊。
        """
        conn = self._conn()
        cur = conn.cursor()
        self._insert(cur)
        cur.execute(
            "UPDATE child_runs SET status = 'completed', completed_at = %s "
            "WHERE child_run_id = %s",
            ("2030-01-02 00:00:00", "child_1"),
        )
        with self.assertRaises(Exception):
            cur.execute(
                "UPDATE child_runs SET cancel_requested_at = %s, "
                "cancel_reason = 'because', cancel_requested_by = 'alice' "
                "WHERE child_run_id = %s",
                ("2030-01-03 00:00:00", "child_1"),
            )

    def test_the_control_a_request_before_the_outcome_is_allowed(self) -> None:
        """控制组：先叫停、后跑完 —— 这正是赛跑的合法形状，不许被拦。"""
        conn = self._conn()
        cur = conn.cursor()
        self._insert(cur)
        cur.execute(
            "UPDATE child_runs SET cancel_requested_at = %s, "
            "cancel_reason = 'because', cancel_requested_by = 'alice' "
            "WHERE child_run_id = %s",
            ("2030-01-01 00:00:00", "child_1"),
        )
        cur.execute(
            "UPDATE child_runs SET status = 'completed', completed_at = %s "
            "WHERE child_run_id = %s",
            ("2030-01-02 00:00:00", "child_1"),
        )
        cur.execute(
            "SELECT cancel_requested_at, completed_at FROM child_runs "
            "WHERE child_run_id = %s",
            ("child_1",),
        )
        row = cur.fetchone()
        assert row is not None
        self.assertIsNotNone(row["cancel_requested_at"])
        self.assertIsNotNone(row["completed_at"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
