"""真 PostgreSQL 上的 **审批存储**（M55 / A-10 + A-11）。

--------------------------------------------------------------------------
这一层要验什么（sqlite 替身验不了的）

`tests/unit/test_approval_store_postgres.py` 有 17 条，但它跑在
`sqlite_shim` 上（该文件自己写着"schema 直接读 003_approvals.sql 原文、
测的是真 schema 的语义"）。替身只有**形状**，证明不了下面两件事：

> **A-10**（审批必须活过进程重启）—— 它的全部意义就是"重启后还在"。
> 而"重启"这件事在 sqlite 替身上无从模拟：替身与被测代码共享同一个
> 进程内的 sqlite 连接，内存从来没被清过。
>
> **A-11**（判定与写入是一个语句）—— 源码注释写得很明白：
>     `AND status = 'pending'` 就是"我没输这场竞争"。
> 这是一条**条件 UPDATE**。替身是同步的，两个"进程"根本到不了同一时刻，
> 也无从证明"第二个到达时 rowcount = 0"。

顺带一提：shim 自己也承认不完整 —— 013 那根外键 sqlite 加不了。
所以替身与真库**本来就不同构**。

--------------------------------------------------------------------------
为什么"另一条连接"就是"重启"

服务重启 = 内存里的 stacks / 缓存全丢，只剩 PG。
一条**新的连接**正是那个状态：它什么内存都没有，只能去库里读。
所以下面每条都从另一条连接读 —— 那就是一次重启。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_harness.approval import ApprovalStatus, HumanLoop
from packages.agent_harness.adapters.postgres import PostgresApprovalStore
from packages.execution_kernel import ManualClock

from ._pg import RealPostgresCase, real_pg

RUN = "run_appr"


def _action(run_id: str = RUN, expr: str = "6*7") -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "calculator", "args": {"expr": expr}},
        risk_level=RiskLevel.HIGH,
        rationale="scripted high risk",
    )


class ApprovalStoreOnRealPostgresTest(RealPostgresCase):
    """A-10（活过重启）与 A-11（只有一个赢）在真库上是否成立。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresApprovalStore(self.conn)
        self.clock = ManualClock()
        self.loop = HumanLoop(store=self.store, clock=self.clock)
        #: 另一条连接 = 一次进程重启（内存全丢，只能读库）
        self.restarted = real_pg(fresh=False)
        self.addCleanup(self.restarted.close)

    def _request(self, reason: str = "high risk tool call") -> Any:
        return self.loop.request(_action(), reason=reason)

    # ---------------------------------------------------------------- A-10

    def test_a10_a_pending_approval_survives_a_restart(self):
        """A-10 的主断言：重启之后，待批事项**还在**。

        M19 就是为了这条：在那之前，重启后待批列表是空的，
        而 Run 还实实在在挂着等人批 —— **界面上什么都没有，系统里全在等**，
        这是最难排查的一类故障。
        """
        req = self._request()

        after_restart = PostgresApprovalStore(self.restarted)
        loaded = after_restart.get(req.approval_id)
        self.assertIsNotNone(loaded, "重启后必须还能读到它")
        self.assertIs(loaded.status, ApprovalStatus.PENDING)

    def test_a10_the_pending_list_survives_too(self):
        """列表也要在 —— 人看的是列表，不是单个 id。"""
        self._request("first")
        self._request("second")

        pending = list(PostgresApprovalStore(self.restarted).pending(RUN))
        self.assertEqual(len(pending), 2, "重启后列表不许变空")
        self.assertEqual({p.reason for p in pending}, {"first", "second"})

    def test_a10_the_action_survives_intact(self):
        """人要看的是"我放行的到底是什么" —— 存丢了这个就没意义了。"""
        req = self._request()

        loaded = PostgresApprovalStore(self.restarted).get(req.approval_id)
        self.assertEqual(loaded.action.action_type, ActionType.TOOL_CALL)
        self.assertEqual(loaded.action.risk_level, RiskLevel.HIGH)
        self.assertEqual(loaded.action.payload["tool"], "calculator")

    # ---------------------------------------------------------------- A-11

    def test_a11_two_deciders_only_one_wins(self):
        """A-11 的核心：两条连接同时决定同一条审批，**只有一个成功**。

        这决定"谁批的"能不能说清 —— 两个人都去批，
        如果两次都成功，审计记录就被第二次覆盖了。
        """
        req = self._request()
        now = datetime.now(timezone.utc)

        winner = self.store.transition(
            req.approval_id, ApprovalStatus.APPROVED, by="alice", decided_at=now
        )
        loser = PostgresApprovalStore(self.restarted).transition(
            req.approval_id, ApprovalStatus.REJECTED, by="bob", decided_at=now
        )

        self.assertTrue(winner, "第一个决定者必须成功")
        self.assertFalse(loser, "第二个必须落空 —— A-11")

    def test_a11_the_record_still_names_the_first_decider(self):
        """落空的那次不许覆盖 —— "谁批的"要一直是第一个人。"""
        req = self._request()
        now = datetime.now(timezone.utc)

        self.store.transition(
            req.approval_id, ApprovalStatus.APPROVED, by="alice", decided_at=now
        )
        PostgresApprovalStore(self.restarted).transition(
            req.approval_id, ApprovalStatus.REJECTED, by="bob", decided_at=now
        )

        loaded = PostgresApprovalStore(self.restarted).get(req.approval_id)
        self.assertIs(loaded.status, ApprovalStatus.APPROVED, "不该被改成 REJECTED")
        self.assertEqual(loaded.decided_by, "alice", "谁批的不许被覆盖")

    def test_a11_a_second_transition_does_not_bump_the_version(self):
        """匹配 0 行 = 这一行任何字段都不该变（含 version）。"""
        req = self._request()
        now = datetime.now(timezone.utc)
        self.store.transition(
            req.approval_id, ApprovalStatus.APPROVED, by="alice", decided_at=now
        )

        # `ApprovalRequest` 不带 version（那是存储层的乐观锁），所以直接问库
        def version_of() -> int:
            row = self.restarted.execute(
                "SELECT version FROM approvals WHERE approval_id = %s",
                (req.approval_id,),
            ).fetchone()
            return int(row["version"])

        before = version_of()
        PostgresApprovalStore(self.restarted).transition(
            req.approval_id, ApprovalStatus.APPROVED, by="bob", decided_at=now
        )
        self.assertEqual(version_of(), before, "落空的 transition 不许动 version")


if __name__ == "__main__":
    unittest.main()
