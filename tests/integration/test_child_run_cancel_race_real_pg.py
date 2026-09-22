"""真 PostgreSQL 上的**取消与完成赛跑**（M35 / 空洞 224）。

--------------------------------------------------------------------------
为什么这一层不能只在内存里验

1. D-15 的判据是**条件写**：

       UPDATE child_runs SET cancel_requested_at = ...
        WHERE child_run_id = %s
          AND completed_at IS NULL          ← 这一行
          AND cancel_requested_at IS NULL

   内存版 `request_cancel` 靠 Python 的两个 `if`，它没有"条件写"这回事。
   所以"终态改不动"到底是被**写**保证的还是被**读**运气保证的，
   只有真库能回答。

2. `012` 的两条 CHECK（`child_runs_cancel_attributed` / B-8、
   `child_runs_cancel_before_outcome` / D-17）
   在替身（sqlite）上是同一条语句，但在真库上它们由 PG 求值 ——
   一条只在替身上成立的约束是假承诺（010 里就已经立过这条规矩）。

3. 赛跑的**结局**要落在 PG 里才能被另一个进程看见。
   内存版两个栈共享一个 dict，天生就"看得见"，
   而生产里真正会断的就是这一段。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.recovery import RunRecovery

from ._pg import RealPostgresCase, far_enough_deadline
from .test_run_cancellation_cross_process_real_pg import CrossProcessWorldPG


class CancelRaceOnRealPostgresTest(CrossProcessWorldPG, RealPostgresCase):
    def _child_row(self, child_run_id: str) -> dict[str, Any]:
        rows = self._rows(
            "SELECT status, completed_at, delivered_at, cancel_requested_at, "
            "cancel_reason, cancel_requested_by FROM child_runs "
            "WHERE child_run_id = %s",
            (child_run_id,),
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _insert_child(self, child_run_id: str = "child_1") -> None:
        self.conn.execute(
            "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
            "parent_execution_id, parent_task_id, target, action, wait_until) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
            (
                child_run_id,
                "agent",
                "run_parent",
                f"exec_{child_run_id}",
                "task_1",
                "researcher",
                '{"action_type": "AGENT_DELEGATION"}',
                far_enough_deadline(),
            ),
        )

    # ---------------------------------------------------------- D-14

    def test_the_request_lands_in_postgres_and_is_not_an_outcome(self) -> None:
        """D-14：跨进程叫停在真库上留下的是**请求**，不是终态。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner._stacks.clear()      # 子 Run 在别的进程里

        stack.loop.cancel(reason="user asked", by="alice")

        row = self._child_row(child_id)
        self.assertIsNotNone(row["cancel_requested_at"])
        self.assertEqual(row["cancel_requested_by"], "alice")
        self.assertIn("user asked", row["cancel_reason"])
        self.assertIsNone(row["completed_at"], "D-14：父不许替它写终态")
        self.assertNotEqual(row["status"], "cancelled")

    def test_another_connection_sees_the_request(self) -> None:
        """生产里"另一个进程"= 另一条连接。同一张表，不同的 store 对象。"""
        from packages.agent_runtime.adapters.postgres import (
            PostgresChildRunRegistry,
        )

        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner._stacks.clear()
        stack.loop.cancel(reason="user asked", by="alice")

        other = PostgresChildRunRegistry(self.conn)
        handle = other.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_cancel_requested)
        self.assertFalse(handle.is_finished)

    # ---------------------------------------------------------- D-15 / D-17

    def test_a_finished_child_run_keeps_its_outcome_in_postgres(self) -> None:
        """D-15：终态改不动。rowcount = 0 的那条路在真库上走得通。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner._stacks.clear()
        self.registry.mark_finished(child_id, "completed", {"n": 1})

        handle = self.registry.request_cancel(
            child_id, reason="too late", by="alice"
        )

        self.assertEqual(handle.status, "completed")
        self.assertFalse(handle.is_cancel_requested)

    def test_the_conditional_write_is_what_refuses_it_not_the_check(self) -> None:
        """与单测同款：让 CHECK 站不住（请求时刻早于终态时刻），
        还能拦住它的只有 `WHERE completed_at IS NULL`。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        self.registry.mark_finished(
            child_id,
            "completed",
            {"n": 1},
            completed_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
        )

        handle = self.registry.request_cancel(
            child_id,
            reason="too late",
            by="alice",
            requested_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
        )

        self.assertIsNone(handle.cancel_requested_at)
        self.assertEqual(handle.status, "completed")

    def test_b8_check_an_anonymous_request_is_refused(self) -> None:
        """B-8 在真库上：查不到归因的叫停进不了库。"""
        self._insert_child()
        for reason, by in (("", "alice"), ("because", "")):
            with self.assertRaises(Exception):
                self.conn.execute(
                    "UPDATE child_runs SET cancel_requested_at = %s, "
                    "cancel_reason = %s, cancel_requested_by = %s "
                    "WHERE child_run_id = %s",
                    (datetime.now(timezone.utc), reason, by, "child_1"),
                )

    def test_d17_check_a_request_cannot_come_after_the_outcome(self) -> None:
        """D-17 在真库上：不可能"先终态、后被叫停"。"""
        self._insert_child()
        self.conn.execute(
            "UPDATE child_runs SET status = 'completed', completed_at = %s "
            "WHERE child_run_id = %s",
            ("2030-01-02 00:00:00", "child_1"),
        )
        with self.assertRaises(Exception):
            self.conn.execute(
                "UPDATE child_runs SET cancel_requested_at = %s, "
                "cancel_reason = 'because', cancel_requested_by = 'alice' "
                "WHERE child_run_id = %s",
                ("2030-01-03 00:00:00", "child_1"),
            )

    def test_the_control_a_request_before_the_outcome_is_allowed(self) -> None:
        """控制组：先叫停后跑完是赛跑的合法形状，真库不许拦它。"""
        self._insert_child()
        self.conn.execute(
            "UPDATE child_runs SET cancel_requested_at = %s, "
            "cancel_reason = 'because', cancel_requested_by = 'alice' "
            "WHERE child_run_id = %s",
            ("2030-01-01 00:00:00", "child_1"),
        )
        self.conn.execute(
            "UPDATE child_runs SET status = 'completed', completed_at = %s "
            "WHERE child_run_id = %s",
            ("2030-01-02 00:00:00", "child_1"),
        )
        row = self._child_row("child_1")
        self.assertIsNotNone(row["cancel_requested_at"])
        self.assertIsNotNone(row["completed_at"])

    # ---------------------------------------------------------- 赛跑（端到端）

    def test_the_race_end_to_end_on_real_postgres(self) -> None:
        """空洞 224 的主断言，跑在真库上。

        父（本进程）叫停 → 请求落 PG → 子 Run 在"另一个进程"里、
        已经跨过最后一个安全点、正在写终态 —— 不许撞 B-3，
        而且结果必须带着"它是在被叫停之后才跑完的"这条痕迹。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        child_loop = self._child_loop(stack)
        stack.loop.spawner._stacks.clear()      # 跨进程

        stack.loop.cancel(reason="user asked", by="alice")

        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")   # 不许抛

        row = self._child_row(child_id)
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNotNone(row["cancel_requested_at"])
        self.assertLessEqual(row["cancel_requested_at"], row["completed_at"])

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.finished_after_cancel_request)

    def _waker(self) -> ChildRunWaker:
        return ChildRunWaker(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            saga=self.saga,

            driver=InProcessRunDriver(
                recovery=RunRecovery(
                    snapshots=self.snapshots,
                    factory=self._factory,
                    approvals=None,
                ),
            ),
        )

    def test_the_orphan_is_recorded_with_the_race_note(self) -> None:
        """D-16：唤醒路径在真库上把这笔副作用接进账本，并点名赛跑。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        child_loop = self._child_loop(stack)
        stack.loop.spawner._stacks.clear()
        stack.loop.cancel(reason="user asked", by="alice")
        child_loop._declare_terminal(AgentRunStatus.COMPLETED, reason="test")

        self._waker().wake(child_id)

        unresolved = self.compensations.unresolved_for("run_parent")
        self.assertTrue(unresolved, "父已终态，这笔副作用必须有主人")
        self.assertTrue(any("D-16" in u.reason for u in unresolved))

    def test_the_control_a_plain_completion_is_not_called_a_race(self) -> None:
        """控制组：没叫停过的子 Run，孤儿理由里**不许**出现 D-16。

        这是脸 B 在真库上的样子：子 Run 先跑完，父才取消 ——
        请求根本没写进去（D-15），所以它不是赛跑。

        判据不能空过 —— 若 D-16 成了无条件前缀，这条就是红的。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner._stacks.clear()
        self.registry.mark_finished(child_id, "completed", {"n": 1})
        stack.loop.cancel(reason="user asked", by="alice")

        self._waker().wake(child_id)

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.finished_after_cancel_request)
        self.assertFalse(
            any(
                "D-16" in u.reason
                for u in self.compensations.unresolved_for("run_parent")
            )
        )

    def test_an_unregistered_child_run_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.request_cancel("nobody", reason="because", by="alice")
        self.assertIn("D-2", str(cm.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
