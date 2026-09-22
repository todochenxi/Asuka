"""M37 / 空洞 226：取消意图的**等待上限**（R-11 · R-12 · R-13 · R-14）。

--------------------------------------------------------------------------
这一轮修的不是"意图永远 pending"

R-10 在 M34 里已经把"一条永远没人认领的意图会一直留在 pending 里"
判成了"变慢且看得见，比变错且没人知道好（A-12）"，而且那个判断**没错**。

它漏的是另一件事：`pending()` 是

    WHERE settled_at IS NULL ORDER BY requested_at LIMIT %s

—— 它有 `ORDER BY`，也有 `LIMIT`。

一条僵尸意图的 `requested_at` 最老，于是它**永久占据队首**：
每轮被捞出来、撞 R-10、`continue`、下一轮再被捞出来。
攒够 `LIMIT` 条之后，新提交的取消请求**一条都进不了扫描窗口**，
而 `sweep()` 每轮返回 0 —— 界面上是"没有待处理的取消"。

那不是变慢，是**跨进程取消通道停止服务**，而且它什么都不喊。
本文件第一组就是这条的主断言 + 控制组。

--------------------------------------------------------------------------
为什么只能靠"上限"，不能靠"检测死亡"

系统里最接近活性证据的是 Kernel 的租约（`LEASE_EXPIRED`）——
但它在 Kernel 的语义里是**可重试**的：意思是"这个 worker 不续约了，
换一个 worker 接着来"，不是"这条 Run 没了"。
拿它当死亡证明会误杀一条正在被 Recovery 救活的 Run。

而一条 **Run** 除此之外没有任何活性证据：没有心跳列，
快照只在挂起时拍（R-1），正在往前跑的 Run 在 PG 里什么都不写。

所以：**没有死亡检测器，只有等待上限。**
前提是上限到期后的动作必须被诚实地记成"我们不知道"（R-11 / R-12）。

--------------------------------------------------------------------------
    R-11  每条意图都带等待上限；到点放弃。放弃 = "我们不知道"，
          不是"它停了"，所以**不写终态**、`settled_at`
    R-12  放弃一条子 Run 的等待时，若父已终态，必须补记一条 D-13 孤儿，
          理由必须点名"结局未知"
    R-13  放弃过的意图必须**退出 pending 队列** —— 否则让路根本没发生
    R-14  放弃**不是撤回**：那条 Run 后来撞上安全点仍应停下来

每条不变量配一个控制组。
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_runtime.adapters.postgres import PostgresRunCancellationStore
from packages.agent_runtime.cancellation import (
    DEFAULT_CANCELLATION_GRACE,
    InMemoryRunCancellationStore,
    RunCancellation,
    RunCancellationService,
)
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.saga import SagaCoordinator

from .sqlite_shim import connect, load_schema_sql
from .test_run_cancellation import CancelWorld


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =========================================================== R-13（主断言）


class R13TheQueueIsNotBlockedForeverTest(unittest.TestCase):
    """空洞 226 真正那一半：`LIMIT` 被永不结算的队首吃光。

    这一组只用 store，不牵扯 Run —— 因为被堵住的是**扫描窗口**，
    与那些 Run 是谁无关。塞不进来就是塞不进来。
    """

    def _store(self) -> InMemoryRunCancellationStore:
        return InMemoryRunCancellationStore(grace=timedelta(0))

    def test_the_control_three_zombies_hide_a_fresh_request(self) -> None:
        """控制组：**这就是 226 的形状**。

        三条僵尸排在队首，`LIMIT 3` 的窗口里没有那条新请求的位子。
        `sweep()` 会一轮一轮地把这三条捞出来又放回去，永远轮不到 `fresh`。
        """
        store = self._store()
        for i in range(3):
            store.request(f"zombie{i}", reason="x", by="alice")

        store.request("fresh", reason="user pressed stop", by="bob")

        window = [r.run_id for r in store.pending(3)]
        self.assertEqual(window, ["zombie0", "zombie1", "zombie2"])
        self.assertNotIn(
            "fresh", window, "新的取消请求被僵尸挤出了扫描窗口 —— 这就是那个洞"
        )

    def test_after_abandoning_the_zombies_the_fresh_one_comes_through(self) -> None:
        """R-13 的主断言：放弃过的退出队列，槽位立刻回到窗口里。

        `grace` 中途被调了一次，用来模拟"老请求早该放弃了、新请求还没到点" ——
        否则 `fresh` 自己也到期，测的就不是让路了。
        """
        store = self._store()
        for i in range(3):
            store.request(f"zombie{i}", reason="x", by="alice")
        store.grace = DEFAULT_CANCELLATION_GRACE       # 新请求还没到点
        store.request("fresh", reason="user pressed stop", by="bob")

        for i in range(3):
            self.assertTrue(store.abandon(f"zombie{i}"))

        self.assertEqual([r.run_id for r in store.pending(3)], ["fresh"])

    def test_a_zombie_would_be_picked_up_every_single_tick(self) -> None:
        """控制组：不放弃的话，同一条僵尸每一轮都在窗口里。

        这不是"偶发"，是**每轮**。所以攒够 `LIMIT` 条只是时间问题。
        """
        store = self._store()
        store.request("zombie", reason="x", by="alice")

        for _ in range(5):
            self.assertEqual([r.run_id for r in store.pending(64)], ["zombie"])

    def test_but_it_is_only_picked_up_once_after_being_abandoned(self) -> None:
        """放弃一次就够 —— 判胜负靠 rowcount，第二轮不再重复写。"""
        store = self._store()
        store.request("zombie", reason="x", by="alice")

        self.assertTrue(store.abandon("zombie"))
        self.assertFalse(store.abandon("zombie"), "放弃过的不该被放弃第二次")
        self.assertEqual(list(store.pending(64)), [])


# =========================================================== R-11


class R11EveryWaitHasADeadlineTest(unittest.TestCase):
    def test_a_request_freezes_its_deadline_at_write_time(self) -> None:
        """`abandon_after` 在 request 那一刻固化。

        事后把策略调长，**不该**追溯地改写一条已经提交的老意图 ——
        "这条请求当初承诺过多久"是审计的一部分。
        """
        store = InMemoryRunCancellationStore(grace=timedelta(minutes=5))
        request = store.request("r1", reason="x", by="alice")

        assert request.abandon_after is not None
        self.assertEqual(
            request.abandon_after - request.requested_at, timedelta(minutes=5)
        )

    def test_the_control_a_fresh_request_is_not_due_yet(self) -> None:
        """控制组：刚提交的不该被放弃 —— 否则"放弃"就成了"立刻放弃"。"""
        store = InMemoryRunCancellationStore(grace=DEFAULT_CANCELLATION_GRACE)
        request = store.request("r1", reason="x", by="alice")

        self.assertFalse(request.is_expired(_now()))
        self.assertEqual(list(store.expiring(_now())), [])

    def test_a_due_request_shows_up_in_expiring(self) -> None:
        store = InMemoryRunCancellationStore(grace=timedelta(0))
        store.request("r1", reason="x", by="alice")

        self.assertEqual([r.run_id for r in store.expiring(_now())], ["r1"])

    def test_the_control_a_settled_request_is_never_due(self) -> None:
        """控制组：已经停了的不是"等不到回音" —— 它已经有回音了。"""
        store = InMemoryRunCancellationStore(grace=timedelta(0))
        store.request("r1", reason="x", by="alice")
        store.settle("r1")

        self.assertEqual(list(store.expiring(_now())), [])

    def test_a_request_without_a_deadline_is_never_abandoned(self) -> None:
        """历史行（`abandon_after IS NULL`）**永不**被自动放弃。

        那不是放宽，是"我们没有资格替一条不知道承诺过多久的请求决定放弃"。
        """
        legacy = RunCancellation(run_id="r1", reason="x", by="alice")

        self.assertFalse(legacy.is_expired(_now()))

    def test_expiring_is_ordered_by_deadline_not_by_request_time(self) -> None:
        """`ORDER BY abandon_after`：最该被放弃的是最早**到点**的那条。

        两条都到期，但先请求的那条**更晚**到点 ——
        按 `requested_at` 排会先捞它，于是"最急的那条"反而排在后面。
        """
        now = _now()
        store = InMemoryRunCancellationStore(grace=DEFAULT_CANCELLATION_GRACE)
        store._by_run["requested_first"] = RunCancellation(
            run_id="requested_first",
            reason="x",
            by="alice",
            requested_at=now - timedelta(hours=2),
            abandon_after=now + timedelta(hours=1),      # 还没到点
        )
        store._by_run["requested_second"] = RunCancellation(
            run_id="requested_second",
            reason="x",
            by="bob",
            requested_at=now - timedelta(hours=1),
            abandon_after=now - timedelta(seconds=1),    # 早就到点
        )

        self.assertEqual(
            [r.run_id for r in store.expiring(now)], ["requested_second"]
        )


# =========================================================== R-12 / R-11 的行为


class GraceWorld(CancelWorld):
    """一个父子都真跑起来的世界，且取消意图**立刻到期**。

    `grace=0` 不是为了省事：它把"十五分钟后再来看"压缩成"现在"，
    于是测试说的仍是同一件事（到点了还没有回音）。
    """

    def setUp(self) -> None:
        super().setUp()
        # 必须在 `_spawned()` 之前换掉：`_factory` 是在被调用的那一刻
        # 才读 `self.cancellations` 的，所以这里换得掉。
        self.cancellations = InMemoryRunCancellationStore(grace=timedelta(0))

    def _service(self, **overrides: Any) -> RunCancellationService:
        kwargs: dict[str, Any] = dict(
            store=self.cancellations,
            recovery=self.recovery,
            child_registry=self.registry,
            saga=SagaCoordinator(store=self.compensations),
        )
        kwargs.update(overrides)
        return RunCancellationService(**kwargs)

    def _child_loop(self, child_id: str) -> Any:
        loop = getattr(self, "_loop", None)
        assert loop is not None
        return loop.spawner._stacks[child_id].loop

    def _spawned_and_the_child_process_is_gone(self) -> tuple[Any, str]:
        """父子都真跑起来，然后**子 Run 的进程彻底没了**。

        单测里"跨进程"只能这样模拟：把 stack 从 spawner 里抹掉。
        抹掉之后 `cancel_child` 走的是 D-14 那条路（只登记请求），
        而那条子 Run 再也没有人能推它到终态 ——
        正是空洞 226 的形状：意图永远 pending、孤儿永不登记。
        """
        loop, child_id = self._spawned()
        self._loop = loop
        del loop.spawner._stacks[child_id]
        return loop, child_id


class R12TheOrphanIsBookedTest(GraceWorld):
    def test_the_sweep_abandons_a_child_run_that_never_answers(self) -> None:
        """主断言：父已终态、子再没回音 ⟹ 放弃等待。"""
        loop, child_id = self._spawned_and_the_child_process_is_gone()

        loop.cancel(reason="user pressed stop", by="alice")   # 父终态，级联请求子

        result = self._service().sweep()

        self.assertEqual(result.abandoned, (child_id,))
        self.assertEqual(result.settled, ())

    def test_the_orphan_says_we_do_not_know(self) -> None:
        """R-12：理由必须点名"结局未知"，不能冒充"已取消"。

        这张账本是那条子 Run 唯一会留下来的痕迹。
        写成 `cancelled`，运维看到的是"已取消，无副作用" ——
        而它可能已经把工单建好了（PR-19）。
        """
        loop, child_id = self._spawned_and_the_child_process_is_gone()
        loop.cancel(reason="user pressed stop", by="alice")

        self._service().sweep()

        records = self._all()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIs(record.status, CompensationStatus.UNRESOLVED)
        self.assertIn("WE DO NOT KNOW", record.reason)
        self.assertIn("R-12", record.reason)
        self.assertIn(child_id, record.reason)
        self.assertNotIn("D-13", record.reason, "这不是'结果交不出去'，是'结局未知'")

    def test_the_control_without_the_ledger_the_sweeper_refuses_to_abandon(self) -> None:
        """控制组：没有账本就**不做**让路。

        一个把队列腾干净却把账本留空的进程，比一个堵住的进程更难发现 ——
        它看起来是健康的。所以这里是抛，不是静默跳过。
        """
        loop, _child_id = self._spawned_and_the_child_process_is_gone()
        loop.cancel(reason="user pressed stop", by="alice")

        with self.assertRaises(RuntimeError) as cm:
            self._service(saga=None).sweep()
        self.assertIn("R-12", str(cm.exception))

        with self.assertRaises(RuntimeError):
            self._service(child_registry=None).sweep()

    def test_the_control_a_root_run_is_abandoned_without_a_ledger_entry(self) -> None:
        """控制组：一条**根** Run（不是派生出来的）让路照做，只是没账可记。

        D-13 的落点是"父 Run 的补偿账本"，而根 Run 没有父 ——
        不能因为没地方记就不让路，否则它照样堵住队首。
        """
        self.cancellations.request("run_orphan_root", reason="x", by="alice")

        result = self._service().sweep()

        self.assertEqual(result.abandoned, ("run_orphan_root",))
        self.assertEqual(self._all(), [], "根 Run 没有父账本可记")

    def test_abandoning_does_not_write_a_terminal_state(self) -> None:
        """R-11：放弃**不是**宣告终态（D-14 / PR-19）。

        我们没有资格替一个看不见的对象作证。它停没停，我们真的不知道。
        """
        loop, child_id = self._spawned_and_the_child_process_is_gone()
        loop.cancel(reason="x", by="alice")

        self._service().sweep()

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished, "放弃不得替它写终态")
        self.assertEqual(handle.status, "created")

    def test_the_intent_is_abandoned_not_settled(self) -> None:
        """`settled_at` 空着 —— "它停了"这件事我们没看见（R-10）。"""
        loop, child_id = self._spawned_and_the_child_process_is_gone()
        loop.cancel(reason="x", by="alice")

        self._service().sweep()

        intent = self.cancellations.for_run(child_id)
        assert intent is not None
        self.assertTrue(intent.is_abandoned)
        self.assertFalse(intent.is_settled, "没看见它停，就不能说它停了")
        self.assertFalse(intent.is_pending, "R-13：它已经退出队列了")


# =========================================================== R-14


class R14AbandonIsNotAWithdrawalTest(GraceWorld):
    def test_the_run_still_stops_itself_after_we_gave_up_waiting(self) -> None:
        """R-14：放弃不是撤回 —— 那条 Run 后来撞上安全点仍应停下来。

        用户按的"停止"不因为我们等累了就作废。
        """
        loop, child_id = self._spawned()
        self._loop = loop

        self.cancellations.request(child_id, reason="stop it", by="alice")
        self._service().sweep()                       # 放弃等待

        outcome = self._child_loop(child_id).step()   # 它自己撞上安全点

        self.assertIs(outcome, StepOutcome.CANCELLED)
        child_run = self._child_loop(child_id).agent_run
        assert child_run is not None
        self.assertIs(child_run.status, AgentRunStatus.CANCELLED)

    def test_the_abandoned_fact_survives_the_run_later_stopping(self) -> None:
        """R-14：`settled_at` 与 `abandoned_at` 是两件独立的事。

        后来真的停了（settled）不把"我们放弃过"（abandoned）抹掉 ——
        抹掉就等于说"我们从来没等累过"，而那不是事实。
        """
        loop, child_id = self._spawned()
        self._loop = loop

        self.cancellations.request(child_id, reason="stop it", by="alice")
        self._service().sweep()
        self._child_loop(child_id).step()

        intent = self.cancellations.for_run(child_id)
        assert intent is not None
        self.assertTrue(intent.is_settled)
        self.assertTrue(intent.is_abandoned, "放弃过的事实不该被抹掉")


# =========================================================== DB 那一层


class CancellationGraceSchemaTest(unittest.TestCase):
    """`014_cancellation_grace.sql`：下推到 DB 的那几条约束。

    判据与 011 那组一致：DB 是**最后一个**能拦住它们的地方。
    断言一律点名约束名 —— 否则任何一句 SQL 语法错误都能让它变绿（PR-19）。
    """

    SCHEMA = (
        "001_kernel.sql",
        "011_run_cancellations.sql",
        "014_cancellation_grace.sql",
    )

    def _conn(self):
        conn = connect(schema_sql=load_schema_sql(*self.SCHEMA))
        self.addCleanup(conn.close)
        return conn

    def _insert(self, cur, run_id="r1", abandon_after=None, settled=None) -> None:
        cur.execute(
            "INSERT INTO run_cancellations "
            "(run_id, reason, requested_by, abandon_after, settled_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (run_id, "because", "alice", abandon_after, settled),
        )

    def test_r11_a_request_without_a_deadline_cannot_be_stored(self) -> None:
        conn = self._conn()
        cur = conn.cursor()
        with self.assertRaises(Exception) as cm:
            self._insert(cur, run_id="r1", abandon_after=None)
        self.assertIn("run_cancellations_deadline_required", str(cm.exception))

    def test_r11_abandoning_before_the_deadline_cannot_be_stored(self) -> None:
        conn = self._conn()
        cur = conn.cursor()
        self._insert(cur, run_id="r1", abandon_after="2030-01-01 00:00:00")
        with self.assertRaises(Exception) as cm:
            cur.execute(
                "UPDATE run_cancellations SET abandoned_at = %s WHERE run_id = %s",
                ("2029-01-01 00:00:00", "r1"),
            )
        self.assertIn("run_cancellations_abandon_after_deadline", str(cm.exception))

    def test_r13_the_pending_index_excludes_abandoned_rows(self) -> None:
        """R-13 唯一真正的落点是**索引的谓词**。

        少了 `abandoned_at IS NULL`，放弃过的意图照样留在部分索引里、
        照样排队首 —— 那两列就只是一份没人读的审计记录。
        """
        sql = load_schema_sql("014_cancellation_grace.sql")
        self.assertIn("AND abandoned_at IS NULL", sql)

    def test_the_adapter_writes_the_deadline(self) -> None:
        store = PostgresRunCancellationStore(self._conn())
        request = store.request("r1", reason="because", by="alice")

        assert request.abandon_after is not None
        self.assertGreater(request.abandon_after, request.requested_at)

    def test_the_adapter_pending_excludes_abandoned(self) -> None:
        """R-13 在真 SQL 上的样子（替身也过，因为谓词写在语句里）。

        两个不同宽限的 writer 共用一张表 —— 正好印证"上限在写入那一刻固化"：
        同一张表里，一条早就该放弃了，另一条还没到点。
        """
        conn = self._conn()
        impatient = PostgresRunCancellationStore(conn, grace=timedelta(0))
        patient = PostgresRunCancellationStore(conn, grace=DEFAULT_CANCELLATION_GRACE)

        impatient.request("zombie", reason="x", by="alice")
        self.assertEqual([r.run_id for r in patient.pending(64)], ["zombie"])
        self.assertTrue(impatient.abandon("zombie"))

        patient.request("fresh", reason="y", by="bob")
        self.assertEqual([r.run_id for r in patient.pending(64)], ["fresh"])
        # 控制组：让路不是"把队列清空" —— 新请求还在窗口里，而且还没到点。
        self.assertEqual(list(patient.expiring(_now())), [])

    def test_r11_an_abandoned_request_does_not_resurrect(self) -> None:
        """与 R-8 同一条判据：已经放弃的意图不该被第二次请求重新点亮。

        重新点亮会出现同一件事两个答案 ——
        账本说"我们不等了"，队列说"我们还在等"（B-7）。
        """
        store = PostgresRunCancellationStore(self._conn(), grace=timedelta(0))
        store.request("r1", reason="first", by="alice")
        store.abandon("r1")

        again = store.request("r1", reason="second", by="bob")

        self.assertEqual(again.reason, "first")
        self.assertTrue(again.is_abandoned)


# =========================================================== 一处定义


class TheGraceLivesInOnePlaceTest(unittest.TestCase):
    """`15 minutes` 在迁移里与 Python 里各出现一次 —— 这条盯着它们。

    两个数字分处 SQL 与 Python，天然是 B-7 的裂缝：
    改了一个忘了另一个，历史行与新行就会有两套宽限，
    而"这条请求当初承诺过多久"就会有两个答案。
    """

    _RE = re.compile(r"interval\s*'(\d+)\s*(second|minute|hour|day)s?'", re.IGNORECASE)

    def test_the_grace_in_sql_and_in_python_agree(self) -> None:
        sql = (
            Path(__file__).resolve().parents[2]
            / "infrastructure"
            / "postgres"
            / "014_cancellation_grace.sql"
        ).read_text(encoding="utf-8")

        matches = self._RE.findall(sql)
        self.assertEqual(len(matches), 1, f"迁移里应当只有一处宽限，实际 {matches}")

        n, unit = matches[0]
        factor = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit.lower()]
        self.assertEqual(int(n) * factor, DEFAULT_CANCELLATION_GRACE.total_seconds())
