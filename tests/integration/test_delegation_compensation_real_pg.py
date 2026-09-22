"""真 PostgreSQL 上的 **D-12 / D-13 / S-15**（M54 / 承接 M53）。

--------------------------------------------------------------------------
这一层要验什么（内存单测验不了的）

`tests/unit/test_delegation_compensation.py` 跑在内存 store 上，而下面两条
**恰恰是 PG 物理保证**：

> **S-15**（UNRESOLVED 不会被认领）—— 靠 `claim()` 里
> `UPDATE ... WHERE status = 'pending'` 的条件。
> 与 A-11 / S-4 完全同源：一条**条件 UPDATE**，不是一句 Python `if`。
> 内存版是同步字典，两个 Coordinator 根本抢不起来，也无从证明
> "状态不是 pending 时这条 UPDATE 匹配 0 行"。
>
> D-12 记下的那条账，必须**真的落在库里** —— 它是"外部世界留了东西、
> 但没人负责"这条事实的**唯一**物证。只活在内存里的话，
> 进程一重启它就消失了，而它记的恰恰是**重启后最该被看见**的东西。

--------------------------------------------------------------------------
为什么"另一条连接"是硬要求

同一个连接读自己刚写的，读到的是自己的内存 —— 那条断言什么也证明不了。
所以下面每一条都从**另一条连接**读。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_runtime.adapters.postgres import PostgresCompensationStore
from packages.agent_runtime.saga import SagaCoordinator

from ._pg import RealPostgresCase, real_pg
from .test_child_wait_deadline_real_pg import _compensable

RUN = "run_deleg"


class DelegationCompensationOnRealPostgresTest(RealPostgresCase):
    """委派没做成的时候，那笔"存疑"的账在真库上是不是真的在、真的没人领。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresCompensationStore(self.conn)
        self.saga = SagaCoordinator(store=self.store)
        #: 另一条连接 —— 所有断言都从它读（模拟另一个进程 / 重启后）
        self.other = real_pg(fresh=False)
        self.addCleanup(self.other.close)

    def _book_unresolved(self, execution_id: str, reason: str) -> Any:
        return self.saga.record_unresolved(
            run_id=RUN,
            step_id="step_1",
            task_id=f"task_{execution_id}",
            execution_id=execution_id,
            action=_compensable(RUN),
            reason=reason,
        )

    # ---------------------------------------------------------------- D-12

    def test_d12_the_unresolved_record_really_lands_in_the_database(self):
        """D-12 记下的那条账必须**真在库里**，从另一条连接也读得到。

        它记的是"外部世界留了东西、但没人负责"——
        进程重启后最该被看见的就是它，所以它不能只活在内存里。
        """
        record = self._book_unresolved("exec_d12", "D-12: delegation failed")
        self.assertIsNotNone(record)

        seen = PostgresCompensationStore(self.other).get(record.compensation_id)
        self.assertIsNotNone(seen, "另一条连接必须能读到它")
        self.assertIs(seen.status, CompensationStatus.UNRESOLVED)
        self.assertIn("D-12", seen.reason or "")

    def test_d12_the_reason_distinguishes_failed_from_cancelled(self):
        """failed 与 cancelled 在理由里要能分开 —— 排障方向不一样（D-10 同款）。"""
        failed = self._book_unresolved("exec_failed", "D-12: delegation failed")
        cancelled = self._book_unresolved("exec_cancel", "S-15: delegation cancelled")

        other_store = PostgresCompensationStore(self.other)
        self.assertIn("D-12", other_store.get(failed.compensation_id).reason)
        self.assertIn("S-15", other_store.get(cancelled.compensation_id).reason)

    # ---------------------------------------------------------------- S-15

    def test_s15_unresolved_is_not_claimable_from_another_connection(self):
        """S-15 的核心：UNRESOLVED **不会被认领**。

        这决定了一笔"存疑"的账会不会被人擅自撤销 ——
        不知道副作用到底发生没发生，就不能替它决定回滚。
        """
        record = self._book_unresolved("exec_s15", "D-12: delegation failed")

        # 另一条连接（另一个 Coordinator）试着认领它
        claimed = PostgresCompensationStore(self.other).claim(record.compensation_id)
        self.assertIsNone(claimed, "UNRESOLVED 不许被认领 —— S-15")

    def test_s15_and_the_record_stays_unresolved_after_the_attempt(self):
        """认领失败之后，它还**原封不动**是 UNRESOLVED（不是被改成 RUNNING）。

        条件 UPDATE 匹配 0 行，行的状态不该有任何变化。
        """
        record = self._book_unresolved("exec_s15b", "D-12: delegation failed")
        PostgresCompensationStore(self.other).claim(record.compensation_id)

        seen = PostgresCompensationStore(self.other).get(record.compensation_id)
        self.assertIs(seen.status, CompensationStatus.UNRESOLVED)
        self.assertEqual(seen.attempts, 0, "匹配 0 行就不该动 attempts")

    def test_s15_unresolved_is_visible_not_silent(self):
        """S-5 的另一半：它留在 `unresolved_for()` 里 —— 看得见，不当没发生。"""
        self._book_unresolved("exec_s5", "D-12: delegation failed")

        pending = list(PostgresCompensationStore(self.other).unresolved_for(RUN))
        self.assertEqual(len(pending), 1)
        self.assertIn("D-12", pending[0].reason)


if __name__ == "__main__":
    unittest.main()
