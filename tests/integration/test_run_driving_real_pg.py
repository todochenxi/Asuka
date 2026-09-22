"""真 PostgreSQL 上的**推进**（M42 / 空洞 217）。

--------------------------------------------------------------------------
这一层要验什么（都是单测说不清的）

单测里"最新快照"是内存 list 的最后一个元素，`delivered_at` 是一个
dataclass 字段。真库上它们是 `run_snapshots` 的一行与 `child_runs` 的一列，
而 D-27 / D-29 买的正是"**另一个进程**读得到"这件事：

  1. D-28：终态快照是一行真的记录。它带着 009 的 `pending_child_id`
     与 JSONB 的 `state` —— `state->'observations'` 能用 SQL 查，
     于是"这条 Run 真的跑完了"能被一个**只读**的看板问出来，
     不必重建 Run（而终态 Run 本来就重建不出来，R-3）。
  2. D-29：崩在推进上时，`child_runs.delivered_at` 在 PG 里**是 NULL**。
     内存版里那只是一个字段没被赋值；
     真库上它是"下一个进程扫 `undelivered()` 还会不会看到它"的全部依据。
  3. D-30：把 `delivered_at` 用 **SQL** 改回 NULL（＝崩溃回滚发生在
     另一个进程里），父 Run 已 COMPLETED 的那条路径不许记孤儿。
  4. A-12：崩过一次之后换个进程接着扫 —— 它必须**补完**，不是报错。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker, ChildWakeOutcome
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.driving import DriveOutcome, InProcessRunDriver
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
from tests.unit.test_child_wait_deadline import _compensable

from ._pg import RealPostgresCase, real_pg

RUN = "run_parent"

LATEST_SNAPSHOT = (
    "SELECT status, pending_child_id, reason, "
    "       jsonb_array_length(state->'observations') AS n_obs "
    "  FROM run_snapshots WHERE run_id = %s "
    "  ORDER BY created_at DESC, snapshot_id DESC"
)


class _ExplodingDriver:
    """一步也推不动：模拟推进本身炸了（模型网关 500 / 进程被杀）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def drive(self, run_id: str) -> Any:
        self.calls.append(run_id)
        raise RuntimeError("model gateway is down")


class DriveOnRealPostgresTest(RealPostgresCase):
    """端到端：子 Run 跑完 → 结果回传 → 父 Run 被推进到终态（PG 全程在场）。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.outbox = PostgresOutboxStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn, events=self.outbox)
        self.kernel = ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=self.outbox,
            tasks=PostgresTaskRepository(self.conn),
            clock=ManualClock(),
        )

    # ------------------------------------------------------------ 装配

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

    def _recovery(self) -> RunRecovery:
        return RunRecovery(
            snapshots=self.snapshots, factory=self._factory, approvals=None
        )

    def _waker(self) -> ChildRunWaker:
        return ChildRunWaker(
            registry=self.registry,
            recovery=self._recovery(),
            saga=SagaCoordinator(store=self.compensations),
            driver=InProcessRunDriver(recovery=self._recovery()),
        )

    def _spawned(self) -> tuple[Any, str]:
        """父 Run 派生一条子 Run（脚本带逆操作声明 —— 否则"有没有记孤儿"假绿）。"""
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_compensable(RUN)]),
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
        stack.loop.start("go", run_id=RUN)
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        child_id = stack.loop.pending_child.child_run_id
        return stack, child_id

    def _latest(self, conn: Any) -> Any:
        row = conn.execute(LATEST_SNAPSHOT, (RUN,)).fetchone()
        assert row is not None, "父 Run 一份快照都没落"
        return row

    # ------------------------------------------------------------ D-28

    def test_the_terminal_snapshot_is_a_real_row_another_connection_reads(self) -> None:
        """"这个 Run 跑完了"必须是**一行记录**，不是某个进程的内存。

        看板 / 排障要问的是"它停在哪"，而终态 Run 重建不出来（R-3）——
        所以唯一能回答这个问题的就是这一行。走另一条连接读，
        是因为"同一个连接刚写过所以看得到"不算证据。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.DELIVERED)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = self._latest(other)
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["pending_child_id"], "009 那一列必须是空的")
        self.assertIn("stopped at", row["reason"], "reason 要说清停在哪")
        self.assertGreaterEqual(row["n_obs"], 1, "state 是 JSONB，查得出来")

    def test_the_state_jsonb_carries_the_run_finished_observation(self) -> None:
        """`state->'observations'` 真的能查 —— 下游按它做 RCA，不必重建 Run。

        sqlite 那一层根本没有 `->`，所以这条断言在那里**写不出来**。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        kinds = [
            r["kind"]
            for r in other.execute(
                "SELECT jsonb_array_elements(state->'observations') ->> 'kind' AS kind "
                "  FROM run_snapshots WHERE run_id = %s "
                "  ORDER BY created_at DESC, snapshot_id DESC",
                (RUN,),
            ).fetchall()
        ]
        self.assertIn("run.finished", kinds, "终态快照里必须有'跑完了'那一条")

    # ------------------------------------------------------------ D-29

    def test_a_crash_in_the_drive_leaves_delivered_at_null_in_postgres(self) -> None:
        """崩在推进上：`delivered_at` 在库里**是 NULL** —— 下一轮扫还会看到它。

        内存版里那只是"字段没被赋值"；真库上它是
        "下一个进程扫 `undelivered()` 还会不会看到它"的全部依据。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        waker = self._waker()
        waker.driver = _ExplodingDriver()

        with self.assertRaises(RuntimeError):
            waker.wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        row = other.execute(
            "SELECT delivered_at FROM child_runs WHERE child_run_id = %s", (child_id,)
        ).fetchone()
        assert row is not None
        self.assertIsNone(row["delivered_at"], "标记必须在推进之后才写")

    def test_another_process_picks_it_up_after_the_crash(self) -> None:
        """A-12：崩过一次之后换个进程接着扫 —— 它必须**补完**，不是报错。

        这一条是 D-29 的兑现：排对了顺序，崩溃的代价只是"再来一次"。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        broken = self._waker()
        broken.driver = _ExplodingDriver()
        with self.assertRaises(RuntimeError):
            broken.wake(child_id)

        # 另一个进程（另一条连接）用**好的**驱动方重扫。
        #
        # 它回来时 `pending_child` 已经清了（交付本身在崩之前就落库了 ——
        # D-29 只规定"标记"排在推进之后），所以它走的是"已经交过"那一支：
        # 推一把（D-27）、把 `delivered_at` 补上。这正是"补完"的形状。
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        second = ChildRunWaker(
            registry=PostgresChildRunRegistry(other),
            recovery=RunRecovery(
                snapshots=PostgresRunSnapshotStore(other),
                factory=self._factory_for(other),
                approvals=None,
            ),
            saga=SagaCoordinator(
                store=PostgresCompensationStore(other, events=PostgresOutboxStore(other))
            ),
            driver=InProcessRunDriver(recovery=self._recovery()),
        )
        self.assertIs(second.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(self._latest(other)["status"], "completed", "父 Run 被补完了")
        marked = other.execute(
            "SELECT delivered_at FROM child_runs WHERE child_run_id = %s", (child_id,)
        ).fetchone()
        assert marked is not None
        self.assertIsNotNone(marked["delivered_at"], "标记也补上了 —— 不用再扫第三遍")

    def _factory_for(self, conn: Any) -> Any:
        """另一条连接上的工厂：与 `self._factory` 同签名，只是换了个连接。"""
        snapshots = PostgresRunSnapshotStore(conn)
        compensations = PostgresCompensationStore(conn, events=PostgresOutboxStore(conn))

        def factory(agent_id: str, approvals: Any = None) -> Any:
            return assemble_runtime_stack(
                agent_id=agent_id,
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=ScriptedDecisionEngine([]),
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=self.kernel,
                snapshots=snapshots,
                compensations=compensations,
                spawner=InProcessChildRunSpawner(
                    factory=factory, approvals=approvals,
                    registry=PostgresChildRunRegistry(conn),
                ),
            )

        return factory

    # ------------------------------------------------------------ D-30

    def test_a_completed_parent_does_not_book_an_orphan(self) -> None:
        """把 `delivered_at` 用 **SQL** 改回 NULL（＝崩溃回滚在另一个进程里），
        再来一次不许记孤儿。

        S-16 已经在父 Run 完成那一刻把账本结案成 `not_needed`；
        此时再记一条 D-13 孤儿，账本上会同时写着"不需要撤销"与
        "没人负责这笔副作用" —— 两句互相矛盾的话。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.DELIVERED)

        # 模拟崩溃：`delivered_at` 被回滚了（用 SQL，等于另一个进程干的）
        self.conn.execute(
            "UPDATE child_runs SET delivered_at = NULL WHERE child_run_id = %s",
            (child_id,),
        )

        self.assertIs(self._waker().wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        rows = other.execute(
            "SELECT status, reason FROM compensations WHERE run_id = %s", (RUN,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "not_needed", "S-16：成功 = 按预期保留")
        self.assertNotIn("D-13", rows[0]["reason"] or "", "跑完的父 Run 不该留孤儿")

    # ------------------------------------------------------------ 幂等

    def test_a_second_process_driving_the_same_run_changes_nothing(self) -> None:
        """兜底扫每轮都会撞上同一批 —— 第二个进程进来必须是**空的**。

        判胜负靠 PG：终态快照只有一份，第二次 `drive()` 不新增行。
        """
        stack, child_id = self._spawned()
        child_stack = stack.loop.spawner.stack_for(child_id)
        child_stack.loop.run()
        self._waker().wake(child_id)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        before = other.execute(
            "SELECT count(*) AS n FROM run_snapshots WHERE run_id = %s", (RUN,)
        ).fetchone()["n"]

        driver = InProcessRunDriver(
            recovery=RunRecovery(
                snapshots=PostgresRunSnapshotStore(other),
                factory=self._factory_for(other),
                approvals=None,
            )
        )
        result = driver.drive(RUN)
        self.assertIs(result.outcome, DriveOutcome.TERMINAL)

        after = other.execute(
            "SELECT count(*) AS n FROM run_snapshots WHERE run_id = %s", (RUN,)
        ).fetchone()["n"]
        self.assertEqual(after, before, "第二次不许再多落一份快照")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
