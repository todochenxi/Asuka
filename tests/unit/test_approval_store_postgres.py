"""M19：审批存储持久化 —— 让 A-10 从"断言"变成"事实"。

    A-10  待审批列表查存储，不查内存 Loop（审批必须活过进程重启）
    A-11  判定与写入必须是原子操作（A-5 在并发下不成立）
    A-8   已决定的审批必须有 decided_by —— 在 DB 层兜底
    H-1 / H-5  审批必须有 reason / 必须有正 TTL —— 在 DB 层兜底

测试跑在 sqlite 上的 PG 方言替身（`sqlite_shim.py`），但 schema 直接读
`infrastructure/postgres/003_approvals.sql` 原文 —— 测的是真 schema 的语义。
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta

from packages.agent_api import (
    InProcessControlPlane,
    list_approvals,
)
from packages.agent_domain.errors import ConcurrentStateError
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_harness.approval import (
    ApprovalStatus,
    HumanLoop,
    InMemoryApprovalStore,
)
from packages.agent_harness.adapters.postgres import PostgresApprovalStore
from packages.execution_kernel.inmemory import ManualClock

from .sqlite_shim import connect, load_schema_sql

# ---------------------------------------------------------------- 工具
NOW = ManualClock().now()


def _action(run_id: str = "run_1", expr: str = "6*7") -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "calculator", "args": {"expr": expr}},
        risk_level=RiskLevel.HIGH,
        rationale="scripted high risk",
    )


def _open_store():
    """一个"数据库"。真实生产里是 PG 连接池；这里是同一套 SQL 的 sqlite 替身。"""
    conn = connect(schema_sql=load_schema_sql("003_approvals.sql"))
    return conn, PostgresApprovalStore(conn)


def _human_loop(store, clock: ManualClock | None = None) -> HumanLoop:
    return HumanLoop(store=store, clock=clock or ManualClock())


def _raw_insert(conn, **overrides) -> None:
    """绕过领域层直接写表 —— 用来验证 **DB 约束**真的会拦。

    领域里的检查是"善意"，DB 约束才是"兜底"。所以这里故意走后门。
    """
    values = {
        "approval_id": "apr_raw",
        "run_id": "run_1",
        "execution_id": None,
        "status": "pending",
        "reason": "because",
        "action": '{"action_type": "tool_call", "run_id": "run_1"}',
        "requested_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
        "decided_by": None,
        "decided_at": None,
        "comment": "",
    }
    values.update(overrides)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO approvals (
            approval_id, run_id, execution_id, status, reason, action,
            requested_at, expires_at, decided_by, decided_at, comment
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        tuple(values[k] for k in (
            "approval_id", "run_id", "execution_id", "status", "reason", "action",
            "requested_at", "expires_at", "decided_by", "decided_at", "comment",
        )),
    )


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.store = _open_store()

    def tearDown(self) -> None:
        self.conn.close()

    def test_schema_is_loadable(self) -> None:
        self.assertEqual(list(self.store.pending()), [])

    def test_roundtrip_keeps_the_action_intact(self) -> None:
        """人要看的是"我放行的到底是什么" —— 存丢了这个就没意义了。"""
        loop = _human_loop(self.store)
        req = loop.request(_action(expr="6*7"), reason="high risk tool call")

        loaded = self.store.get(req.approval_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.status, ApprovalStatus.PENDING)
        self.assertEqual(loaded.reason, "high risk tool call")
        self.assertEqual(loaded.action.action_type, ActionType.TOOL_CALL)
        self.assertEqual(loaded.action.risk_level, RiskLevel.HIGH)
        self.assertEqual(loaded.action.payload["args"]["expr"], "6*7")

    def test_execution_binding_survives(self) -> None:
        """H-6：审批 ↔ 挂起的 Execution 双向可追溯，这条链不能因为落库就断。"""
        loop = _human_loop(self.store)
        req = loop.request(_action(), reason="r")
        loop.bind(req.approval_id, "exec_42")
        self.assertEqual(self.store.get(req.approval_id).execution_id, "exec_42")

    def test_pending_is_scoped_by_run(self) -> None:
        loop = _human_loop(self.store)
        loop.request(_action(run_id="run_a"), reason="r")
        loop.request(_action(run_id="run_b"), reason="r")
        self.assertEqual(len(self.store.pending("run_a")), 1)
        self.assertEqual(len(self.store.pending("run_b")), 1)
        self.assertEqual(len(self.store.pending()), 2)


# ---------------------------------------------------------------- A-10
class RestartSurvivalTest(unittest.TestCase):
    """审批必须活过"重启" —— 这是本轮全部工作的理由。"""

    def setUp(self) -> None:
        self.conn, self.store = _open_store()

    def tearDown(self) -> None:
        self.conn.close()

    def test_a10_approvals_survive_a_restart_of_the_control_plane(self) -> None:
        """换一个全新的 ControlPlane（内存里的 Run 全没了），列表里还得有东西。"""
        first = InProcessControlPlane(factory=_noop_factory, approvals=self.store)
        _human_loop(self.store).request(_action(run_id="run_1"), reason="needs a human")

        # ── 重启：内存里的 stacks 全丢，只有 PG 还在 ──
        second = InProcessControlPlane(factory=_noop_factory, approvals=self.store)

        resp = list_approvals(second)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body["items"]), 1, resp.body["items"])
        self.assertEqual(resp.body["items"][0]["run_id"], "run_1")

    def test_a10_in_memory_store_does_not_survive(self) -> None:
        """对照：换成内存存储，同一个"重启"就什么都没有了。

        这条存在的意义是证明上面那条**不是白测的** ——
        如果内存版也能过，说明测试根本没碰到 A-10。
        """
        first = InProcessControlPlane(factory=_noop_factory, approvals=InMemoryApprovalStore())
        _human_loop(first.approvals).request(_action(run_id="run_1"), reason="r")

        restarted = InProcessControlPlane(factory=_noop_factory)
        self.assertEqual(list_approvals(restarted).body["items"], [])

    def test_listing_does_not_require_the_run_to_be_loaded(self) -> None:
        """列表是过滤语义：Run 还没被装载时返回空集，而不是 404。

        最需要看见待批事项的时刻，恰恰是服务刚重启、Run 还没重新装载的时刻。
        """
        _human_loop(self.store).request(_action(run_id="run_ghost"), reason="r")
        cp = InProcessControlPlane(factory=_noop_factory, approvals=self.store)
        resp = list_approvals(cp, "run_ghost")
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body["items"]), 1)


# ---------------------------------------------------------------- A-11
class AtomicDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.store = _open_store()
        self.loop = _human_loop(self.store)

    def tearDown(self) -> None:
        self.conn.close()

    def _new(self):
        return self.loop.request(_action(), reason="r")

    def test_a11_second_transition_loses(self) -> None:
        """A-5 声称"已决定的再回调 → 409"。在并发下只有原子 transition 能兑现。"""
        req = self._new()
        self.assertTrue(
            self.store.transition(req.approval_id, ApprovalStatus.APPROVED,
                                  by="alice", decided_at=NOW)
        )
        self.assertFalse(
            self.store.transition(req.approval_id, ApprovalStatus.REJECTED,
                                  by="bob", decided_at=NOW)
        )
        # 关键：审计记录里留下的是第一个人
        self.assertEqual(self.store.get(req.approval_id).decided_by, "alice")

    def test_a11_the_hole_this_closes_is_real(self) -> None:
        """反过来证明这个洞是真的：只 save 不 transition，后写的会覆盖先写的。

        这条测试就是 A-11 存在的理由 —— 它演示的是"读出来判一下再写"会怎样。

        注意这里两次写入都**完全合法**（都带 by / decided_at）：
        DB 的 CHECK 约束能拦住"畸形写入"，但拦不住"两个都合法的写入互相覆盖" ——
        约束管的是**值的形状**，管不了**先后顺序**。
        """
        req = self._new()

        first = self.store.get(req.approval_id)
        second = self.store.get(req.approval_id)     # 两个请求读到的都是 PENDING
        assert first is not None and second is not None
        self.assertEqual(first.status, ApprovalStatus.PENDING)
        self.assertEqual(second.status, ApprovalStatus.PENDING)

        first.status = ApprovalStatus.APPROVED
        first.decided_by = "alice"
        first.decided_at = NOW
        self.store.save(first)
        second.status = ApprovalStatus.REJECTED
        second.decided_by = "bob"
        second.decided_at = NOW
        self.store.save(second)

        # 两边的前置检查都通过了，但"谁批的"变成了后到的人
        self.assertEqual(self.store.get(req.approval_id).decided_by, "bob")

    def test_a11_lost_race_raises_concurrent_state_error(self) -> None:
        """输了竞争必须报错，不能假装成功 —— 否则调用方以为自己批过了。

        顺序调用撞不出这个窗口（`_must_get()` 会重新读库，读到的已经是终态），
        所以这里用替身把"读的时候 PENDING、写的时候不是了"显式摆出来。
        """
        req = self._new()
        loop = HumanLoop(store=_RacingStore(self.store), clock=self.loop.clock)
        with self.assertRaises(ConcurrentStateError):
            loop.approve(req.approval_id, by="bob")
        # 关键：库里**仍然**是 PENDING —— "没批成"绝不能被记成"批过了"
        self.assertEqual(self.store.get(req.approval_id).status, ApprovalStatus.PENDING)

    def test_expire_due_is_also_atomic(self) -> None:
        """扫描也可能并发（多个 sweeper），同样只认 PENDING。"""
        req = self._new()
        clock = self.loop.clock
        clock.advance(timedelta(minutes=31))

        self.assertEqual(self.loop.expire_due(), [req.approval_id])
        self.assertEqual(self.loop.expire_due(), [])      # 第二次扫不到了
        self.assertEqual(
            self.store.get(req.approval_id).status, ApprovalStatus.EXPIRED
        )
        self.assertEqual(self.store.get(req.approval_id).decided_by, "timeout")


# ---------------------------------------------------------------- DB 兜底
class DbConstraintTest(unittest.TestCase):
    """领域里的检查是"善意"，DB 约束才是"兜底"。"""

    def setUp(self) -> None:
        self.conn, _ = _open_store()

    def tearDown(self) -> None:
        self.conn.close()

    def _expect_rejected(self, **overrides) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _raw_insert(self.conn, **overrides)
        self.conn.rollback()

    def test_a8_decided_without_an_actor_is_rejected(self) -> None:
        """匿名审批进不了审计 —— 在 DB 层也进不去。"""
        self._expect_rejected(status="approved", decided_by=None, decided_at=None)

    def test_a8_decided_without_a_time_is_rejected(self) -> None:
        self._expect_rejected(status="approved", decided_by="alice", decided_at=None)

    def test_pending_with_an_actor_is_rejected(self) -> None:
        """待批状态里不能藏着决策者 —— 否则分不清"还没人批"和"批过了没推进"。"""
        self._expect_rejected(status="pending", decided_by="alice")

    def test_h5_non_positive_ttl_is_rejected(self) -> None:
        """一条没有截止时间的审批 = 允许一个人把 Run 永久挂住。"""
        self._expect_rejected(expires_at=NOW)

    def test_unknown_status_is_rejected(self) -> None:
        self._expect_rejected(status="maybe")

    def test_timeout_is_not_a_human_decision(self) -> None:
        """expired 是时间判死的，不是人 —— 所以它没有 decided_by 也合法。"""
        _raw_insert(
            self.conn,
            status="expired",
            decided_at=NOW,
        )
        row = self.store_get()
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["decided_by"])

    def store_get(self):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT status, decided_by FROM approvals WHERE approval_id = %s",
            ("apr_raw",),
        )
        return cur.fetchone()


# ---------------------------------------------------------------- 替身
def _noop_factory(agent_id: str, approvals):  # pragma: no cover - 不会被调用
    raise AssertionError("this test never starts a run")


class _RacingStore:
    """模拟"我读的时候是 PENDING，我写的时候已经不是了"。

    这是 A-11 要防的那个窗口。顺序调用撞不出来，只能靠替身摆出来。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def save(self, request):
        return self._inner.save(request)

    def get(self, approval_id):
        return self._inner.get(approval_id)

    def pending(self, run_id=None):
        return self._inner.pending(run_id)

    def transition(self, *args, **kwargs) -> bool:
        return False                        # 别人抢先决定了


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
