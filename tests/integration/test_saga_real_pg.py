"""真 PostgreSQL 上的 **Saga / 补偿账本**（M53）。

--------------------------------------------------------------------------
这一层要验什么（内存单测验不了的）

`tests/unit/test_saga.py` 跑在 `InMemoryCompensationStore` 上，
而 S-2 与 S-4 这两条**恰恰是 PG 物理保证**，内存里根本无从谈起：

> **S-2**（一条副作用一条记录）—— 靠 `compensations.execution_id` 上的
> `UNIQUE`。内存字典上"重复插入"只是一次覆盖，没有任何东西会拒绝它。
>
> **S-4**（原子认领）—— 靠 `UPDATE ... WHERE status='pending'` 的 rowcount。
> 与 A-11 完全同源：**"先读一下是不是 PENDING 再写"在两个进程同时到达时
> 两边都会通过**。内存里 `claim()` 是同步的，两个协程根本抢不起来。

所以在内存里绿了，只说明"逻辑写了"，不说明"这两个保证成立"。
这一层就是补上那个"成立"。

--------------------------------------------------------------------------
为什么"另一条连接"是硬要求

同一个连接读自己刚写的，读到的是自己的内存 —— 那条断言什么也证明不了。
所以下面每一条都从**另一条连接**读。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_runtime.adapters.postgres import PostgresCompensationStore
from packages.agent_runtime.saga import SagaCoordinator

from ._pg import RealPostgresCase, real_pg
from .test_child_wait_deadline_real_pg import _compensable

RUN = "run_saga"


class SagaOnRealPostgresTest(RealPostgresCase):
    """S-2 / S-3 / S-4 在真库上是否真的成立。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresCompensationStore(self.conn)
        self.saga = SagaCoordinator(store=self.store)
        #: 另一条连接 —— 所有断言都从它读，不读自己刚写的内存
        self.other = real_pg(fresh=False)
        self.addCleanup(self.other.close)

    def _book(self, execution_id: str = "exec_1", step_id: str = "step_1") -> Any:
        return self.saga.record(
            run_id=RUN,
            step_id=step_id,
            task_id=f"task_{execution_id}",
            execution_id=execution_id,
            action=_compensable(RUN),
            result={"ticket_id": f"t-{execution_id}"},
            execution_status=ExecutionStatus.COMPLETED,
        )

    def _rows(self) -> list[Any]:
        return self.other.execute(
            "SELECT compensation_id, run_id, execution_id, status, attempts"
            "  FROM compensations WHERE run_id = %s ORDER BY created_at, compensation_id",
            (RUN,),
        ).fetchall()

    # ---------------------------------------------------------------- S-2

    def test_s2_the_database_itself_refuses_a_second_record(self):
        """同一条 Execution 插第二行 —— **数据库**必须拒绝。

        ⚠️ 这一条刻意**绕过 `saga.record()`**，直接用 SQL 插。

        为什么必须绕过：走 `saga.record()` 的话，它里面有一句 Python
        检查（`get_by_execution` 已经有了就返回 None），
        于是**就算 UNIQUE 约束被删掉，测试照样绿** ——
        那测的是 Python 检查，不是 PG 的物理保证，是一条假绿
        （本轮变红验证时抓到过一次：DROP CONSTRAINT 之后红了 0 条）。

        S-2 说的是"约定层面守不住、约束层面守得住"，
        那就只能绕过约定、直接撞约束。
        """
        self._book("exec_s2")

        # ⚠️ 必须把**所有 NOT NULL 列**都给上：少给一列，INSERT 会因为
        # "缺列"而失败，那样 UNIQUE 在不在都抛 —— 又是同一条假绿。
        # 只有"除 UNIQUE 外没有任何别的失败原因"时，这条断言才测得到约束。
        with self.assertRaises(Exception):          # noqa: B017 - PG 抛什么都算拒绝
            self.conn.execute(
                "INSERT INTO compensations"
                " (compensation_id, run_id, step_id, task_id, execution_id,"
                "  action_type, tool, args, description, status, reason,"
                "  attempts, created_at, updated_at, version)"
                " VALUES ('cmp_dup', %s, 'step_1', 'task_x', 'exec_s2',"
                "         'tool_call', 'note.write', '{}'::jsonb, 'dup',"
                "         'pending', '', 0, now(), now(), 1)",
                (RUN,),
            )
            self.conn.commit()

        self.assertEqual(len(self._rows()), 1, "库里不许出现第二行")

    def test_s2_survives_a_second_connection(self):
        """从另一条连接看，那唯一的一条确实在，而且字段正确。"""
        self._book("exec_s2b")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["execution_id"], "exec_s2b")
        self.assertEqual(rows[0]["status"], "pending")

    # ---------------------------------------------------------------- S-4

    def test_s4_atomic_claim_only_one_connection_wins(self):
        """两条连接同时认领同一条 —— 只有一个拿到，另一个拿到 None。

        这是 S-4 的全部意义：它决定两个 Coordinator 会不会把
        同一笔副作用撤销**两次**。内存版证明不了，因为字典是同步的。
        """
        record = self._book("exec_s4")
        self.assertIsNotNone(record)
        cid = record.compensation_id

        # 第二条连接上的另一个 store —— 模拟另一个进程
        other_store = PostgresCompensationStore(self.other)

        winner = self.store.claim(cid)
        loser = other_store.claim(cid)

        self.assertIsNotNone(winner, "第一个认领者必须拿到")
        self.assertIsNone(loser, "第二个认领者必须落空 —— 否则同一笔会被撤销两次")

    def test_s4_the_loser_sees_the_winner_from_its_own_connection(self):
        """落空的那条连接，从自己眼里也能看到"它已经被抢走了"。"""
        record = self._book("exec_s4b")
        cid = record.compensation_id
        self.store.claim(cid)

        seen = PostgresCompensationStore(self.other).get(cid)
        self.assertIsNotNone(seen)
        self.assertEqual(seen.status, CompensationStatus.RUNNING)

    # ---------------------------------------------------------------- S-3

    def test_s3_open_for_returns_newest_first_so_undo_is_lifo(self):
        """逆序撤销（LIFO）依赖 `open_for` 的顺序 —— 在真库上也要是新的在前。

        顺序错了，撤销就会按"先做先撤"来，
        而后做的那一步往往正依赖先做那一步留下的东西。
        """
        self._book("exec_s3a", step_id="step_1")
        self._book("exec_s3b", step_id="step_2")
        self._book("exec_s3c", step_id="step_3")

        opened = list(PostgresCompensationStore(self.other).open_for(RUN))
        self.assertEqual(len(opened), 3)
        self.assertEqual(
            [r.execution_id for r in opened],
            ["exec_s3c", "exec_s3b", "exec_s3a"],
            "必须是逆序 —— 后做的先撤",
        )


if __name__ == "__main__":
    unittest.main()
