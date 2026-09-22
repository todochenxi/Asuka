"""适配器必须在**生产那条连接**上被验证，而不是在"更好用的替身"上（M28）。

--------------------------------------------------------------------------
为什么要有这个文件

M28 第一次真起 `apps/api`（真 PG、真 uvicorn、真 HTTP）时，
走到"挂起等审批"那一步直接 500：

    File "packages/agent_harness/adapters/postgres.py", line 111, in _row_to_approval
        approval_id=row["approval_id"],
    TypeError: tuple indices must be integers or slices, not str

原因：`apps._bootstrap.pg_connection()` 是 `psycopg.connect(dsn)`，
**没有设 `row_factory`**。psycopg 默认返回元组，而三个 PG 适配器
全部按列名取字段。

为什么 660 个测试没一个发现它：

    `tests/unit/sqlite_shim.py`  →  `raw.row_factory = sqlite3.Row`（按名取 √）
    `tests/integration/_pg.py`   →  `row_factory=dict_row`（按名取 √）

**两个替身都比生产好用。** 生产那条路径（`pg_connection`）从来没有被
跑过一次 —— 这正是 PR-28 说的"替身能过 ≠ 被认可"，而且是最狠的一个变体：
不是替身简化了对错，是替身比真的更宽容。

--------------------------------------------------------------------------
这个文件的判据

它**不许**用 `RealPostgresCase.conn`（那个自带 `dict_row`）。
连接必须由 `apps._bootstrap.pg_connection()` 造出来 ——
也就是生产真正用的那个函数。否则这个 bug 明天还能再回来一次。
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from tests.integration import _pg

from packages.agent_domain.business.compensation import CompensationSpec

from packages.agent_domain.intelligence.action import (
    Action,
    ActionType,
    RiskLevel,
)
from packages.agent_harness.adapters.postgres import PostgresApprovalStore
from packages.agent_harness.approval import ApprovalRequest, ApprovalStatus
from packages.execution_kernel.adapters.postgres import (
    PostgresIdempotencyStore,
)


def _production_connection() -> Any:  # type: ignore[valid-type] # noqa: F821
    """**生产**用的那个连接 —— 不是测试替身。

    少这一层，本文件就退化成又一份 `RealPostgresCase`，
    而它要防的那个 bug 恰好只存在于这一层。
    """
    from apps._bootstrap import RuntimeConfig, pg_connection

    config = RuntimeConfig.from_env(
        {"AGENTOS_PG_DSN": _pg.dsn(), "AGENTOS_HEARTBEAT_SECONDS": "1"}
    )
    return pg_connection(config)


class ConnectionShapeTest(unittest.TestCase):
    """连接本身的形状 —— 失败时报错要能直接指出原因。"""

    def setUp(self) -> None:
        self.conn = _pg.real_pg()                 # 迁移 + 建表
        self.conn.close()
        self.prod = _production_connection()
        self.addCleanup(self.prod.close)

    def test_rows_are_addressable_by_column_name(self) -> None:
        """三个适配器全部 `row["col"]`。元组行会让它们全部崩。"""
        cur = self.prod.cursor()
        cur.execute("SELECT 1 AS probe")
        row = cur.fetchone()
        self.assertNotIsInstance(
            row, tuple, "生产连接返回元组行 —— 适配器按列名取字段会全部 TypeError"
        )
        self.assertEqual(row["probe"], 1)

    def test_the_connection_has_a_real_transaction_boundary(self) -> None:
        """M29：这条断言在 M28 时是反过来的，现在必须改正。

        M28 时 `pg_connection()` 开的是 `autocommit=True`，理由是当时
        `packages/` 里**没有任何一处 `commit()`** —— 不开它，
        每次写都在隐式事务里、进程退出即回滚，"PG = Truth" 是空话且不报错。

        M29 补上了真正的边界（`pg_unit_of_work`，PR-30 / PR-31），
        于是这里**必须**关掉 autocommit：开着它，
        `repository.add()` 和 `outbox.append()` 各自提交，
        X-3 要求的"一起生效"就没有任何物理对应物。

        代价是任何不经过边界的写都会静默丢失 ——
        所以边界由组合根统一接（`pg_unit_of_work`），
        而不是指望每个进程自己记得提交。
        那个代价的实证在 `test_x3_transaction_boundary_real_pg.py`。
        """
        self.assertFalse(
            getattr(self.prod, "autocommit", True),
            "生产连接开在 autocommit 上 —— X-3（状态与事件同一事务）"
            "没有任何物理对应物",
        )


class ApprovalStoreOnTheProductionConnectionTest(unittest.TestCase):
    """把 `PostgresApprovalStore` 的四个方法都在生产连接上走一遍。"""

    def setUp(self) -> None:
        _pg.real_pg().close()                     # 只为了推平 schema + 迁移
        self.conn = _production_connection()
        self.addCleanup(self.conn.close)
        self.store = PostgresApprovalStore(self.conn)

    def _approval(self, approval_id: str = "apr_it_1") -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=approval_id,
            run_id="run_it_1",
            action=Action(
                run_id="run_it_1",
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "note.write", "args": {"text": "hi"}},
                risk_level=RiskLevel.HIGH,
                compensation=CompensationSpec(
                    tool="note.write",
                    args={"text": ""},
                    description="删掉刚写的那条笔记",
                ),
            ),
            reason="policy[risk-gate]",
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        )

    def test_save_then_get_round_trips(self) -> None:
        """`get()` 会读列 —— 这一条就是 M28 那个 500 的落点。"""
        self.store.save(self._approval())
        got = self.store.get("apr_it_1")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.approval_id, "apr_it_1")
        self.assertEqual(got.status, ApprovalStatus.PENDING)

    def test_the_action_survives_the_round_trip(self) -> None:
        """PR-27：声明了就必须能读回来 —— 补偿声明丢了，Saga 就少一条账。"""
        self.store.save(self._approval())
        got = self.store.get("apr_it_1")
        assert got is not None and got.action is not None
        self.assertIsNotNone(got.action.compensation)

    def test_pending_lists_it(self) -> None:
        self.store.save(self._approval())
        items = self.store.pending("run_it_1")
        self.assertEqual([a.approval_id for a in items], ["apr_it_1"])

    def test_transition_wins_only_once(self) -> None:
        """A-11：判定与写入是一个语句，第二次必须输。"""
        self.store.save(self._approval())
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        self.assertTrue(
            self.store.transition(
                "apr_it_1", ApprovalStatus.APPROVED, by="alice", decided_at=now
            )
        )
        self.assertFalse(
            self.store.transition(
                "apr_it_1", ApprovalStatus.REJECTED, by="bob", decided_at=now
            )
        )
        got = self.store.get("apr_it_1")
        assert got is not None
        self.assertEqual(got.decided_by, "alice")


class IdempotencyStoreOnTheProductionConnectionTest(unittest.TestCase):
    """A-3：幂等键必须与 PG 同生共死，所以它也得在生产连接上被验。"""

    def setUp(self) -> None:
        _pg.real_pg().close()
        self.conn = _production_connection()
        self.addCleanup(self.conn.close)
        self.store = PostgresIdempotencyStore(self.conn)

    def test_put_then_get(self) -> None:
        self.store.put("run:key-1", {"run_id": "run_it_1"})
        self.assertEqual(self.store.get("run:key-1"), {"run_id": "run_it_1"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
