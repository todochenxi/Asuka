"""空洞 215 在真 PostgreSQL 上：Task 落库、重启后认得回来、且外键真的成立。

--------------------------------------------------------------------------
为什么这一层不可省

`tests/unit/test_task_persistence.py` 跑在 sqlite 替身上，而 sqlite 的
`ALTER TABLE` **加不了外键**（`near "FOREIGN": syntax error`）。
013 那句 `ALTER TABLE executions ADD CONSTRAINT fk_executions_task ...`
在替身里被整句丢掉 —— 于是那一层只能验"代码路径"，
验不了"**数据库**拒绝了没有 Task 的 Execution"。

按 PR-23 的判据（换掉测试替身，测试还会红吗），E-27 只能钉在这里。

--------------------------------------------------------------------------
另一个只有真库才能回答的问题

PG 的 JSONB 读回来是 dict 还是 str，取决于驱动怎么配 loader。
`_load_mapping` 两种都认 —— 但"两种都认"这件事本身只有真库能验。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import (
    ExecutionStatus,
    ExecutorType,
    ResourceReq,
    Task,
    TaskType,
)
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)
from packages.execution_kernel.inmemory import ManualClock
from packages.execution_kernel.kernel import ExecutionKernel, KernelConfig
from packages.execution_kernel.scheduler import (
    Scheduler,
    SchedulingPolicy,
    WorkerCapability,
)
from packages.execution_kernel.worker import Worker, WorkerConfig
from tests.unit.helpers import make_task

from ._pg import RealPostgresCase


class RecordingExecutor:
    def __init__(self) -> None:
        self.seen: list[Task] = []

    def execute(self, task: Task, ctx: Any) -> Mapping[str, Any]:
        self.seen.append(task)
        return {"ok": True}


class TaskPersistenceOnRealPostgresTest(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = ManualClock()

    def _kernel(self, *, tasks: Any = "new") -> ExecutionKernel:
        return ExecutionKernel(
            repository=PostgresExecutionRepository(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            clock=self.clock,
            tasks=None if tasks is None else PostgresTaskRepository(self.conn),
            config=KernelConfig(default_lease_ttl=timedelta(seconds=30)),
        )

    def _executions(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) AS n FROM executions")
        return int(cur.fetchone()["n"])

    def _tasks(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) AS n FROM tasks")
        return int(cur.fetchone()["n"])

    # ------------------------------------------------------------ E-27：外键
    def test_the_database_refuses_an_execution_with_no_task(self) -> None:
        """E-27 的本体：不是代码拒绝，是**数据库**拒绝。

        断言点到**约束名**，不只是"抛了" ——
        否则这条测试会因为任何一句 SQL 语法错误而绿（PR-19）。
        """
        cur = self.conn.cursor()
        with self.assertRaises(Exception) as ctx:
            cur.execute(
                "INSERT INTO executions (execution_id, task_id, idempotency_key, "
                "execution_mode, status, current_attempt_no, version) "
                "VALUES ('exe_orphan', 'task_never_existed', 'exe_orphan', "
                "'task', 'PENDING', 0, 1)"
            )
        self.assertIn("fk_executions_task", str(ctx.exception))
        self.assertEqual(self._executions(), 0)

    def test_the_control_with_a_task_row_the_same_insert_is_accepted(self) -> None:
        task = make_task()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, run_id, step_id, task_type, executor_type, "
            "timeout_seconds) VALUES (%s, %s, %s, %s, %s, 60)",
            (task.task_id, task.run_id, task.step_id, "tool_call", "native"),
        )
        cur.execute(
            "INSERT INTO executions (execution_id, task_id, idempotency_key, "
            "execution_mode, status, current_attempt_no, version) "
            "VALUES ('exe_ok', %s, 'exe_ok', 'task', 'PENDING', 0, 1)",
            (task.task_id,),
        )
        self.assertEqual(self._executions(), 1)

    def test_the_task_row_cannot_be_deleted_while_an_execution_points_at_it(self) -> None:
        """PR-26：这条断言证明 E-27 的代码分支在真 PG 上**不可达**。

        插不进去（上一条）、也删不掉（这一条）——
        于是 `task_of()` 里那条 E-27 报错不是主要保证，
        是给"没有这根外键的后端"留的（sqlite 替身、013 之前的库）。
        主要保证是这根外键本身。
        """
        execution = self._kernel().submit(make_task())
        cur = self.conn.cursor()
        with self.assertRaises(Exception) as ctx:
            cur.execute("DELETE FROM tasks WHERE task_id = %s", (execution.task_id,))
        self.assertIn("fk_executions_task", str(ctx.exception))
        self.assertEqual(self._tasks(), 1)

    # ------------------------------------------------------------ E-26：重启
    def test_submit_writes_a_task_row_into_postgres(self) -> None:
        self._kernel().submit(make_task())
        self.assertEqual(self._tasks(), 1)

    def test_the_row_survives_the_process_boundary(self) -> None:
        """另一个连接（也就是另一个进程）看得到同一行。"""
        first = self._kernel()
        task = make_task(
            task_type=TaskType.LLM_CALL,
            executor_type=ExecutorType.HTTP,
            priority=9,
            tenant_id="acme",
            payload={"model": "gpt-x", "prompt": "hi"},
            timeout=timedelta(seconds=90),
        )
        execution = first.submit(task)

        second = self._kernel()
        back = second.task_of(execution.execution_id)
        self.assertEqual(back.priority, 9)
        self.assertEqual(back.tenant_id, "acme")
        self.assertEqual(back.task_type, TaskType.LLM_CALL)
        self.assertEqual(back.timeout, timedelta(seconds=90))

    def test_jsonb_payload_roundtrips_on_a_real_database(self) -> None:
        """真 JSONB：`_load_mapping` 拿到的可能是 dict，也可能是 str。"""
        first = self._kernel()
        execution = first.submit(
            make_task(payload={"tool": "search", "args": {"q": "agentos"}, "n": 3})
        )
        self.assertEqual(
            self._kernel().task_of(execution.execution_id).payload,
            {"tool": "search", "args": {"q": "agentos"}, "n": 3},
        )

    def test_jsonb_resource_requirement_roundtrips_on_a_real_database(self) -> None:
        first = self._kernel()
        execution = first.submit(
            make_task(resource_requirement=ResourceReq(gpu=2, labels=("gpu", "a100")))
        )
        back = self._kernel().task_of(execution.execution_id)
        self.assertEqual(back.resource_requirement.gpu, 2)
        self.assertEqual(back.resource_requirement.labels, ("gpu", "a100"))

    def test_a_restarted_scheduler_still_picks_the_pending_execution(self) -> None:
        """修复前这里是 `KeyError: 'exe_xxx'`，然后 worker 退避到退出。"""
        self._kernel().submit(make_task())
        self.assertEqual(len(Scheduler(self._kernel()).select(limit=10)), 1)

    def test_a_restarted_worker_still_gets_the_payload(self) -> None:
        self._kernel().submit(make_task(payload={"tool": "search"}))

        restarted = self._kernel()
        executor = RecordingExecutor()
        worker = Worker(
            kernel=restarted,
            scheduler=Scheduler(restarted),
            executors={"native": executor},
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )
        worker.run_once(limit=1)

        self.assertEqual(len(executor.seen), 1)
        self.assertEqual(executor.seen[0].payload["tool"], "search")

    def test_the_tenant_quota_still_holds_after_a_restart(self) -> None:
        first = self._kernel()
        first.submit(make_task(tenant_id="acme"))
        first.submit(make_task(tenant_id="acme"))
        running = first.repository.list_by_status(ExecutionStatus.PENDING, limit=1)[0]
        first.claim(running.execution_id, worker_id="w1")

        restarted = self._kernel()
        picked = Scheduler(
            restarted, policy=SchedulingPolicy(max_concurrency_per_tenant=1)
        ).select(limit=10)
        self.assertEqual(picked, [])

    def test_resource_labels_still_filter_after_a_restart(self) -> None:
        self._kernel().submit(make_task(resource_requirement=ResourceReq(labels=("gpu",))))

        restarted = self._kernel()
        self.assertEqual(
            Scheduler(restarted).select(
                limit=10,
                capability=WorkerCapability(
                    executors=frozenset({"native"}), labels=frozenset()
                ),
            ),
            [],
        )

    def test_a_kernel_without_a_task_store_refuses_instead_of_fabricating(self) -> None:
        """真库上的同一条拒绝：没接仓储 = 不知道这活要干什么。"""
        execution = self._kernel().submit(make_task())
        bare = self._kernel(tasks=None)
        with self.assertRaises(InvariantViolation) as ctx:
            bare.task_of(execution.execution_id)
        self.assertIn("E-26", str(ctx.exception))

    # ------------------------------------------------------------ E-28：写一次
    def test_resubmitting_after_the_execution_row_was_lost_works(self) -> None:
        kernel = self._kernel()
        task = make_task(priority=4)
        execution = kernel.submit(task)

        cur = self.conn.cursor()
        cur.execute("DELETE FROM executions WHERE execution_id = %s", (execution.execution_id,))

        kernel.submit(task)
        self.assertEqual(self._tasks(), 1)
        self.assertEqual(self._executions(), 1)

    def test_the_task_row_keeps_the_first_write(self) -> None:
        """`ON CONFLICT (task_id) DO NOTHING`：重演不改原件。"""
        kernel = self._kernel()
        task = make_task(priority=4)
        kernel.submit(task)

        store = PostgresTaskRepository(self.conn)
        store.add(Task(**{**task.__dict__, "priority": 99}))
        self.assertEqual(store.get(task.task_id).priority, 4)

    def test_get_many_reads_a_whole_batch(self) -> None:
        first = self._kernel()
        for _ in range(10):
            first.submit(make_task())

        store = PostgresTaskRepository(self.conn)
        cur = self.conn.cursor()
        cur.execute("SELECT task_id FROM tasks")
        ids = [r["task_id"] for r in cur.fetchall()]
        self.assertEqual(len(store.get_many(ids)), 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
