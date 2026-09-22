"""真 PostgreSQL 上的**跨进程**取消（M34 / 空洞 222）。

--------------------------------------------------------------------------
为什么这一层不能只在内存里验

本轮四条判据全是 SQL 谓词，内存版一行都跑不到：

1. `request()` 的 upsert：

       ON CONFLICT (run_id) DO UPDATE SET ...
        WHERE run_cancellations.settled_at IS NULL

   最后那句 `WHERE` 是**不复活**：已经结掉的意图不能被第二次请求重新点亮
   （R-8 的存储侧）。写反了 / 漏了，内存版完全看不见 ——
   它的 `request()` 是 Python 的一行 `if`。

2. `pending()`：`WHERE settled_at IS NULL ORDER BY requested_at LIMIT %s`。
   Sweeper 的入口。谓词写反（比如写成 `IS NOT NULL`）内存版照样绿。

3. `settle()` 靠 `cur.rowcount` 判胜负，而不是"先读再写"。
   at-least-once 语义下"读出来判一下"永远慢一拍 —— 这条只有真库能证明
   它真的在判胜负（并发下两条 UPDATE 只有一条拿到 rowcount=1）。

4. 三条 CHECK：`run_cancellations_attributed`（B-8）、
   `run_cancellations_settled_after_request`（R-7）、以及 `run_id` 主键（幂等）。

--------------------------------------------------------------------------
"跨进程"在集成层怎么验

真起两个进程太重，而且验的东西其实是同一件：**那条意图是不是落在
两个栈都看得到的地方**。所以这里做的是：

    父栈在内存里 cancel  →  意图写进 PG
    子栈（另一个 stack 对象）下一次 step  →  从 PG 读到它 → 自己停

两边的 `cancellations` 是"同一个连接上的同一个 store"，
这正是生产里两个进程共享一张表的形状。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunCancellationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.cancellation import RunCancellationService
from packages.agent_runtime.delegation import InProcessChildRunSpawner
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
from tests.unit.test_delegation_compensation import _delegation

from ._pg import RealPostgresCase


class CrossProcessWorldPG:
    """父子**共享同一张 PG 表**的世界搭建。

    刻意**不是** `TestCase`：它是一个 mixin，好让别的测试文件复用这个世界
    而不把本文件的 11 条用例一并继承过去 —— 继承会让它们被跑两遍，
    那不是更严格，是计数失真。
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = PostgresChildRunRegistry(self.conn)
        self.snapshots = PostgresRunSnapshotStore(self.conn)
        self.compensations = PostgresCompensationStore(self.conn)
        # 空洞 222 的主角：父子**共享**同一张表，与生产两个进程同构
        self.cancellations = PostgresRunCancellationStore(self.conn)
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
            cancellations=self.cancellations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
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
            cancellations=self.cancellations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id="run_parent")
        self.assertIs(stack.loop.step(), StepOutcome.WAITING_CHILD)
        assert stack.loop.pending_child is not None
        return stack

    def _child_loop(self, stack: Any) -> Any:
        """另一个进程里那条子 Run 的 loop（只在抓 stack 时借用一下）。"""
        child_id = stack.loop.pending_child.child_run_id
        spawner = stack.loop.spawner
        return spawner._stacks[child_id].loop

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------ 通道


class CrossProcessCancellationOnRealPostgresTest(CrossProcessWorldPG, RealPostgresCase):
    def test_the_intent_lands_in_postgres(self) -> None:
        """R-7 在真库上：取消那一刻，库里真的躺着一条意图。"""
        stack = self._spawned()
        stack.loop.cancel(reason="user asked", by="alice")

        rows = self._rows(
            "SELECT run_id, reason, requested_by, requested_at, settled_at "
            "FROM run_cancellations WHERE run_id = %s",
            ("run_parent",),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "user asked")
        self.assertEqual(rows[0]["requested_by"], "alice")
        self.assertIsNotNone(rows[0]["settled_at"], "R-8：认领完了必须结掉")

    def test_the_cascade_intent_is_written_for_the_child(self) -> None:
        """空洞 222 的主断言：子 Run 也有一条**还没被认领**的意图。

        这条在 M33 时是空的：父 Run 只把登记处判死，
        没有任何东西告诉跑在另一个进程里的那条 Run。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id
        stack.loop.spawner._stacks.clear()     # 子 Run 在**别的进程**里

        stack.loop.cancel(reason="user asked", by="alice")

        rows = self._rows(
            "SELECT reason, requested_by, settled_at FROM run_cancellations "
            "WHERE run_id = %s",
            (child_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("user asked", rows[0]["reason"])
        self.assertEqual(rows[0]["requested_by"], "alice")
        self.assertIsNone(rows[0]["settled_at"], "还没人认领 —— 它自己会认领")

    def test_the_control_when_the_child_is_local_it_settles_at_once(self) -> None:
        """控制组：子 Run 就在本进程时，它自己当场结掉那条意图（R-8）。

        上面那条是"跨进程 ⟹ 留一条 pending"，这条是"进程内 ⟹ 立刻结掉"。
        两条一起才说得清 `settled_at` 到底在表达什么。
        """
        stack = self._spawned()
        child_id = stack.loop.pending_child.child_run_id

        stack.loop.cancel(reason="user asked", by="alice")

        rows = self._rows(
            "SELECT settled_at FROM run_cancellations WHERE run_id = %s",
            (child_id,),
        )
        self.assertIsNotNone(rows[0]["settled_at"])

    def test_the_child_adopts_it_from_postgres(self) -> None:
        """跨进程那条路：另一个"进程"的栈从 PG 读到它，然后自己停。"""
        stack = self._spawned()
        child_loop = self._child_loop(stack)
        child_id = stack.loop.pending_child.child_run_id

        # 抹掉进程内的 stack —— 父 Run 手里只剩 child_run_id
        stack.loop.spawner._stacks.clear()
        stack.loop.cancel(reason="user asked", by="alice")

        self.assertIs(child_loop.step(), StepOutcome.CANCELLED)
        assert child_loop.agent_run is not None
        self.assertIs(child_loop.agent_run.status, AgentRunStatus.CANCELLED)

        rows = self._rows(
            "SELECT settled_at FROM run_cancellations WHERE run_id = %s",
            (child_id,),
        )
        self.assertIsNotNone(rows[0]["settled_at"], "R-8：它自己结掉了")

    def test_the_pending_predicate_finds_only_unsettled_intents(self) -> None:
        """`pending()` 的 SQL 谓词第一次被真正执行。"""
        self.cancellations.request("run_a", reason="x", by="alice")
        self.cancellations.request("run_b", reason="y", by="bob")
        self.cancellations.settle("run_a")

        self.assertEqual([r.run_id for r in self.cancellations.pending()], ["run_b"])

    # ------------------------------------------------------------ 不复活

    def test_a_settled_intent_does_not_come_back_to_life(self) -> None:
        """upsert 的 `WHERE settled_at IS NULL`：结掉之后不许被重新点亮。

        重新点亮一条已结掉的意图，Sweeper 每轮都会撞一次 R-3 ——
        一个每轮都抛的后台进程，比一个不干活的更难发现。
        """
        self.cancellations.request("run_a", reason="first", by="alice")
        self.cancellations.settle("run_a")

        self.cancellations.request("run_a", reason="second", by="bob")

        rows = self._rows(
            "SELECT reason, requested_by, settled_at FROM run_cancellations "
            "WHERE run_id = %s",
            ("run_a",),
        )
        self.assertEqual(rows[0]["reason"], "first", "已结掉的意图不复活")
        self.assertIsNotNone(rows[0]["settled_at"])
        self.assertEqual(list(self.cancellations.pending()), [])

    def test_settle_reports_whether_it_won(self) -> None:
        """rowcount 判胜负：第二个 `settle()` 必须说 False。"""
        self.cancellations.request("run_a", reason="x", by="alice")

        self.assertTrue(self.cancellations.settle("run_a"))
        self.assertFalse(self.cancellations.settle("run_a"))

    # ------------------------------------------------------------ Sweeper

    def test_the_sweeper_adopts_a_suspended_run_from_postgres(self) -> None:
        """走不到安全点的那条，由 Sweeper 从 PG 里捞出来叫停。"""
        stack = self._spawned()
        assert stack.loop.agent_run is not None
        self.assertFalse(stack.loop.agent_run.is_terminal)   # 挂在 WAITING_CHILD

        self.cancellations.request("run_parent", reason="because", by="alice")
        service = RunCancellationService(
            store=self.cancellations,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            #: R-12：放弃等待要记账 —— 生产里这两个由 `build_run_cancellation_sweeper`
            #: 接上（`014` 之后缺了它们 `sweep()` 会抛，而不是静默跳过）
            child_registry=self.registry,
            saga=self.saga,
        )

        result = service.sweep()

        self.assertEqual(result.settled, ("run_parent",))
        rows = self._rows(
            "SELECT status FROM run_snapshots WHERE run_id = %s "
            "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
            ("run_parent",),
        )
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_r10_the_sweeper_does_not_settle_a_run_it_cannot_rebuild(self) -> None:
        """R-10 在真库上：没有快照 ≠ 已经停了。

        这里结掉它，等于在系统里写下"我取消了它"而它还在跑（PR-19），
        而且从此**再没有人**会去叫停它 —— 意图已经不在 pending 里了。
        """
        self.cancellations.request("run_ghost", reason="x", by="alice")
        service = RunCancellationService(
            store=self.cancellations,
            recovery=RunRecovery(
                snapshots=self.snapshots, factory=self._factory, approvals=None
            ),
            child_registry=self.registry,
            saga=self.saga,
        )

        self.assertEqual(service.sweep().settled, ())

        rows = self._rows(
            "SELECT settled_at FROM run_cancellations WHERE run_id = %s",
            ("run_ghost",),
        )
        self.assertIsNone(rows[0]["settled_at"], "还在 pending —— 等它自己的安全点")
        self.assertEqual(
            [r.run_id for r in self.cancellations.pending()], ["run_ghost"]
        )

    # ------------------------------------------------------------ DB 约束

    def test_b8_an_attributed_only_cancellation_cannot_be_stored(self) -> None:
        """B-8 在真库上：说不出是谁 / 说不出为什么的取消进不了这张表。"""
        for reason, by in (("", "alice"), ("because", "")):
            with self.assertRaises(Exception):
                self.cancellations.request("run_x", reason=reason, by=by)

    def test_one_run_has_at_most_one_request(self) -> None:
        """幂等：父 Run 取消了两次，不会变成两条意图、两次记账。"""
        self.cancellations.request("run_x", reason="first", by="alice")
        self.cancellations.request("run_x", reason="second", by="bob")

        rows = self._rows(
            "SELECT reason FROM run_cancellations WHERE run_id = %s", ("run_x",)
        )
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
