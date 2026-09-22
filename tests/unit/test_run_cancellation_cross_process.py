"""M34 / 空洞 222：跨进程下"子 Run 收到取消"的通道。

--------------------------------------------------------------------------
M33 结束时那个形状

父 Run 能把登记处那一行判成 `cancelled`，但跑在**另一个进程**里的那条 Run
没有任何人告诉它。于是两件事实同时成立，而它们说的是相反的东西：

    父侧：这条子 Run 的结果我不要了
    子侧：我还活着，我还在跑

用户按的是"停止"，得到的是**停止了一半**（B-9 只走通了进程内那一半）。

--------------------------------------------------------------------------
为什么 Execution 级的取消补不上这个洞

Kernel 有一套完整的取消（Durable Intent + Fast Signal + 安全点 Worker），
但它挂的是 **Execution**。一条子 Run 不是一条 Execution：

    · 它有自己**一堆** Execution（也要调模型、调工具、派生孙 Run）
    · 它在等孙 Run 的时候，**手上根本没有活的 Execution** ——
      那条因为闸门而 SUSPENDED 的 Execution 是"它在等"的证据，
      不是"它在跑"的证据。往它上面写取消信号 = 取消一次等待，
      等待结束之后它还会继续往下走。

所以必须有 **Run 级**这一层。

--------------------------------------------------------------------------
三段式（与 Kernel 同构）

    Durable Intent   `run_cancellations`（PG，唯一事实来源）
    Safe Point       `AgentLoop._step()` 顶部：协作式，读到就自己停
    Sweeper          `RunCancellationService.sweep()`：兜住所有走不到安全点的 Run

    本文件三条各测一组。

--------------------------------------------------------------------------
    R-7   取消**意图**必须先于取消**宣告**落库
    R-8   认领完了必须结掉，否则 Sweeper 一遍遍叫停同一条 Run
    R-9   已经终态不是失败，是**已经完成** —— 结掉它，不报错
    R-10  `settled_at` 是"确实停了"的证据，不是"请求过"的证据

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_runtime.cancellation import (
    InMemoryRunCancellationStore,
    RunCancellation,
    RunCancellationService,
)
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.saga import SagaCoordinator

from .sqlite_shim import connect, load_schema_sql
from .test_delegation_compensation import LedgerWorld, _delegation
from .test_run_cancellation import CancelWorld


class CrossProcessWorld(CancelWorld):
    """`CancelWorld` + 一个能拿到**子 Run loop** 的助手。

    跨进程在单测里只能靠"把 stack 从 spawner 里抹掉"来模拟，
    而"子 Run 那条 loop"必须在这之前抓在手上 ——
    否则没法验证它**后来自已读到了**那条意图。
    """

    def _spawned_with_child(self) -> tuple[Any, str, Any]:
        loop, child_id = self._spawned()
        self._loop = loop
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        stack = spawner._stacks[child_id]
        return loop, child_id, stack.loop


# ---------------------------------------------------------------- 安全点


class TheChildHearsAboutItTest(CrossProcessWorld):
    """空洞 222 的主断言：子 Run 自己读得到那条意图。"""

    def test_the_child_stops_itself_at_its_next_step(self) -> None:
        """没有人调它的 `cancel()`，它自己在安全点停下。"""
        _parent, child_id, child_loop = self._spawned_with_child()

        # 父 Run 在另一个进程里请求叫停它（真实路径是
        # `AgentLoop._cancel_pending_child` 写下的，这里直接写效果相同）
        self.cancellations.request(
            child_id, reason="parent run cancelled", by="alice"
        )

        outcome = child_loop.step()

        self.assertIs(outcome, StepOutcome.CANCELLED)
        assert child_loop.agent_run is not None
        self.assertIs(child_loop.agent_run.status, AgentRunStatus.CANCELLED)

    def test_the_control_without_the_intent_the_child_just_finishes(self) -> None:
        """控制组：没有意图时它照常跑完 —— 上面那条不是"它本来就会停"。"""
        _parent, _child_id, child_loop = self._spawned_with_child()

        outcome = child_loop.step()

        self.assertIs(outcome, StepOutcome.FINISHED)
        assert child_loop.agent_run is not None
        self.assertIs(child_loop.agent_run.status, AgentRunStatus.COMPLETED)

    def test_the_control_without_the_channel_the_child_never_hears(self) -> None:
        """控制组的控制组：没有通道，意图就无处可写。

        这条钉的是"通道"本身：把 `cancellations` 摘掉，
        父 Run 的取消就退回 M33 那个形状 —— 只有登记处被判死。
        """
        loop, child_id, child_loop = self._spawned_with_child()
        child_loop.cancellations = None
        self.cancellations.request(child_id, reason="x", by="alice")

        outcome = child_loop.step()

        self.assertIs(outcome, StepOutcome.FINISHED, "没有通道 = 没有安全点")

    def test_the_parents_cancellation_reaches_the_child(self) -> None:
        """端到端：父在 A 进程、子在 B 进程。"""
        loop, child_id, child_loop = self._spawned_with_child()
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        spawner._stacks.clear()                     # 子 Run 在别的进程

        loop.cancel(reason="user changed their mind", by="alice")

        request = self.cancellations.for_run(child_id)
        assert request is not None
        self.assertFalse(request.is_settled, "还没人认领 —— 它自己会认领")
        self.assertEqual(request.by, "alice")

        # B 进程下一次 step 读到它
        self.assertIs(child_loop.step(), StepOutcome.CANCELLED)
        self.assertTrue(self.cancellations.for_run(child_id).is_settled)

    def test_a_cancelled_run_stops_the_whole_drive(self) -> None:
        """`run()` 必须在 CANCELLED 上停 —— 否则终态之后还在推进。"""
        _parent, child_id, child_loop = self._spawned_with_child()
        self.cancellations.request(child_id, reason="x", by="alice")

        child_loop.run()

        assert child_loop.agent_run is not None
        self.assertIs(child_loop.agent_run.status, AgentRunStatus.CANCELLED)
        self.assertIs(child_loop.last_outcome, StepOutcome.CANCELLED)


# ---------------------------------------------------------------- R-7 / R-8


class R7IntentBeforeDeclarationTest(CrossProcessWorld):
    def test_the_intent_survives_a_crash_in_the_middle(self) -> None:
        """R-7：崩在"落意图"与"宣告终态"之间，意图必须还在。

        顺序反了的话，留下的残局是：Run 对外已经是 CANCELLED
        （快照、Trace、State 都写过了），而"有人要求停它"这件事
        从来没落过库 —— 重启后没人知道该停它。
        """
        loop, child_id, _child_loop = self._spawned_with_child()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the child's process is gone")

        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        spawner.cancel_child = boom               # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            loop.cancel(reason="because", by="alice")

        mine = self.cancellations.for_run("run_parent")
        assert mine is not None
        self.assertFalse(mine.is_settled)
        childs = self.cancellations.for_run(child_id)
        self.assertIsNotNone(childs, "子 Run 的意图也必须先落（级联版 R-7）")

        assert loop.agent_run is not None
        self.assertFalse(loop.agent_run.is_terminal, "没宣告成 —— 就该没宣告")

    def test_the_control_a_settled_intent_is_not_picked_up_twice(self) -> None:
        """R-8：取消完那一刻，pending 里就没有它了。

        不结的后果不是报错，是一个每轮都抛的后台进程：
        第一轮是真的取消，之后每一轮都撞 B-10。
        """
        loop, _child_id, _child_loop = self._spawned_with_child()

        loop.cancel(reason="because", by="alice")

        self.assertEqual(list(self.cancellations.pending()), [])
        mine = self.cancellations.for_run("run_parent")
        assert mine is not None
        self.assertTrue(mine.is_settled)


# ---------------------------------------------------------------- R-9 / R-10


class RunCancellationSweepTest(CrossProcessWorld):
    def _service(self) -> RunCancellationService:
        return RunCancellationService(
            store=self.cancellations,
            recovery=self.recovery,
            #: R-12：放弃等待要记账，记账要这两个。
            #: 不给它们，`sweep()` 在有到点意图时会**抛**而不是静默跳过。
            child_registry=self.registry,
            saga=SagaCoordinator(store=self.compensations),
        )

    def test_the_sweep_stops_a_run_that_cannot_reach_a_safe_point(self) -> None:
        """Sweeper 存在的理由：一条挂在 WAITING_CHILD 上的 Run 不会调 `step()`。"""
        loop, _child_id, _child_loop = self._spawned_with_child()
        loop.cancel(reason="because", by="alice")          # 先让父 Run 终态

        # 换个 id：造一条"挂起中、被请求取消、还没人认领"的 Run
        self.cancellations.request("run_parent", reason="again", by="bob")

        result = self._service().sweep()

        self.assertEqual(result.settled, ())
        # 已经终态 → R-9 结掉它，而不是抛
        mine = self.cancellations.for_run("run_parent")
        assert mine is not None
        self.assertTrue(mine.is_settled)

    def test_the_sweep_adopts_a_suspended_run(self) -> None:
        """主断言：一条挂起（有快照）的 Run 被 Sweeper 真的叫停。"""
        loop, child_id, _child_loop = self._spawned_with_child()
        assert loop.agent_run is not None
        self.assertFalse(loop.agent_run.is_terminal)       # 还挂在 WAITING_CHILD

        self.cancellations.request("run_parent", reason="because", by="alice")
        self.assertEqual(
            list(self.cancellations.pending())[0].run_id, "run_parent"
        )

        result = self._service().sweep()

        self.assertEqual(result.settled, ("run_parent",))
        rebuilt_snapshot = self.snapshots.latest("run_parent")
        assert rebuilt_snapshot is not None
        self.assertEqual(rebuilt_snapshot.status, AgentRunStatus.CANCELLED.value)

        # 父 Run 是被 Sweeper 叫停的；它自己那一条已经结掉。
        self.assertTrue(self.cancellations.for_run("run_parent").is_settled)
        # 而它**级联**给子 Run 的那一条还在 pending —— 这是对的：
        # 子 Run 正在另一个进程里跑（它没挂起，没有快照），
        # Sweeper 结不掉它（R-10），只能等它自己的安全点。
        remaining = [r.run_id for r in self.cancellations.pending()]
        self.assertEqual(remaining, [child_id])

    def test_r10_a_run_without_a_snapshot_is_not_settled(self) -> None:
        """R-10：重建不出来 ≠ 已经停了。

        `LookupError` 的意思只是"没有快照"，而快照只在挂起时拍（R-1）。
        一条正在往前跑的 Run 也没有快照 —— 它活得好好的。
        这里结掉它，等于写下"我取消了它"而它还在跑（PR-19），
        而且从此再没有人会去叫停它。
        """
        self.cancellations.request("run_ghost", reason="x", by="alice")

        result = self._service().sweep()

        self.assertEqual(result.settled, ())
        ghost = self.cancellations.for_run("run_ghost")
        assert ghost is not None
        self.assertFalse(ghost.is_settled, "还在 pending —— 等它自己的安全点")

    def test_a_sweep_without_a_recovery_says_so(self) -> None:
        """叫不动就说叫不动，不返回 0 假装扫过了。"""
        service = RunCancellationService(store=self.cancellations)
        with self.assertRaises(RuntimeError) as cm:
            service.sweep()
        self.assertIn("RunRecovery", str(cm.exception))


# ---------------------------------------------------------------- 意图本身


class RunCancellationIntentTest(unittest.TestCase):
    """`RunCancellation` 的构造期校验（B-8）。"""

    def test_reason_is_required(self) -> None:
        with self.assertRaises(ValueError) as cm:
            RunCancellation(run_id="r1", reason="", by="alice")
        self.assertIn("B-8", str(cm.exception))

    def test_by_is_required(self) -> None:
        with self.assertRaises(ValueError) as cm:
            RunCancellation(run_id="r1", reason="because", by="")
        self.assertIn("B-8", str(cm.exception))

    def test_a_settled_request_does_not_come_back_to_life(self) -> None:
        """R-8 的存储侧：结掉之后再请求，不该重新进入 pending。

        重新点亮一条已经结掉的意图，Sweeper 每轮都会撞一次 R-3。
        """
        store = InMemoryRunCancellationStore()
        store.request("r1", reason="because", by="alice")
        store.settle("r1")

        store.request("r1", reason="again", by="bob")

        self.assertEqual(list(store.pending()), [])


# ---------------------------------------------------------------- DB 那一层


class RunCancellationSchemaTest(unittest.TestCase):
    """`011_run_cancellations.sql`：下推到 DB 的那几条约束。

    为什么这些在 Python 侧已经守住了还要在 DB 再守一次：
    DB 是**最后一个**能拦住它们的地方（与 007 的 `value <> '{}'` 同款）。
    """

    def _conn(self):
        conn = connect(
            schema_sql=load_schema_sql("001_kernel.sql", "011_run_cancellations.sql")
        )
        self.addCleanup(conn.close)
        return conn

    def test_b8_an_anonymous_cancellation_cannot_be_stored(self) -> None:
        conn = self._conn()
        cur = conn.cursor()
        for reason, by in (("", "alice"), ("because", "")):
            with self.assertRaises(Exception):
                cur.execute(
                    "INSERT INTO run_cancellations (run_id, reason, requested_by) "
                    "VALUES (%s, %s, %s)",
                    ("r1", reason, by),
                )

    def test_r7_a_request_cannot_be_settled_before_it_was_made(self) -> None:
        """`settled_at >= requested_at`：一条意图不可能在被写下之前就被处理掉。"""
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO run_cancellations "
            "(run_id, reason, requested_by, requested_at) "
            "VALUES (%s, %s, %s, %s)",
            ("r1", "because", "alice", "2030-01-01 00:00:00"),
        )
        with self.assertRaises(Exception):
            cur.execute(
                "UPDATE run_cancellations SET settled_at = %s WHERE run_id = %s",
                ("2029-01-01 00:00:00", "r1"),
            )

    def test_one_run_has_at_most_one_request(self) -> None:
        """主键 = 幂等：父 Run 取消了两次，不会变成两条意图、两次记账。"""
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO run_cancellations (run_id, reason, requested_by) "
            "VALUES (%s, %s, %s)",
            ("r1", "first", "alice"),
        )
        with self.assertRaises(Exception):
            cur.execute(
                "INSERT INTO run_cancellations (run_id, reason, requested_by) "
                "VALUES (%s, %s, %s)",
                ("r1", "second", "bob"),
            )


# ---------------------------------------------------------------- 契约层


class ControlPlaneCrossProcessTest(unittest.TestCase):
    """`POST /runs/{id}/cancel` 对一条**不在本进程**的 Run。

    M33 那条路只能叫停装载得回来的 Run：`_must_stack()` 找不到就 404。
    而一条正在别的进程里跑的 Run **没有快照**（R-1：快照只在挂起时拍），
    于是控制台看得到它，点"叫停"却得到 RUN_NOT_FOUND。
    """

    def _cp(self, *, cancellations: Any):
        from packages.agent_api.service import InProcessControlPlane
        from packages.agent_runtime.recovery import InMemoryRunSnapshotStore

        return InProcessControlPlane(
            factory=lambda agent_id, approvals: None,   # type: ignore[return-value]
            snapshots=InMemoryRunSnapshotStore(),
            cancellations=cancellations,
        )

    def test_a_run_that_lives_elsewhere_gets_a_request_not_a_404(self) -> None:
        store = InMemoryRunCancellationStore()
        cp = self._cp(cancellations=store)

        view = cp.cancel_run("run_elsewhere", reason="because", by="alice")

        self.assertTrue(view.cancel_requested)
        request = store.for_run("run_elsewhere")
        assert request is not None
        self.assertEqual(request.reason, "because")
        self.assertEqual(request.by, "alice")

    def test_the_control_without_a_channel_it_is_still_404(self) -> None:
        """控制组：没有通道时，"不存在"与"在别处"**无法区分** —— 不猜。"""
        from packages.agent_api.errors import NotFound

        cp = self._cp(cancellations=None)
        with self.assertRaises(NotFound) as cm:
            cp.cancel_run("run_elsewhere", reason="because", by="alice")
        self.assertEqual(cm.exception.code, "RUN_NOT_FOUND")

    def test_the_request_is_not_reported_as_done(self) -> None:
        """PR-19：说"已请求"，不说"已取消" —— 那条 Run 还活着。"""
        store = InMemoryRunCancellationStore()
        cp = self._cp(cancellations=store)

        view = cp.cancel_run("run_elsewhere", reason="because", by="alice")

        self.assertNotEqual(view.status, AgentRunStatus.CANCELLED.value)
        self.assertFalse(store.for_run("run_elsewhere").is_settled)

    def test_the_view_can_be_serialised(self) -> None:
        """新字段必须进 `to_dict()` —— 否则页面上永远看不到它。"""
        store = InMemoryRunCancellationStore()
        cp = self._cp(cancellations=store)

        body = cp.cancel_run("run_elsewhere", reason="because", by="alice").to_dict()

        self.assertTrue(body["cancel_requested"])


__all__ = ["LedgerWorld", "_delegation", "InvariantViolation"]
