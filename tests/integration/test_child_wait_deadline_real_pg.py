"""真 PostgreSQL 上的**等待上限**（M38 / 空洞 229）。

--------------------------------------------------------------------------
这一层要验的是什么

单测（`tests/unit/test_child_wait_deadline.py`）跑的是内存登记处 +
`sqlite_shim`。而三件事只有真库说得清：

  1. `idx_child_runs_overdue` 那个**部分索引的谓词**真的在 PG 里
     —— sqlite 恰好也支持部分索引，于是单测里那句"谓词存在"
     是**碰巧通过**（与 008 那批同一个陷阱）。而谓词是 R-13 的唯一落点。
  2. 三条 CHECK 由 PG 自己判定（`child_runs_wait_deadline_required` 等）。
  3. `TIMESTAMPTZ` 的回填与比较 —— `spawned_at + interval '30 minutes'`
     是 PG 方言，替身那句 `datetime()` 只是"看起来对"。

--------------------------------------------------------------------------
端到端那一条为什么也要在这里重跑

内存版里"登记处"和"快照"是同一个进程的两块内存。
真 PG 上它们落在不同的表里，而 D-20 要求"到期不写终态"这一句话
同时被 `child_runs` 与 `run_snapshots` 两边都承认 ——
这件事只有真库验得了。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.agent_domain.intelligence.action import (
    Action,
    ActionType,
    CompensationSpec,
)
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wait import (
    ChildRunWaitExpirer,
    WaitExpiryOutcome,
)
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.recovery import RunRecovery
from packages.agent_runtime.saga import SagaCoordinator
from packages.execution_kernel import ExecutionKernel, ManualClock
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from ._pg import MIGRATIONS_DIR, RealPostgresCase

CHECK_REQUIRED = "child_runs_wait_deadline_required"
CHECK_AFTER_SPAWN = "child_runs_wait_deadline_after_spawn"
CHECK_EXPIRED_AFTER = "child_runs_wait_expired_after_deadline"


def _015() -> str:
    return (MIGRATIONS_DIR / "015_child_wait_deadline.sql").read_text(encoding="utf-8")


def _compensable(run_id: str) -> Action:
    """S-1：带逆操作声明的委派 —— 没有它就**记不进账本**，
    于是"到期有没有记账"这条断言会假绿。"""
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


class ChildWaitDeadlineOnRealPostgresTest(RealPostgresCase):
    """`015_child_wait_deadline.sql` 在真 PG 上留下的东西。"""

    def _constraint_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'child_runs'::regclass"
        ).fetchall()
        return {r["conname"] for r in rows}

    def test_the_three_checks_are_in_real_postgres(self) -> None:
        names = self._constraint_names()
        for expected in (CHECK_REQUIRED, CHECK_AFTER_SPAWN, CHECK_EXPIRED_AFTER):
            self.assertIn(expected, names)

    def test_a_derivation_without_a_deadline_is_refused_by_postgres(self) -> None:
        """D-18 本身：没有等待上限的派生 = 等到世界末日的派生。"""
        with self.assertRaises(Exception) as cm:
            self.conn.execute(
                "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
                "parent_execution_id, parent_task_id, target, action) "
                "VALUES ('child_x', 'agent', 'run_1', 'exec_x', 'task_1', "
                "'researcher', '{\"action_type\": \"AGENT_DELEGATION\"}'::jsonb)"
            )
        self.assertIn(CHECK_REQUIRED, str(cm.exception))

    def test_the_overdue_index_predicate_is_in_real_postgres(self) -> None:
        """R-13 的落点：谓词写在**索引**里，不是写在 Python 的 `if` 里。

        只查源文件的文本是不够的 —— 漏了 `DROP INDEX` 那句，
        文件里照样有这段文本。所以这里读的是 PG 自己记的 `indexdef`。
        """
        rows = self.conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'child_runs' AND indexname = 'idx_child_runs_overdue'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "015 必须建 idx_child_runs_overdue")
        text = rows[0]["indexdef"]
        self.assertIn("wait_expired_at IS NULL", text)
        self.assertIn("completed_at IS NULL", text)
        self.assertIn("delivered_at IS NULL", text)

    def test_the_backfill_measures_from_spawned_at(self) -> None:
        """历史行的上限从**派生那一刻**起算，不是从迁移那一刻起算。

        后者会让"三天前派出、早该到期"的派生再白等 30 分钟，
        而它等的那条子 Run 多半连进程都没了。
        """
        self.conn.execute(
            "ALTER TABLE child_runs DROP COLUMN IF EXISTS wait_until CASCADE, "
            "DROP COLUMN IF EXISTS wait_expired_at CASCADE"
        )
        spawned = datetime.now(timezone.utc) - timedelta(days=3)
        self.conn.execute(
            "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
            "parent_execution_id, parent_task_id, target, action, spawned_at) "
            "VALUES ('child_old', 'agent', 'run_1', 'exec_old', 'task_1', "
            "'researcher', %s::jsonb, %s)",
            (_action_json(), spawned),
        )
        self.conn.execute(_015())

        row = self.conn.execute(
            "SELECT spawned_at, wait_until FROM child_runs "
            "WHERE child_run_id = 'child_old'"
        ).fetchone()
        assert row is not None
        delta = row["wait_until"] - row["spawned_at"]
        self.assertEqual(delta, timedelta(minutes=30))
        # 而且它立刻就在"等不到"那一队里 —— 三天前的派生早该到期了
        registry = PostgresChildRunRegistry(self.conn)
        now = datetime.now(timezone.utc)
        self.assertEqual([h.child_run_id for h in registry.overdue(now)], ["child_old"])

    def test_the_deadline_is_visible_to_another_connection(self) -> None:
        """跨进程：上限写在库里，不是写在派它出去的那个进程的内存里。"""
        from ._pg import real_pg

        registry = PostgresChildRunRegistry(self.conn, wait_timeout=timedelta(minutes=7))
        registry.bind(
            _handle("child_1", parent_execution_id="exec_1")
        )
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT wait_until, wait_expired_at FROM child_runs "
            "WHERE child_run_id = 'child_1'"
        ).fetchone()
        assert row is not None
        self.assertIsNotNone(row["wait_until"])
        self.assertIsNone(row["wait_expired_at"])


def _handle(child_run_id: str, **kwargs: Any) -> Any:
    from packages.agent_runtime.delegation import ChildRunHandle, ChildRunKind

    kwargs.setdefault(
        "action", Action(run_id="run_parent", action_type=ActionType.AGENT_DELEGATION)
    )
    return ChildRunHandle(
        child_run_id=child_run_id,
        kind=ChildRunKind.AGENT,
        parent_run_id="run_parent",
        parent_execution_id=kwargs.pop("parent_execution_id", "exec_1"),
        parent_task_id=kwargs.pop("parent_task_id", "task_1"),
        target="researcher",
        **kwargs,
    )


def _action_json() -> str:
    """真库上的 `action` 列存的是 `action_to_dict` 的产物。

    手写成 `{"action_type": "AGENT_DELEGATION"}` 是**错的**：
    枚举的值是 `agent_delegation`。这种行插得进去（CHECK 只管非空），
    读回来时才炸 —— 而那时报错说的是"枚举取值非法"，
    跟"我手写的 JSON 不对"差了十万八千里（PR-19）。
    """
    import json

    from packages.agent_domain.business.snapshot import action_to_dict

    return json.dumps(
        action_to_dict(
            Action(run_id="run_1", action_type=ActionType.AGENT_DELEGATION)
        )
    )


class ExpiredWaitOnRealPostgresTest(RealPostgresCase):
    """端到端：派生 → 子 Run 的进程没了 → 到期 → 父 Run 不再等。"""

    def setUp(self) -> None:
        super().setUp()
        #: 1 秒的上限：库里的那一行会在 1 秒后到期，
        #: 而测试用"未来的 now"去扫，于是**不需要真的睡 1 秒**。
        self.registry = PostgresChildRunRegistry(
            self.conn, wait_timeout=timedelta(seconds=1)
        )
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        #: 账本也是 PG 的 —— "孤儿有没有记下来"要能被**另一个连接**读出来，
        #: 否则这条断言只是在问一块内存。
        self.compensations = PostgresCompensationStore(self.conn)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
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

    def _expirer(self) -> ChildRunWaitExpirer:
        return ChildRunWaitExpirer(
            registry=self.registry,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
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

    def _spawned(self, run_id: str = "run_parent") -> tuple[Any, str]:
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
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        # 子 Run 的"进程"没了：它永远不会产生终态，也没人替它写终态（D-14）
        del stack.loop.spawner._stacks[child_id]
        return stack, child_id

    def _future(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=5)

    def test_a_dead_child_releases_its_parent(self) -> None:
        _stack, child_id = self._spawned()

        result = self._expirer().sweep(self._future())
        self.assertEqual(result.expired, (child_id,))

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_wait_expired)
        self.assertFalse(handle.is_finished, "D-20：到期不是终态")

        # 而且父 Run **已经往前走了**（D-27）—— 这是这一轮买的东西。
        #
        # 判据从"重建出来再 step 一次"换成"PG 里最新那一行的 status"：
        # 前者证明的是**能力**（它走得动），后者证明的是**事实**（它走了）。
        # 而空洞 217 恰恰是"有能力、没人去做"。
        from ._pg import real_pg

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT status, pending_child_id FROM run_snapshots "
            "  WHERE run_id = 'run_parent' "
            "  ORDER BY created_at DESC, snapshot_id DESC"
        ).fetchone()
        assert row is not None
        self.assertIsNone(row["pending_child_id"], "闸门已经清了")
        self.assertIn(row["status"], ("completed", "failed"), "D-27：已经推到终态")

    def test_the_expired_row_leaves_the_overdue_queue(self) -> None:
        """R-13 的同款：处置过就必须让出队首，否则新到期的一个也进不来。"""
        _stack, child_id = self._spawned()
        expirer = self._expirer()
        self.assertEqual(
            [h.child_run_id for h in self.registry.overdue(self._future())], [child_id]
        )
        expirer.sweep(self._future())
        self.assertEqual(
            [h.child_run_id for h in self.registry.overdue(self._future())], []
        )
        self.assertEqual(expirer.sweep(self._future()).total, 0)

    def test_the_ledger_row_is_in_real_postgres(self) -> None:
        """账本那一行要能被**另一条连接**原样读出来。"""
        from ._pg import real_pg

        _stack, child_id = self._spawned()
        self._expirer().sweep(self._future())

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        rows = other.execute(
            "SELECT status, reason FROM compensations WHERE run_id = 'run_parent'"
        ).fetchall()
        self.assertTrue(rows, "到期必须在账本上留一行")
        self.assertEqual(rows[0]["status"], "unresolved")
        self.assertIn("WE DO NOT KNOW", rows[0]["reason"])

    def test_a_terminal_parent_books_an_orphan_that_says_unknown(self) -> None:
        """父 Run 已终态：结果永远无处可交，但副作用得有人记账（D-13）。

        而且这一行必须写着"不知道"，不能写着"失败"。
        """
        stack, child_id = self._spawned()
        stack.loop.cancel(reason="user asked", by="alice")

        self.assertIs(
            self._expirer().expire(child_id, now=self._future()),
            WaitExpiryOutcome.PARENT_TERMINAL,
        )
        rows = self.conn.execute(
            "SELECT status, reason FROM compensations WHERE run_id = 'run_parent'"
        ).fetchall()
        self.assertTrue(rows)
        unresolved = [r for r in rows if r["status"] == "unresolved"]
        self.assertTrue(unresolved, f"没有 UNRESOLVED：{rows}")
        self.assertIn("WE DO NOT KNOW", unresolved[-1]["reason"])
        self.assertNotIn("ended failed", unresolved[-1]["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
