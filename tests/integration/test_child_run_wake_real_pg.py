"""真 PostgreSQL 上的子 Run 结果回传（M30 / 空洞 209）。

--------------------------------------------------------------------------
这一层存在的理由

`tests/unit/test_child_run_wake.py` 跑的是**内存**登记处 + `sqlite_shim`。
而 010 里那两条 CHECK、`WHERE completed_at IS NULL` 的 rowcount 判据、
`TIMESTAMPTZ` 往返、`result JSONB` 的结构化读回 —— 没有一样被真 PG 认可过。

尤其这一条：sqlite 恰好支持 `ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)`，
于是单测里的 CHECK 是"碰巧通过"（与 008 那批同一个陷阱）。
真 PG 认不认，只有这里说得清。

--------------------------------------------------------------------------
端到端那一条为什么也要在这里重跑一遍

内存版里"结果在登记处"和"事件在 outbox"是同一个进程的两块内存，
谁丢了都看得见。真 PG 上它们落在**不同的表**里，
而 X-3 要求它们同属一个事务 —— 这件事只有真库验得了。
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from typing import Any

from packages.agent_domain.ids import new_id
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker, ChildWakeOutcome
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunKind,
    InProcessChildRunSpawner,
)
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.recovery import RunRecovery
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator
from packages.execution_kernel import ExecutionKernel, ManualClock
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)

from tests.unit.test_delegation_compensation import _delegation

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import MIGRATIONS_DIR, RealPostgresCase, far_enough_deadline

CHECK_OUTCOME = "child_runs_outcome_consistent"
CHECK_DELIVERY = "child_runs_delivery_after_completion"

#: `wait_until` 必须给：015 的 `child_runs_wait_deadline_required` 拒绝
#: "没有等待上限的派生"（D-18）。裸 INSERT 绕过了 `bind()`。
INSERT_CHILD = """INSERT INTO child_runs (
    child_run_id, kind, parent_run_id, parent_execution_id, parent_task_id,
    target, action, wait_until
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""


def _row(child_run_id: str = "child_1", **over: Any) -> tuple:
    base = {
        "child_run_id": child_run_id,
        "kind": "agent",
        "parent_run_id": "run_1",
        "parent_execution_id": "exec_1",
        "parent_task_id": "task_1",
        "target": "researcher",
        "action": json.dumps({"action_type": "AGENT_DELEGATION"}),
        # 016：等待上限不得长于平台上限（D-32）—— 不再是 "等到 2099"。
        "wait_until": far_enough_deadline(),
    }
    base.update(over)
    return tuple(base[k] for k in (
        "child_run_id",
        "kind",
        "parent_run_id",
        "parent_execution_id",
        "parent_task_id",
        "target",
        "action",
        "wait_until",
    ))


class _FakeConsumer:
    """替身只顶掉 broker —— 数据库那一侧**不许**替。"""

    def __init__(self, events: Any = ()) -> None:
        self.events = list(events)
        self.committed = 0

    def subscribe(self, topics: Any) -> None:
        self.subscribed = list(topics)

    def poll(self, timeout: Any = None) -> Any:
        return list(self.events)

    def commit(self) -> None:
        self.committed += 1


class OutcomeConstraintsTest(RealPostgresCase):
    """010 那两条 CHECK 在真 PG 上真的生效（sqlite 是碰巧支持）。"""

    def _rejects(self, label: str, sql: str, params: tuple = ()) -> None:
        with self.assertRaises(Exception) as cm:
            self.conn.execute(sql, params)
        self.assertTrue(str(cm.exception), f"{label} 被拦下但没给原因")

    def test_a_terminal_status_without_a_completion_time_is_rejected(self) -> None:
        """D-6：说完了却说不出什么时候说完 → 那条记录是在说谎。"""
        self.conn.execute(INSERT_CHILD, _row())
        self._rejects(
            CHECK_OUTCOME,
            "UPDATE child_runs SET status='completed' WHERE child_run_id='child_1'",
        )

    def test_a_completion_time_on_a_non_terminal_row_is_rejected(self) -> None:
        """反方向：没说完却有说完的时刻。"""
        self.conn.execute(INSERT_CHILD, _row())
        self._rejects(
            CHECK_OUTCOME,
            "UPDATE child_runs SET completed_at=now() WHERE child_run_id='child_1'",
        )

    def test_delivery_without_a_result_is_rejected(self) -> None:
        """D-7：结果不可能在产生之前被交付。"""
        self.conn.execute(INSERT_CHILD, _row())
        self._rejects(
            CHECK_DELIVERY,
            "UPDATE child_runs SET delivered_at=now() WHERE child_run_id='child_1'",
        )

    def test_the_control_a_consistent_transition_is_accepted(self) -> None:
        """控制组：上三条不是因为这张表根本写不进去。"""
        self.conn.execute(INSERT_CHILD, _row())
        self.conn.execute(
            "UPDATE child_runs SET status='completed', completed_at=now(), "
            "result=%s::jsonb WHERE child_run_id='child_1'",
            (json.dumps({"status": "completed"}),),
        )
        self.conn.execute(
            "UPDATE child_runs SET delivered_at=now() WHERE child_run_id='child_1'"
        )
        row = self.conn.execute(
            "SELECT status, result, completed_at IS NOT NULL AS done, "
            "delivered_at IS NOT NULL AS sent FROM child_runs"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result"], {"status": "completed"})
        self.assertTrue(row["done"] and row["sent"])

    def test_the_control_the_migration_is_the_one_under_test(self) -> None:
        """控制组：010 真的加了这三列 —— 只看"跑通"会放过一份空迁移。"""
        cols = {
            r["column_name"]
            for r in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='child_runs'"
            ).fetchall()
        }
        for expected in ("result", "completed_at", "delivered_at"):
            self.assertIn(expected, cols)
        sql = (MIGRATIONS_DIR / "010_child_run_result.sql").read_text(encoding="utf-8")
        self.assertIn(CHECK_OUTCOME, sql)
        self.assertIn(CHECK_DELIVERY, sql)


class MarkFinishedOnRealPostgresTest(RealPostgresCase):
    """`WHERE completed_at IS NULL` + rowcount：真 PG 上的幂等判据。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)
        self.registry.bind(
            ChildRunHandle(
                child_run_id="child_1",
                kind=ChildRunKind.AGENT,
                parent_run_id="run_1",
                parent_execution_id="exec_1",
                parent_task_id="task_1",
                target="researcher",
                action=Action(
                    run_id="run_1", action_type=ActionType.AGENT_DELEGATION
                ),
            )
        )

    def test_a_repeated_completion_changes_nothing(self) -> None:
        first = self.registry.mark_finished("child_1", "completed", {"n": 1})
        second = self.registry.mark_finished("child_1", "completed", {"n": 999})
        self.assertEqual(second.completed_at, first.completed_at)
        self.assertEqual(dict(second.result), {"n": 1})

    def test_a_conflicting_terminal_status_is_refused(self) -> None:
        self.registry.mark_finished("child_1", "completed", {})
        with self.assertRaises(Exception) as cm:
            self.registry.mark_finished("child_1", "failed", {})
        self.assertIn("B-3", str(cm.exception))

    def test_delivery_happens_once_and_undelivered_sees_it(self) -> None:
        self.registry.mark_finished("child_1", "completed", {"n": 2})
        self.assertEqual(len(self.registry.undelivered()), 1)
        self.assertTrue(self.registry.mark_delivered("child_1"))
        self.assertFalse(self.registry.mark_delivered("child_1"))
        self.assertEqual(self.registry.undelivered(), [])

    def test_the_control_the_result_round_trips_as_structure(self) -> None:
        """`::jsonb` 读回来必须是**结构**而不是字符串 —— 唤醒路径直接用它。"""
        self.registry.mark_finished("child_1", "completed", {"n": 5, "s": "hi"})
        handle = self.registry.for_child("child_1")
        assert handle is not None
        self.assertIsInstance(handle.result, dict)
        self.assertEqual(handle.result["s"], "hi")

    def test_the_control_an_unfinished_child_is_not_undelivered(self) -> None:
        """控制组：兜底扫不会把还在跑的子 Run 当成"待交付"。"""
        self.assertEqual(self.registry.undelivered(), [])


class WakeOnRealPostgresTest(RealPostgresCase):
    """端到端：派生 → 子 Run 跑完 → 结果落库 → 唤醒 → 父 Run 继续。

    全程**不碰 Kafka**：事件那一段在单测里验过了，这里验的是
    "结果在 PG 里"这件事本身能不能支撑唤醒（A-12：Kafka 丢了只等于变慢）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.conn = self.conn
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = InMemoryCompensationStore()

    def _kernel(self) -> ExecutionKernel:
        return ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        """与 ControlPlane.factory 同签名。父子共用同一个连接上的三样存储。"""
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, approvals=approvals, registry=self.registry
            ),
        )

    def _parent(self, run_id: str = "run_parent") -> Any:
        """父 Run：脚本第一步就是一次委派。"""
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine(
                [
                    Action(
                        run_id=run_id,
                        action_type=ActionType.AGENT_DELEGATION,
                        payload={"agent_id": "researcher"},
                    )
                ]
            ),
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
        return stack

    def _waker(self) -> ChildRunWaker:
        return ChildRunWaker(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots,
                factory=self._factory,
                approvals=None,
            ),
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(
                recovery=RunRecovery(
                    snapshots=self.snapshots,
                    factory=self._factory,
                    approvals=None,
                ),
            ),
        )

    def test_the_whole_round_trip_on_real_postgres(self) -> None:
        """派得出去、结果落库、认得回来、父 Run 还能往前走。"""
        self.kernel = self._kernel()
        stack = self._parent()
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id

        # 子 Run 在"另一个进程"里跑完：这里用同一个内核，但结果只经 PG 传递
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished, "结果必须已经落在 child_runs 上")

        waker = self._waker()
        self.assertIs(waker.wake(child_id), ChildWakeOutcome.DELIVERED)

        # 父 Run 不是"还能往前走"，而是**已经走完**了（D-27）。
        #
        # 判据换到 PG 里那一行、且走**另一条连接**读：终态快照是一行真的
        # 数据库记录，不是这个进程的内存 —— 而这正是这一层存在的理由。
        from ._pg import real_pg

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT status, pending_child_id, reason FROM run_snapshots "
            "  WHERE run_id = 'run_parent' "
            "  ORDER BY created_at DESC, snapshot_id DESC"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["pending_child_id"])
        self.assertIn("stopped at", row["reason"], "快照的 reason 要说清停在哪（D-28）")

    def test_the_sweep_finds_it_without_any_event(self) -> None:
        """A-12：把事件那一条路整个拿掉，兜底扫照样叫得醒。"""
        self.kernel = self._kernel()
        stack = self._parent()
        stack.loop.step()
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner.stack_for(child_id).loop.run()

        waker = self._waker()
        self.assertEqual(waker.sweep().delivered, (child_id,))
        self.assertEqual(waker.sweep().total, 0, "第二次不该再交付")

    def test_the_composition_root_wires_it_to_postgres(self) -> None:
        """PR-29：适配器必须在**生产那条连接**上被验证。

        `build_child_run_consumer` 是真正会被部署的那条装配路径。
        只测 `ChildRunWaker` 本身，等于把"组合根有没有把 PG 版接进去"
        留在了没人看的地方 —— 那正是空洞 212~214 的形状。

        所以这里**不注入** registry / snapshots，只注入一个假 Kafka 消费者
        （真 broker 不在这一层的职责里），让组合根自己造那三样存储。
        """
        import os

        from apps._bootstrap import RuntimeConfig, build_child_run_consumer

        os.environ["AGENTOS_PG_DSN"] = __import__(
            "tests.integration._pg", fromlist=["dsn"]
        ).dsn()
        try:
            app = build_child_run_consumer(
                RuntimeConfig.from_env(),
                consumer=_FakeConsumer([]),
                factory=self._factory,
                conn=self.conn,
            )
        finally:
            os.environ.pop("AGENTOS_PG_DSN", None)

        from packages.agent_runtime.adapters.postgres import (
            PostgresChildRunRegistry,
            PostgresCompensationStore,
            PostgresRunSnapshotStore,
        )
        from packages.execution_kernel.adapters.postgres import (
            PostgresProcessedEventStore,
        )

        self.assertIsInstance(app.waker.registry, PostgresChildRunRegistry)
        self.assertIsInstance(app.waker.recovery.snapshots, PostgresRunSnapshotStore)
        self.assertIsInstance(app.processed, PostgresProcessedEventStore)
        # 三样必须共用**同一个连接** —— 否则交付的写与去重的写不在同一个事务里
        self.assertIs(app.waker.registry.conn, self.conn)
        self.assertIs(app.waker.recovery.snapshots.conn, self.conn)
        self.assertIs(app.processed.conn, self.conn)
        # D-13：唤醒路径的账本也必须是 PG 版、也在**这条**连接上。
        # 若是内存版，孤儿副作用（父 Run 已终态那一类）会随进程重启一起消失，
        # 而它记的恰恰是"外部世界有一笔东西没人管" —— 最容易丢、最不该丢。
        self.assertIsInstance(app.waker.saga.store, PostgresCompensationStore)
        self.assertIs(app.waker.saga.store.conn, self.conn)

    def test_the_result_survives_a_fresh_registry(self) -> None:
        """X-5：结果住在 PG 里，换一个登记处对象照样读得到。

        这是"结果不能只活在事件里"的可验证形式 ——
        内存登记处一换就没了，而 Kafka 有 retention。
        """
        self.kernel = self._kernel()
        stack = self._parent()
        stack.loop.step()
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner.stack_for(child_id).loop.run()

        fresh = PostgresChildRunRegistry(self.conn)
        handle = fresh.for_child(child_id)
        assert handle is not None
        self.assertEqual(handle.status, "completed")
        self.assertIn("steps", handle.result)


class DelegationLedgerOnRealPostgresTest(RealPostgresCase):
    """D-12 / D-13：委派没收成时，账本在**真 PG** 上也要落得住。

    ------------------------------------------------------------------
    为什么这一条必须到真 PG 上验

    `compensations.step_id` 是 `TEXT NOT NULL`。而 D-13 的孤儿记录
    **说不出**是哪一步（父 Run 已终态 ⟹ `rebuild()` 抛 R-3 ⟹ 拿不到 Step），
    于是它写的是**空字符串**。

    "空串"和 "NULL" 不是一回事：适配器若把 `""` 翻成 `NULL`，
    插入会直接失败 —— 而失败的那一刻，恰恰是"父 Run 已终态、
    这笔副作用最需要被记住"的那一刻。
    最该被记住的那一笔，因为一个字段被拒之门外。

    内存版 `InMemoryCompensationStore` 什么都不校验，这类错它永远看不见。
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn)
        self.saga = SagaCoordinator(store=self.compensations)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )
        self.decisions = ScriptedDecisionEngine([_delegation()])

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        engine = (
            self.decisions if agent_id == "parent" else ScriptedDecisionEngine([])
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=engine,
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
        )

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

    def _spawned(self) -> Any:
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=self.decisions,
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
        stack.loop.start("go", run_id="run_parent")
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        return stack

    def test_a_failed_delegation_lands_in_postgres_as_unresolved(self) -> None:
        """D-12 在真 PG 上：落得进去，而且说的是**那一步**。

        注意 D-12 与 D-13 的差别：委派失败时父 Run 还活着，
        所以 `step_id` 是**精确的**（S-1）；只有孤儿（D-13）才说不出是哪一步。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        step_id = self.snapshots.latest("run_parent").current_step_id
        self.registry.mark_finished(child_id, "failed", {"summary": "budget out"})
        self._waker().wake(child_id)

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, step_id, task_id, execution_id, reason "
                "FROM compensations WHERE run_id = %s",
                ("run_parent",),
            )
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["step_id"], step_id, "S-1：说得清是哪一步的副作用")
        self.assertTrue(row["task_id"], "task_id 必须精确（S-1）")
        self.assertIn("D-12", row["reason"])

    def _make_parent_terminal(self) -> None:
        """把父 Run 的最新快照置成终态（AgentOS 目前没有 Run 级取消入口，空洞 221）。

        真 PG 上不能像内存版那样 `save()` 同一条快照 —— `snapshot_id` 是主键，
        重插是 UniqueViolation。所以这里存的是**一条新的**快照：
        新的 `snapshot_id`，`created_at` 往后推一秒（`latest()` 按它排序），
        其余照抄。这仍然是"父 Run 已被判终态"这件事，不是绕过去。
        """
        snap = self.snapshots.latest("run_parent")
        assert snap is not None
        self.snapshots.save(
            replace(
                snap,
                snapshot_id=new_id("snap"),
                status="cancelled",
                created_at=snap.created_at + timedelta(seconds=1),
            )
        )

    def test_a_terminal_parent_orphan_lands_in_postgres(self) -> None:
        """D-13 在真 PG 上：孤儿记录落得进去，且不会被 `release()` 抹掉。"""
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        self.registry.mark_finished(child_id, "completed", {"summary": "done"})
        self._make_parent_terminal()

        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.PARENT_TERMINAL)
        self.assertEqual(len(self.compensations.unresolved_for("run_parent")), 1)

        self.saga.release("run_parent")
        self.assertEqual(
            len(self.compensations.unresolved_for("run_parent")),
            1,
            "S-16 只结 PENDING，孤儿必须留下",
        )

        # 这一整段的**起因**：`step_id` 是 NOT NULL，而孤儿只能写空串。
        # 适配器若把 `""` 翻成 NULL，插入会在"最该被记住的那一刻"失败。
        # 内存版什么都不校验 —— 这个错只有真库看得见。
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT step_id FROM compensations WHERE run_id = %s "
                "AND step_id IS NOT NULL AND step_id = ''",
                ("run_parent",),
            )
            self.assertEqual(len(cur.fetchall()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
