"""真 PostgreSQL 上的取消幂等键（M44 / 空洞 223）。

--------------------------------------------------------------------------
为什么这一层不能只在内存里验

幂等键的全部价值在于**它活得比这个进程久**：

    t0  进程 A 收到"停止"，落了键、取消了 Run
    t1  进程 A 挂了（或重启了）
    t2  进程 B 收到客户端的重试

内存版 `InMemoryIdempotencyStore` 在 t1 那一刻就空了 ——
于是 t2 会重新走一遍取消，撞上终态，报 409：
**一次成功的取消，因为重启而被报成失败**。

而 A-3 写着得更死："幂等键**不能放 Redis**"（丢了 = UNKNOWN）。
所以这一层要验的三件事是内存版一行都验不到的：

1. 键在 PG 里，换一个 store 对象（= 换一个进程）仍然命中；
2. 那一行的 `durable` 是 TRUE（丢了就是事故，不是"回查下游即可"）；
3. 跨进程那条路（取消意图）的答案在重启后**照原样**回放 ——
   它的 `status` 是 `unknown`，而 `unknown` 恰恰是**装载不回来**才有的答案。
"""
from __future__ import annotations

import unittest

from packages.agent_api import InProcessControlPlane, start_run
from packages.agent_api.handlers import cancel_run
from packages.agent_runtime.adapters.postgres import PostgresRunCancellationStore
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.execution_kernel.adapters.postgres import PostgresIdempotencyStore

from tests.unit.test_child_run import (
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)
from tests.unit.test_control_plane_api import RiskyThenFinish

from ._pg import RealPostgresCase

BODY = {"reason": "user asked", "by": "alice"}


class CancelIdempotencyCase(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        self.cp = InProcessControlPlane(
            factory=self._factory,
            idempotency=PostgresIdempotencyStore(self.conn),
            cancellations=PostgresRunCancellationStore(self.conn),
        )

    def _factory(self, agent_id: str, approvals):  # noqa: ANN001
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=RiskyThenFinish(),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            approval_store=approvals,
        )

    def _start(self, *, key: str = "") -> str:
        resp = start_run(
            self.cp,
            {"agent_id": "agent-it", "user_request": "compute 6*7"},
            idempotency_key=key,
        )
        self.assertIn(resp.status, (200, 201), resp.body)
        return str(resp.body["run_id"])

    def _cancel(self, run_id: str, *, key: str = "", **over):
        body = dict(BODY)
        body.update(over)
        return cancel_run(self.cp, run_id, body, idempotency_key=key)

    def _restart(self) -> None:
        """换一个 store 对象 —— 等价于"进程重启了，内存全丢"。

        刻意换的是**对象**而不是连接：同一个库、同一张表，
        唯一丢的是进程内存。这正是 t1 那一刻发生的事。
        """
        self.cp = InProcessControlPlane(
            factory=self._factory,
            idempotency=PostgresIdempotencyStore(self.conn),
            cancellations=PostgresRunCancellationStore(self.conn),
        )


class TheKeyOutlivesTheProcessTest(CancelIdempotencyCase):
    """键活得比进程久 —— 重启之后的重试必须拿到第一次的答案。"""

    def test_the_replay_survives_a_restart(self) -> None:
        run_id = self._start()
        first = self._cancel(run_id, key="k1")
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual(first.body["status"], "cancelled")

        self._restart()

        second = self._cancel(run_id, key="k1")
        self.assertEqual(second.status, 200, second.body)
        self.assertTrue(second.body["replayed"])
        self.assertEqual(second.body["status"], "cancelled")
        self.assertEqual(second.body["last_outcome"], "cancelled")

    def test_the_cross_process_answer_survives_a_restart(self) -> None:
        """`unknown` 那份答案在重启后照原样回放（D-36）。

        它是最容易被"重新推导"吃掉的一份答案：
        重启后这条 Run 仍然装载不回来，于是任何"先看看现在怎么样"的写法
        都只能给出"我不知道"之外的一个猜测。
        """
        run_id = self._start()
        self.cp.runs.pop(run_id)                 # 它跑到别的进程去了

        first = self._cancel(run_id, key="k1")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.body["status"], "unknown")
        self.assertTrue(first.body["cancel_requested"])

        self._restart()

        second = self._cancel(run_id, key="k1")
        self.assertEqual(second.status, 200, second.body)
        self.assertTrue(second.body["replayed"])
        self.assertEqual(second.body["status"], "unknown")

    def test_a_reused_key_is_refused_after_a_restart_too(self) -> None:
        """指纹也在 PG 里 —— 重启之后"换了请求体"照样认得出来。"""
        run_id = self._start()
        self._cancel(run_id, key="k1")
        self._restart()
        resp = self._cancel(run_id, key="k1", reason="something else")
        self.assertEqual(resp.status, 422, resp.body)
        self.assertEqual(resp.body["error"]["code"], "IDEMPOTENCY_KEY_REUSED")


class TheCancelKeyIsDurableTest(CancelIdempotencyCase):
    """A-3：取消的幂等键**不能放 Redis**，丢了就是事故。"""

    def test_the_row_is_marked_durable(self) -> None:
        run_id = self._start()
        self._cancel(run_id, key="k1")

        cur = self.conn.cursor()
        cur.execute("SELECT durable FROM idempotency_keys WHERE key = %s", ("cancel:k1",))
        row = cur.fetchone()
        self.assertIsNotNone(row, "取消的键必须落库 —— 没有它重启就重跑一遍")
        self.assertTrue(row["durable"], "取消的键丢了就是事故（A-3）")

    def test_the_cancel_namespace_does_not_collide_with_the_run_namespace(self) -> None:
        """同一个裸键 "shared" 先开 Run、再叫停 —— 两次都得真的发生。

        共用一个命名空间的后果不是报错，是**静默**：
        cancel 命中 start 留下的记录，返回一个 RunView 而根本没叫停。
        所以这里同时钉两侧：两行都在，且那条 Run **真的停了**。
        """
        run_id = self._start(key="shared")
        resp = self._cancel(run_id, key="shared")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertFalse(resp.body["replayed"], "cancel 不该命中 start 的记录")
        self.assertEqual(resp.body["status"], "cancelled")

        cur = self.conn.cursor()
        cur.execute("SELECT key FROM idempotency_keys ORDER BY key")
        keys = [r["key"] for r in cur.fetchall()]
        self.assertIn("run:shared", keys)
        self.assertIn("cancel:shared", keys)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
