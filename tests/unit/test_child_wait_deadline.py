"""空洞 229：父 Run 挂在 `WAITING_CHILD` 上等一条死掉的子 Run（M38）。

--------------------------------------------------------------------------
这个洞为什么必须由**旁观者**来治

    AgentLoop.step()  一见 pending_child 就返回 WAITING_CHILD（D-5）
                        ↓
                      父 Run 挂起、落快照、不再做任何事

于是"我等了多久"没有任何人在算：父 Run 自己不会算（它不跑），
`ChildRunWaker.sweep()` 不算（它只扫 `completed_at IS NOT NULL`），
`undelivered()` 也不算（同一个谓词）。一条死掉的子 Run 让它的父 Run
**永远挂在 WAITING_CHILD 上**，界面显示"在等子 Agent"，一切正常。

--------------------------------------------------------------------------
这里的世界是**真跑起来的**

`WakeWorld`（`test_child_run_wake.py`）会真派生一条子 Run、真挂起、
真落快照。手搓 handle + 快照的测试会让"到期"退化成几个对象互相赋值，
而真跑一遍才会撞上闸门、快照、D-3 这些真正会出事的地方（PR-28）。
"""
from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.agent_domain.business.snapshot import state_from_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import (
    Action,
    ActionType,
    CompensationSpec,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker, ChildWakeOutcome
from packages.agent_runtime.child_wait import (
    ChildRunWaitExpirer,
    WaitExpiryOutcome,
    WaitExpirySweepResult,
)
from packages.agent_runtime.delegation import (
    DEFAULT_CHILD_WAIT_TIMEOUT,
    ChildRunRegistry,
    InProcessChildRunSpawner,
)
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.saga import SagaCoordinator
from packages.agent_domain.execution.retry import FailureClass

from .sqlite_shim import connect, load_schema_sql
from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)
from .test_child_run_wake import WakeWorld, _handle


# ---------------------------------------------------------------- 真跑的世界


def _compensable(run_id: str) -> Action:
    """委派动作 + 逆操作声明（S-1）。

    没有 `compensation` 的 Action 在账本上是**记不进去**的
    （`SagaCoordinator.record_unresolved` 返回 None —— 没有可记的账），
    于是"到期有没有记账"这条断言会假绿。
    """
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher"},
        compensation=CompensationSpec(
            tool="cancel_ticket",
            args={"ticket_id": "t-1"},
            result_keys=(),
            description="撤销子 Agent 建的那张工单",
        ),
    )


class WaitWorld(WakeWorld):
    """`WakeWorld` + 一个**等不到**的等待上限（1 毫秒）。

    上限设到几乎为零不是图省事：它让"到期"在测试里立刻成立，
    于是被测的是"到期之后发生了什么"，而不是"时钟走了多久"。

    为什么是 1 毫秒而不是 0（M43 / D-32 之后）：上限必须**晚于**派生，
    015 的 `CHECK (wait_until > spawned_at)` 与 `freeze_wait_deadline`
    都是这么判的 —— 一个在产生瞬间就已经逾期的派生，连一次被等的机会
    都没有。所以 0 不再是合法值，替身也得跟着改。
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = ChildRunRegistry(wait_timeout=timedelta(milliseconds=1))
        self.waker = ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )
        self.expirer = ChildRunWaitExpirer(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )

    def _parent(self, run_id: str = "run_parent") -> Any:
        """与父类同款，唯一区别是委派动作**带**逆操作声明（S-1）。"""
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_compensable(run_id)]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id=run_id)
        return stack.loop

    def _gone(self, run_id: str = "run_parent") -> tuple[Any, str]:
        """派生一条子 Run，然后把**那条子 Run 的进程**弄没。

        `InProcessChildRunSpawner` 把子 Run 的栈留在 `_stacks` 里；
        删掉它之后，这条子 Run 就变成了"在别的机器上、谁也碰不到"的那种 ——
        它永远不会产生终态，也不会有人替它写终态（D-14）。
        这正是空洞 229 的真实形状：不是"它失败了"，是**它没了**。
        """
        loop, child_id = self._spawned(run_id=run_id)
        assert loop.spawner is not None
        del loop.spawner._stacks[child_id]
        return loop, child_id

    def _now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=1)

    def _latest(self) -> Any:
        """父 Run 的**最新一份**快照 —— 从存储读，不走 `rebuild()`。

        D-27 之后到期器也会把父 Run 推走，推到终态后 R-3 就挡住了 `rebuild()`。
        "它现在停在哪个可恢复点上"该问存储：快照是事实，
        `rebuild()` 是"再来一次"的能力 —— 问事实不该被它挡住。
        """
        snapshot = self.snapshots.latest("run_parent")
        assert snapshot is not None, "父 Run 一份快照都没落"
        return snapshot

    def _observation_kinds(self) -> list[str]:
        """推进之后 State 里留下了哪些 Observation 的 kind。

        同样**不**走 `rebuild()`：父 Run 被推到终态之后恢复不出来，
        而"它相信过什么"已经写进快照里了（R-4：账本必须延续）。
        """
        return [
            obs.kind for obs in state_from_dict(self._latest().state).observations
        ]

    def _terminal_error(self, execution_id: str) -> Any:
        """父 Execution **最后**一次 Attempt 的 ErrorInfo —— 排障的第一入口。"""
        ex = self.kernel.repository.get(execution_id)
        assert ex is not None
        attempt = self.kernel.attempts.get(execution_id, ex.current_attempt_no)
        assert attempt is not None
        return attempt.error


class TheParentIsReleasedTest(WaitWorld):
    """那条真正的断言：父 Run 从"永远挂着"变成"可以被推一步"。"""

    def test_a_dead_child_leaves_its_parent_hanging_forever(self) -> None:
        """**控制组**：不治的话，父 Run 真的永远挂着，而且没人报错。

        这一条是整个文件的地基 —— 它先证明那个洞存在。
        """
        _loop, child_id = self._gone()

        rebuilt = self.recovery.rebuild("run_parent")
        self.assertIs(
            rebuilt.loop.step(),
            StepOutcome.WAITING_CHILD,
            "父 Run 还在等那条永远不会回来的子 Run",
        )
        # 而且没有任何一条队列看得到它
        self.assertEqual([h.child_run_id for h in self.registry.undelivered()], [])

    def test_the_overdue_queue_sees_what_nobody_else_sees(self) -> None:
        """D-18 的队首：只有 `overdue()` 看得到"等不到"的那些。"""
        _loop, child_id = self._gone()
        self.assertEqual([h.child_run_id for h in self.registry.overdue(self._now())],
                         [child_id])

    def test_the_wait_expires_and_the_parent_stops_waiting(self) -> None:
        _loop, child_id = self._gone()
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.EXPIRED)

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_wait_expired)
        self.assertIsNone(self._latest().pending_child_id, "父 Run 不该还在等")
        self.assertTrue(self._latest().is_terminal, "D-27：到期器也把父 Run 推走了")

    def test_the_parent_can_be_driven_on_after_the_expiry(self) -> None:
        """这一条才是买的东西。

        到期之前：重建出来的父 Run 一见 `pending_child` 就 `WAITING_CHILD`，
        **谁也推不动它**（控制台点"继续"也没用）。
        到期之后：闸门清了，它可以往前走。

        ------------------------------------------------------------------
        D-27 把它从"走得动"升级成"已经走了"

        判据从"重建出来再 step 一次不会 WAITING_CHILD"换成
        "最新那份快照已经不在闸门上、且已经推到终态"。
        前者证明的是**能力**，后者证明的是**事实** ——
        而空洞 217 恰恰是"有能力、没人去做"：
        父 Run 一直都走得动，只是跨进程部署里没有进程去推它。
        """
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        latest = self._latest()
        self.assertIsNone(latest.pending_child_id, "闸门已经清了")
        self.assertTrue(latest.is_terminal, "D-27：没人点'继续'，它自己走完了")

    def test_d8_a_new_snapshot_is_written_after_the_expiry(self) -> None:
        """交回（哪怕交回的是"什么都没有"）之后必须落一个新的可恢复点。

        不落的话，重建出来的父 Run 还是从挂起时那份快照装载，
        那份快照里写着 `pending_child_id` —— 白解开一次。
        """
        _loop, child_id = self._gone()
        before = len(self.snapshots.list_for("run_parent"))
        self.expirer.expire(child_id)
        self.assertGreater(len(self.snapshots.list_for("run_parent")), before)

    def test_the_expiry_is_idempotent(self) -> None:
        """重复扫 / 两个进程撞上：第二次什么都不做。"""
        _loop, child_id = self._gone()
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.EXPIRED)
        before = len(self.snapshots.list_for("run_parent"))
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.ALREADY_EXPIRED)
        self.assertEqual(len(self.snapshots.list_for("run_parent")), before)


class D19TheOutcomeIsUnknownNotFailedTest(WaitWorld):
    """"等不到结果"和"它失败了"是两件事（PR-19）。"""

    def test_the_ledger_says_we_do_not_know(self) -> None:
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        rows = list(self.compensations._by_id.values())  # type: ignore[attr-access]
        self.assertEqual(len(rows), 1)
        reason = rows[0].reason
        self.assertIn("WE DO NOT KNOW", reason)
        self.assertNotIn("ended failed", reason)
        self.assertNotIn("ended cancelled", reason)

    def test_the_execution_error_is_not_a_failure(self) -> None:
        """父 Execution 的终态 error 是排障的第一入口。

        写成 `CHILD_RUN_FAILED` 会让排障的人去找"子 Run 为什么失败"，
        而它可能压根没失败 —— 它可能正在某个 worker 上跑得好好的。
        """
        loop, child_id = self._gone()
        execution_id = loop.pending_child.parent_execution_id
        self.expirer.expire(child_id)

        error = self._terminal_error(execution_id)
        assert error is not None
        self.assertEqual(error.code, "CHILD_WAIT_EXPIRED")
        self.assertIs(error.failure_class, FailureClass.EXTERNAL_UNKNOWN)

    def test_the_observation_is_not_child_run_finished(self) -> None:
        """State 里不能留下"子 Run 已完成"这么一条不成立的事实。"""
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        kinds = self._observation_kinds()
        self.assertIn("child_run.unknown", kinds)
        self.assertNotIn("child_run.finished", kinds)

    def test_the_control_a_real_failure_still_says_failed(self) -> None:
        """控制组：`child_failed` 那一支没有被这次改动改写。"""
        _loop, child_id = self._gone()
        self.registry.mark_finished(child_id, "failed", {"summary": "boom"})
        self.waker.wake(child_id)  # type: ignore[attr-access]

        rows = list(self.compensations._by_id.values())  # type: ignore[attr-access]
        self.assertEqual(len(rows), 1)
        self.assertIn("ended failed", rows[0].reason)
        self.assertNotIn("WE DO NOT KNOW", rows[0].reason)


class D20TheExpiryIsNotATerminalStateTest(WaitWorld):
    """到期只说明"我们不再等了"，不说明那条子 Run 发生了什么。"""

    def test_the_handle_keeps_saying_it_has_no_result(self) -> None:
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished)
        self.assertFalse(handle.is_delivered)
        self.assertEqual(handle.status, "created")
        self.assertIsNone(handle.completed_at)
        self.assertIsNotNone(handle.wait_expired_at)

    def test_a_late_result_is_still_recognised(self) -> None:
        """它万一路回来了：唤醒路径照样认它（到期没把它从那条队列摘走）。"""
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        self.registry.mark_finished(child_id, "completed", {"n": 1})
        self.assertEqual(
            [h.child_run_id for h in self.registry.undelivered()], [child_id]
        )
        # 父 Run 已经不在等它了 —— 于是结果是"**迟到**"而不是"交付"，
        # 也不是"丢失"（D-22 / 空洞 231）。这里的 `LATE` 是 M39 给出的答案：
        # M38 冻结时这里写的是 `ALREADY_DELIVERED`，而那名字说"已经交过了"
        # —— 事实上从来没有人接过它。账本那一半见
        # `tests/unit/test_child_late_result.py`。
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.LATE)
        rows = list(self.compensations._by_id.values())  # type: ignore[attr-access]
        self.assertEqual(len(rows), 1, "迟到不得再造一条")

    def test_expiring_a_child_that_already_has_a_result_is_refused(self) -> None:
        """D-20：有结果就该走唤醒路径，走到期路径是把"有"当成"没有"。"""
        _loop, child_id = self._gone()
        self.registry.mark_finished(child_id, "completed", {"n": 1})
        with self.assertRaises(InvariantViolation) as cm:
            self.expirer.expire(child_id)
        self.assertIn("D-20", str(cm.exception))


class R13TheQueueIsNotBlockedForeverTest(WaitWorld):
    """过期过的必须退出队首 —— 与 M37 那一侧同一个形状。"""

    def test_two_overdue_children_and_a_limit_of_one(self) -> None:
        """两个父 Run 各等一条死掉的子 Run，一次只扫一条。

        队首被占住的后果在这里是**看得见**的：第二次扫到的是第二条，
        而不是又扫到第一条（那会让第二条永远等下去）。
        """
        _loop_a, child_a = self._gone(run_id="run_a")
        _loop_b, child_b = self._gone(run_id="run_b")

        first = self.expirer.sweep(self._now(), limit=1)
        second = self.expirer.sweep(self._now(), limit=1)
        self.assertEqual(first.expired, (child_a,))
        self.assertEqual(second.expired, (child_b,))
        self.assertEqual(self.expirer.sweep(self._now(), limit=1).total, 0)

    def test_the_control_without_letting_go_the_same_one_comes_back(self) -> None:
        """控制组：`mark_wait_expired` 才让队伍往前走，不是 `sweep` 自己。"""
        _loop, child_id = self._gone()
        # 绕过处置，只登记"我扫过它了"—— 什么都没做的情况下它必须还在队首
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.EXPIRED)
        self.assertEqual([h.child_run_id for h in self.registry.overdue(self._now())], [])
        # 而如果连登记都不做：
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.registry._store(replace(handle, wait_expired_at=None))
        self.assertEqual([h.child_run_id for h in self.registry.overdue(self._now())],
                         [child_id])


class TheParentIsAlreadyTerminalTest(WaitWorld):
    """父 Run 已经终态：结果永远无处可交，但副作用得有人记账（D-13）。"""

    def _terminal_parent(self) -> tuple[Any, str]:
        loop, child_id = self._gone()
        loop.cancel(reason="user asked", by="alice")
        return loop, child_id

    def test_the_orphan_is_booked(self) -> None:
        _loop, child_id = self._terminal_parent()
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.PARENT_TERMINAL)

        rows = list(self.compensations._by_id.values())  # type: ignore[attr-access]
        self.assertTrue(rows)
        self.assertIn("WE DO NOT KNOW", rows[-1].reason)
        self.assertIn(child_id, rows[-1].reason)

    def test_the_control_the_child_itself_is_untouched(self) -> None:
        """D-14 同款：没人替那条子 Run 写终态，连"到期"也不替它写。"""
        _loop, child_id = self._terminal_parent()
        self.expirer.expire(child_id)

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished)
        self.assertEqual(handle.status, "created")


class D18TheDeadlineIsFrozenAtBindTest(unittest.TestCase):
    """上限在**第一次登记**那一刻固化 —— 谁后来改都改不动它。"""

    def test_bind_freezes_the_deadline(self) -> None:
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1"))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, DEFAULT_CHILD_WAIT_TIMEOUT)

    def test_a_second_bind_does_not_move_the_deadline(self) -> None:
        """D-1 的另一半：第二条派生拿回的是**第一条**，上限也是第一条的。"""
        registry = ChildRunRegistry()
        first = registry.bind(_handle("child_1"))
        later = replace(_handle("child_2", parent_execution_id="exec_1"),
                        spawned_at=first.spawned_at + timedelta(hours=2))
        second = registry.bind(later)
        self.assertEqual(second.child_run_id, "child_1")
        self.assertEqual(second.wait_until, first.wait_until)

    def test_a_handle_that_brings_its_own_deadline_keeps_it(self) -> None:
        registry = ChildRunRegistry()
        # 曾经这里写的是 `datetime(2030, 1, 1)`：015 只要求上限晚于派生，
        # 于是"等到四年后"是合法的。016 加了平台上限（D-32）之后它不合法了 ——
        # 一次派生不许把父 Run 挂到明年，哪怕夹具里也不行。
        own = _handle("child_1").spawned_at + timedelta(hours=5)
        bound = registry.bind(replace(_handle("child_1"), wait_until=own))
        self.assertEqual(bound.wait_until, own)

    def test_the_timeout_is_a_knob_on_the_registry(self) -> None:
        registry = ChildRunRegistry(wait_timeout=timedelta(seconds=5))
        handle = registry.bind(_handle("child_1"))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(seconds=5))


class D18OverdueQueueTest(unittest.TestCase):
    """`overdue()` 的谓词：不是"派出去过"，是"等不到且还没处置"。"""

    def _registry(self, **kwargs: Any) -> ChildRunRegistry:
        registry = ChildRunRegistry(**kwargs)
        registry.bind(_handle("c_old", parent_execution_id="e1",
                              spawned_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        registry.bind(_handle("c_mid", parent_execution_id="e2",
                              spawned_at=datetime(2020, 1, 2, tzinfo=timezone.utc)))
        registry.bind(_handle("c_new", parent_execution_id="e3",
                              spawned_at=datetime(2020, 1, 3, tzinfo=timezone.utc)))
        return registry

    def test_ordering_is_by_the_deadline(self) -> None:
        registry = self._registry(wait_timeout=timedelta(milliseconds=1))
        now = datetime(2020, 1, 4, tzinfo=timezone.utc)
        self.assertEqual([h.child_run_id for h in registry.overdue(now)],
                         ["c_old", "c_mid", "c_new"])
        self.assertEqual([h.child_run_id for h in registry.overdue(now, limit=2)],
                         ["c_old", "c_mid"])

    def test_a_result_takes_it_out_of_this_queue(self) -> None:
        registry = self._registry(wait_timeout=timedelta(milliseconds=1))
        registry.mark_finished("c_old", "completed", {})
        now = datetime(2020, 1, 4, tzinfo=timezone.utc)
        self.assertEqual([h.child_run_id for h in registry.overdue(now)],
                         ["c_mid", "c_new"])
        # 但它**没有**离开唤醒队列
        self.assertEqual([h.child_run_id for h in registry.undelivered()], ["c_old"])

    def test_an_expired_wait_takes_it_out(self) -> None:
        registry = self._registry(wait_timeout=timedelta(milliseconds=1))
        registry.mark_wait_expired("c_old")
        now = datetime(2020, 1, 4, tzinfo=timezone.utc)
        self.assertEqual([h.child_run_id for h in registry.overdue(now)],
                         ["c_mid", "c_new"])

    def test_a_deadline_that_has_not_passed_is_not_overdue(self) -> None:
        registry = self._registry()
        # 默认上限 30 分钟：派生后一分钟没人会把它算成"等不到"
        now = datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc)
        self.assertEqual([h.child_run_id for h in registry.overdue(now)], [])

    def test_marking_twice_returns_false_the_second_time(self) -> None:
        registry = self._registry(wait_timeout=timedelta(milliseconds=1))
        self.assertTrue(registry.mark_wait_expired("c_old"))
        self.assertFalse(registry.mark_wait_expired("c_old"))

    def test_marking_a_child_that_has_a_result_is_refused(self) -> None:
        registry = self._registry(wait_timeout=timedelta(milliseconds=1))
        registry.mark_finished("c_old", "completed", {})
        with self.assertRaises(InvariantViolation) as cm:
            registry.mark_wait_expired("c_old")
        self.assertIn("D-20", str(cm.exception))


# ---------------------------------------------------------------- schema / PG


class ChildWaitSchemaTest(unittest.TestCase):
    """`015_child_wait_deadline.sql` 下推到 DB 的那几句。

    替身（sqlite）能验 CHECK，验不了"索引谓词真的被重建过"——
    那一半留给真 PostgreSQL（`tests/integration/`）。
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
        self.registry = PostgresChildRunRegistry(
            self.conn, wait_timeout=timedelta(minutes=5)
        )

    def _insert(self, child_run_id: str, spawned_at: str,
                wait_until: str | None = None) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
            "parent_execution_id, parent_task_id, target, action, spawned_at, "
            "wait_until) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                child_run_id,
                "agent",
                "run_parent",
                f"exec_{child_run_id}",
                "task_1",
                "researcher",
                '{"action_type": "AGENT_DELEGATION"}',
                spawned_at,
                wait_until,
            ),
        )

    def test_a_derivation_without_a_deadline_is_refused(self) -> None:
        """D-18 本身：没有等待上限的派生 = 等到世界末日的派生。"""
        from sqlite3 import IntegrityError

        with self.assertRaises(IntegrityError) as cm:
            self._insert("child_1", "2020-01-01 00:00:00.000000")
        self.assertIn("child_runs_wait_deadline_required", str(cm.exception))

    def test_a_deadline_before_the_spawn_is_refused(self) -> None:
        from sqlite3 import IntegrityError

        with self.assertRaises(IntegrityError) as cm:
            self._insert("child_2", "2020-01-01 00:00:00.000000",
                         "2019-01-01 00:00:00.000000")
        self.assertIn("child_runs_wait_deadline_after_spawn", str(cm.exception))

    def test_expiring_before_the_deadline_is_refused(self) -> None:
        from sqlite3 import IntegrityError

        self._insert("child_3", "2020-01-01 00:00:00.000000",
                     "2020-01-01 00:05:00.000000")
        cur = self.conn.cursor()
        with self.assertRaises(IntegrityError) as cm:
            cur.execute(
                "UPDATE child_runs SET wait_expired_at = %s WHERE child_run_id = %s",
                ("2020-01-01 00:01:00.000000", "child_3"),
            )
        self.assertIn("child_runs_wait_expired_after_deadline", str(cm.exception))

    def test_the_overdue_index_carries_the_predicate(self) -> None:
        """R-13 的落点：谓词写在索引里，不是写在 Python 的 `if` 里。"""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_child_runs_overdue'"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row, "015 必须建 idx_child_runs_overdue")
        text = str(row["sql"])
        self.assertIn("wait_expired_at IS NULL", text)
        self.assertIn("completed_at IS NULL", text)
        self.assertIn("delivered_at IS NULL", text)

    def test_the_pg_registry_freezes_the_deadline_on_bind(self) -> None:
        handle = self.registry.bind(_handle("child_9", parent_execution_id="exec_9"))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(minutes=5))

    def test_the_pg_overdue_queue(self) -> None:
        handle = self.registry.bind(_handle("child_9", parent_execution_id="exec_9"))
        assert handle.wait_until is not None
        before = handle.wait_until - timedelta(seconds=1)
        after = handle.wait_until + timedelta(seconds=1)
        self.assertEqual([h.child_run_id for h in self.registry.overdue(before)], [])
        self.assertEqual([h.child_run_id for h in self.registry.overdue(after)],
                         ["child_9"])

    def test_the_pg_mark_wait_expired_takes_it_out(self) -> None:
        #: 一个"等不了那么久"的登记处：上限 0，于是立刻就能处置。
        #: 用默认 5 分钟那个去 mark 会被 CHECK 挡住 ——
        #: 那不是测试的麻烦，那正是"先有约定，后有处置"这条判据本身。
        from packages.agent_runtime.adapters.postgres import (
            PostgresChildRunRegistry as Registry,
        )

        impatient = Registry(self.conn, wait_timeout=timedelta(seconds=1))
        impatient.bind(_handle("child_9", parent_execution_id="exec_9"))
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertTrue(impatient.mark_wait_expired("child_9", expired_at=later))
        self.assertFalse(impatient.mark_wait_expired("child_9", expired_at=later))
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual([h.child_run_id for h in self.registry.overdue(future)], [])

    def test_the_pg_mark_wait_expired_refuses_a_delivered_child(self) -> None:
        self.registry.bind(_handle("child_9", parent_execution_id="exec_9"))
        self.registry.mark_finished("child_9", "completed", {})
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.mark_wait_expired("child_9")
        self.assertIn("D-20", str(cm.exception))


class TheTimeoutLivesInOnePlaceTest(unittest.TestCase):
    """`015` 的回填与 `DEFAULT_CHILD_WAIT_TIMEOUT` 必须是同一个数。

    改一边忘另一边的后果：库里的历史派生按 30 分钟算、新派生按另一个数算，
    于是"等多久算等不到"有了两个答案（B-7）。
    """

    def test_the_backfill_matches_the_constant(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "infrastructure"
            / "postgres"
            / "015_child_wait_deadline.sql"
        )
        found = re.findall(r"interval '(\d+) (second|minute|hour|day)s?'",
                           path.read_text(encoding="utf-8"))
        self.assertEqual(len(found), 1, f"015 里应当只有一处 interval：{found}")
        n, unit = found[0]
        seconds = int(n) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]
        self.assertEqual(seconds, DEFAULT_CHILD_WAIT_TIMEOUT.total_seconds())

    def test_the_two_waits_are_two_knobs(self) -> None:
        """控制组：取消的等待上限（R-11）与结果的等待上限（D-18）**不是**一个数。"""
        from packages.agent_runtime.cancellation import DEFAULT_CANCELLATION_GRACE

        self.assertNotEqual(DEFAULT_CANCELLATION_GRACE, DEFAULT_CHILD_WAIT_TIMEOUT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
