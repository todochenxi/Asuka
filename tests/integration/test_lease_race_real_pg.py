"""真 PostgreSQL 上的 **租约并发**（M56 / A-11 + E-13 + E-22）。

--------------------------------------------------------------------------
这一层要验什么（内存单测验不了的）

`tests/unit/test_attempt_lease_retry.py` 里那几条租约测试，
验的都是**领域语义**（E-6 序号单调、E-7 只有 RUNNING 能持租约、
E-20 租约挂在 Execution 上、E-22 过期持有者的写回被拒、租约到期）。

但有一条它验不了：

> **两个 worker 同时抢同一个 Execution 的租约 —— 只能一个拿到。**

这条决定的是：同一条 Execution **会不会被两个 worker 各执行一遍**。
工具是有副作用的，执行两遍就是外部世界被改两次。

它的物理保证是 `UPDATE_EXECUTION` 末尾那句：

    WHERE execution_id = %s AND version = %s       ← E-25 乐观锁

**条件 UPDATE + rowcount 判胜负** —— 与 S-4 / S-15 / A-11 同一种写法
（M55 §93.7 记过：先读一下再写，并发下必然两边都通过）。

内存版是同步的单线程，两个 worker 根本到不了同一时刻，
所以它那里"只有一个赢"是一句空话。

--------------------------------------------------------------------------
"另一条连接" = 另一个 worker

两个进程各自持有一条连接，各自以为自己拿到了租约 ——
这正是生产环境里 worker 池的形状。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from packages.execution_kernel import ExecutionKernel, KernelConfig, ManualClock
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)

from tests.unit.helpers import make_task

from ._pg import RealPostgresCase, real_pg


class LeaseRaceOnRealPostgresTest(RealPostgresCase):
    """两个 worker 抢同一条 Execution，只有一个能拿到租约。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = ManualClock()
        #: 第二个 worker：另一条连接、另一个 kernel、自己的仓库
        #
        # ⚠️ 必须 `fresh=False`：`real_pg()` 默认会**重建库**，
        # 那会把第一个 worker 刚写的东西全清掉。于是第二个 worker 的
        # `claim()` 会因为"找不到这条 Execution"而抛异常 ——
        # 断言照样通过，但通过的原因是"库被我清空了"，**一条假绿**。
        self.worker_b = real_pg(fresh=False)
        self.addCleanup(self.worker_b.close)

    def _kernel(self, conn: Any) -> ExecutionKernel:
        return ExecutionKernel(
            repository=PostgresExecutionRepository(conn),
            attempts=PostgresAttemptRepository(conn),
            outbox=PostgresOutboxStore(conn),
            clock=self.clock,
            tasks=PostgresTaskRepository(conn),
            config=KernelConfig(default_lease_ttl=timedelta(seconds=30)),
        )

    def _submit(self, kernel: ExecutionKernel) -> str:
        execution = kernel.submit(make_task())
        return str(execution.execution_id)

    def test_two_workers_racing_only_one_gets_the_lease(self):
        """两个 worker 同时认领同一条 Execution —— 只有一个成功。

        失败的那个必须**报错**，不能静默返回"我也拿到了"：
        静默成功意味着这条 Execution 会被执行两遍。
        """
        kernel_a = self._kernel(self.conn)
        kernel_b = self._kernel(self.worker_b)

        execution_id = self._submit(kernel_a)

        first = kernel_a.claim(execution_id, worker_id="worker-a")
        self.assertIsNotNone(first, "第一个 worker 必须拿到租约")

        # 第二个 worker：它手上的还是旧版本，认领必须失败
        with self.assertRaises(Exception):          # noqa: B017 - E-13 并发冲突
            kernel_b.claim(execution_id, worker_id="worker-b")

    def test_the_lease_names_the_worker_that_won(self):
        """库里记的持租人必须是赢的那个 —— 输了的不许覆盖。"""
        kernel_a = self._kernel(self.conn)
        kernel_b = self._kernel(self.worker_b)
        execution_id = self._submit(kernel_a)

        kernel_a.claim(execution_id, worker_id="worker-a")
        try:
            kernel_b.claim(execution_id, worker_id="worker-b")
        except Exception:                            # noqa: BLE001
            pass

        # 从**第三条**连接看，持租人到底是谁（同样不能 fresh，否则库被清空）
        witness = real_pg(fresh=False)
        self.addCleanup(witness.close)
        row = witness.execute(
            "SELECT lease_worker_id, lease_fencing_token FROM executions"
            " WHERE execution_id = %s",
            (execution_id,),
        ).fetchone()
        self.assertEqual(row["lease_worker_id"], "worker-a",
                         "输了的那次不许把持租人改成自己")

    def test_a_stale_writer_cannot_report_a_result(self):
        """E-22 的真库版本：拿着**落后**的 fencing token 回报结果，必须被拒。

        两个细节，写错一个就变假绿：

        1. token 必须**更小**才叫过期 —— `authorize()` 只在
           `token < fencing_token` 时抛（传更大的 token 是合法的）。
        2. 不能拿 `cancel()` 来测：源码注释写着
           "过期 Worker 也能被系统级取消"，`cancel()` 是**故意放行**的。
           所以要用"回报结果"这种会改业务状态的写回。
        """
        kernel_a = self._kernel(self.conn)
        kernel_b = self._kernel(self.worker_b)
        execution_id = self._submit(kernel_a)

        _attempt, lease_a = kernel_a.claim(execution_id, worker_id="worker-a")
        current = int(lease_a.fencing_token)

        with self.assertRaises(Exception):          # noqa: B017 - E-22
            kernel_b.succeed(
                execution_id,
                token=current - 1,                  # 落后于当前值 = 过期持有者
                result={"answer": 42},
            )


if __name__ == "__main__":
    unittest.main()
