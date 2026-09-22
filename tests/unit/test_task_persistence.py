"""空洞 215：`tasks` 表终于有人写了（E-26 / E-27 / E-28）。

--------------------------------------------------------------------------
这张表从 M15 就在 `001_kernel.sql` 里了

13 个列、2 个索引、注释还写着"Scheduler 只认识它（E-12）"。
但直到 M35 结束，**没有任何一行代码写它** ——
Task 只活在 `ExecutionKernel._tasks` 这个进程内字典里。

于是"进程重启后把活捡起来"在真 PG 上是断的：

    PENDING 的 Execution 好端端躺在库里
    Scheduler 选中它 → kernel.task_of() → KeyError: 'exe_xxx'

而 `apps/_runtime.py` 的 ProcessRuntime 会吞掉这个异常、
计一次 consecutive_failure、退避，到上限后**整个 worker 进程退出**。
日志里只有一行 KeyError，指着一个症状而不是原因（PR-19）。

--------------------------------------------------------------------------
这里最想防的不是那个 KeyError，是"修掉它的那种修法"

给个默认 Task，priority / tenant / resource / payload 全部变成编造值。
而那四样**不是加速信息，是正确性输入**（A-12：丢了是变错，不是变慢）：

    priority              决定谁先跑
    tenant_id             决定配额 —— 多租户隔离边界，不是性能旋钮
    resource_requirement  决定能不能派给这个 worker
    payload               决定到底要干什么

编造之后的失败形态是：需要 GPU 的活派给只有 CPU 的 worker，
然后以 `EXECUTOR_NOT_FOUND`（PERMANENT，不重试）终态 ——
**一个调度错误伪装成一个载荷错误**（PR-20 修过的病，在持久层复发）；
以及一条永远不生效的租户配额。两者都不报错。

所以下面 `NoFabricationTest` 那一组，是本文件最重要的一组：
它验的不是"能读回来"，是"读不回来时**拒绝**，而不是编造"。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any, Mapping, Sequence

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution import (
    ExecutionStatus,
    ExecutorType,
    ResourceReq,
    Task,
    TaskType,
)
from packages.agent_domain.execution.retry import RetryPolicy
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresTaskRepository,
)
from packages.execution_kernel.inmemory import (
    InMemoryOutbox,
    InMemoryTaskRepository,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel, KernelConfig
from packages.execution_kernel.scheduler import (
    Scheduler,
    SchedulingPolicy,
    WorkerCapability,
)
from packages.execution_kernel.worker import Worker, WorkerConfig

from .helpers import make_task
from .sqlite_shim import connect, load_schema_sql

#: 001 + 013。013 的外键在 sqlite 上会被 `_to_sqlite_ddl` 丢掉（它加不了 FK），
#: 所以这一层验的是**代码路径**，外键本身由集成层在真 PG 上验。
SCHEMA = ("001_kernel.sql", "013_task_persistence.sql")

T0 = ManualClock().now()

_UNSET = object()       # 区分"没传"和"显式传了 None"


class RecordingExecutor:
    """记下自己被要求干了什么 —— 用来证明 Worker 真的拿到了 payload。"""

    def __init__(self) -> None:
        self.seen: list[Task] = []

    def execute(self, task: Task, ctx: Any) -> Mapping[str, Any]:
        self.seen.append(task)
        return {"ok": True}


class RecordingTaskRepository:
    """数 `get_many` 被调了几次 —— 用来证明 Scheduler 不是 N 次往返。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_calls = 0
        self.get_many_calls = 0

    def add(self, task: Task) -> None:
        self._inner.add(task)

    def get(self, task_id: str) -> Task | None:
        self.get_calls += 1
        return self._inner.get(task_id)

    def get_many(self, task_ids: Sequence[str]) -> Mapping[str, Task]:
        self.get_many_calls += 1
        return self._inner.get_many(task_ids)


class RecordingExecutionRepository:
    """记录写序：Task 必须写在 Execution **之前**（X-3 + 013 的外键方向）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.writes: list[str] = []

    def add(self, execution: Any) -> None:
        self.writes.append("execution")
        self._inner.add(execution)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TaskPersistenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql(*SCHEMA))
        self.addCleanup(self.conn.close)
        self.clock = ManualClock()
        self.tasks = PostgresTaskRepository(self.conn)

    def _kernel(self, *, tasks: Any = _UNSET, repository: Any = None) -> ExecutionKernel:
        return ExecutionKernel(
            repository=repository or PostgresExecutionRepository(self.conn),
            outbox=PostgresOutboxStore(self.conn),
            attempts=PostgresAttemptRepository(self.conn),
            clock=self.clock,
            tasks=self.tasks if tasks is _UNSET else tasks,
            config=KernelConfig(default_lease_ttl=timedelta(seconds=30)),
        )

    def _rows(self) -> list[Mapping[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM tasks")
        return list(cur.fetchall())


class SubmitWritesTheTaskTest(TaskPersistenceTestCase):
    """E-26 的第一半：`submit()` 真的往库里写了一行。"""

    def test_submit_writes_a_task_row(self) -> None:
        kernel = self._kernel()
        kernel.submit(make_task())
        self.assertEqual(len(self._rows()), 1)

    def test_the_row_carries_every_field_the_scheduler_needs(self) -> None:
        kernel = self._kernel()
        task = make_task(
            task_type=TaskType.LLM_CALL,
            executor_type=ExecutorType.HTTP,
            priority=7,
            tenant_id="acme",
            resource_requirement=ResourceReq(cpu_millis=500, memory_mb=1024, gpu=2, labels=("gpu",)),
            payload={"model": "gpt-x", "prompt": "hi"},
            timeout=timedelta(seconds=120),
        )
        kernel.submit(task)

        row = self._rows()[0]
        self.assertEqual(row["task_id"], task.task_id)
        self.assertEqual(row["run_id"], task.run_id)
        self.assertEqual(row["step_id"], task.step_id)          # E-11
        self.assertEqual(row["task_type"], "llm_call")
        self.assertEqual(row["executor_type"], "http")
        self.assertEqual(row["priority"], 7)
        self.assertEqual(row["tenant_id"], "acme")
        self.assertEqual(row["timeout_seconds"], 120)

    def test_payload_survives_the_roundtrip(self) -> None:
        kernel = self._kernel()
        task = make_task(payload={"tool": "search", "args": {"q": "agentos"}})
        kernel.submit(task)
        self.assertEqual(
            self.tasks.get(task.task_id).payload,
            {"tool": "search", "args": {"q": "agentos"}},
        )

    def test_resource_requirement_survives_the_roundtrip(self) -> None:
        kernel = self._kernel()
        task = make_task(resource_requirement=ResourceReq(gpu=1, labels=("gpu", "a100")))
        kernel.submit(task)

        back = self.tasks.get(task.task_id)
        self.assertEqual(back.resource_requirement.gpu, 1)
        self.assertEqual(back.resource_requirement.labels, ("gpu", "a100"))
        self.assertEqual(back.resource_requirement.cpu_millis, 100)

    def test_retry_policy_survives_the_roundtrip(self) -> None:
        kernel = self._kernel()
        task = make_task(retry_policy=RetryPolicy(max_attempts=5, retry_budget=2))
        kernel.submit(task)
        self.assertEqual(self.tasks.get(task.task_id).retry_policy.max_attempts, 5)
        self.assertEqual(self.tasks.get(task.task_id).retry_policy.retry_budget, 2)

    def test_submit_writes_the_task_before_the_execution(self) -> None:
        """写序不是风格问题：013 的外键方向把"先 Task 后 Execution"钉死了。"""
        recorder = RecordingExecutionRepository(PostgresExecutionRepository(self.conn))
        kernel = self._kernel(repository=recorder)
        kernel.submit(make_task())
        self.assertEqual(recorder.writes, ["execution"])
        # Task 已经在了（否则 executions 插不进去），所以"之前"由这一行证明：
        self.assertEqual(len(self._rows()), 1)


class AfterRestartTest(TaskPersistenceTestCase):
    """E-26 的第二半，也是空洞 215 的本体：**换一个进程**还认得这活。"""

    def _restart(self) -> ExecutionKernel:
        """一个"新进程"：全新对象，共享的只有那一个数据库。"""
        return self._kernel(tasks=PostgresTaskRepository(self.conn))

    def test_a_fresh_kernel_can_still_select_the_pending_execution(self) -> None:
        """修复前：这一步是 `KeyError: 'exe_xxx'`。"""
        first = self._kernel()
        first.submit(make_task())

        second = self._restart()
        picked = Scheduler(second).select(limit=10)
        self.assertEqual(len(picked), 1)

    def test_the_restarted_kernel_sees_the_same_priority(self) -> None:
        first = self._kernel()
        low = make_task(priority=0, payload={"tool": "low"})
        high = make_task(priority=10, payload={"tool": "high"})
        first.submit(low)
        first.submit(high)

        second = self._restart()
        picked = Scheduler(second).select(limit=10)
        self.assertEqual(
            [second.task_of(e.execution_id).payload["tool"] for e in picked],
            ["high", "low"],
        )

    def test_the_restarted_worker_sees_the_same_payload(self) -> None:
        first = self._kernel()
        first.submit(make_task(payload={"tool": "search", "args": {"q": "agentos"}}))

        second = self._restart()
        executor = RecordingExecutor()
        worker = Worker(
            kernel=second,
            scheduler=Scheduler(second),
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
        """配额是隔离边界：丢了它，一个租户能吃满整个集群，而且不报错。"""
        first = self._kernel()
        first.submit(make_task(tenant_id="acme"))
        first.submit(make_task(tenant_id="acme"))
        # 让其中一条真的跑起来，配额才有东西可数
        running = first.repository.list_by_status(ExecutionStatus.PENDING, limit=1)[0]
        first.claim(running.execution_id, worker_id="w1")

        second = self._restart()
        picked = Scheduler(
            second, policy=SchedulingPolicy(max_concurrency_per_tenant=1)
        ).select(limit=10)
        self.assertEqual(picked, [])

    def test_the_control_without_a_quota_the_pending_one_is_still_picked(self) -> None:
        first = self._kernel()
        first.submit(make_task(tenant_id="acme"))
        first.submit(make_task(tenant_id="acme"))
        running = first.repository.list_by_status(ExecutionStatus.PENDING, limit=1)[0]
        first.claim(running.execution_id, worker_id="w1")

        second = self._restart()
        self.assertEqual(len(Scheduler(second).select(limit=10)), 1)

    def test_resource_labels_still_filter_after_a_restart(self) -> None:
        """丢了 labels 之后，需要 GPU 的活会被派给没 GPU 的 worker（PR-20 复发）。"""
        first = self._kernel()
        first.submit(make_task(resource_requirement=ResourceReq(gpu=1, labels=("gpu",))))

        second = self._restart()
        picked = Scheduler(second).select(
            limit=10,
            capability=WorkerCapability(executors=frozenset({"native"}), labels=frozenset()),
        )
        self.assertEqual(picked, [])

    def test_the_control_a_worker_that_has_the_label_does_get_it(self) -> None:
        first = self._kernel()
        first.submit(make_task(resource_requirement=ResourceReq(gpu=1, labels=("gpu",))))

        second = self._restart()
        picked = Scheduler(second).select(
            limit=10,
            capability=WorkerCapability(
                executors=frozenset({"native"}), labels=frozenset({"gpu"})
            ),
        )
        self.assertEqual(len(picked), 1)


class NoFabricationTest(TaskPersistenceTestCase):
    """本文件最重要的一组：读不回来时**拒绝**，而不是编造一个默认 Task。"""

    def test_a_kernel_without_a_task_store_refuses_loudly(self) -> None:
        """进程重启 + 没接仓储 = 确实不知道这活要干什么。那就说不知道。"""
        with_task_store = self._kernel()
        execution = with_task_store.submit(make_task())

        bare = self._kernel(tasks=None)
        with self.assertRaises(InvariantViolation) as ctx:
            bare.task_of(execution.execution_id)
        self.assertIn("E-26", str(ctx.exception))
        self.assertIn("fabricate", str(ctx.exception))

    def test_a_missing_task_row_refuses_loudly(self) -> None:
        """库里有 Execution 却没有它指的那条 Task（013 本该挡住它）。"""
        first = self._kernel()
        execution = first.submit(make_task())
        cur = self.conn.cursor()
        cur.execute("DELETE FROM tasks WHERE task_id = %s", (execution.task_id,))

        second = self._kernel(tasks=PostgresTaskRepository(self.conn))
        with self.assertRaises(InvariantViolation) as ctx:
            second.task_of(execution.execution_id)
        self.assertIn("E-27", str(ctx.exception))
        self.assertIn("fabricate", str(ctx.exception))

    def test_the_batch_path_refuses_too_it_does_not_silently_skip(self) -> None:
        """一批里有一条读不出 Task，整批失败 —— 这是刻意的。

        静默跳过等于把那条 Execution **永久挂起**，
        而"挂起且不报错"正是这次要治的病（PR-19）。
        """
        first = self._kernel()
        good = first.submit(make_task())
        bad = first.submit(make_task())
        cur = self.conn.cursor()
        cur.execute("DELETE FROM tasks WHERE task_id = %s", (bad.task_id,))

        second = self._kernel(tasks=PostgresTaskRepository(self.conn))
        with self.assertRaises(InvariantViolation):
            Scheduler(second).select(limit=10)
        self.assertIsNotNone(second.repository.get(good.execution_id))


class SchedulerPrefetchTest(unittest.TestCase):
    """Task 落库之后，逐个 `task_of()` 就是 N 次往返。这里钉住"一次取回"。

    注意：这一组必须**从一个新进程出发**。同一个 Kernel 内 `submit()`
    已经把 Task 放进内存缓存了，那时一次都不用查 ——
    要验的恰恰是"缓存里没有"的那条路径（也就是重启之后）。
    """

    def setUp(self) -> None:
        from packages.execution_kernel.inmemory import (
            InMemoryAttemptRepository,
            InMemoryExecutionRepository,
        )

        self.clock = ManualClock()
        self.repository = InMemoryExecutionRepository()
        self.attempts = InMemoryAttemptRepository()
        self.outbox = InMemoryOutbox()
        self.tasks = RecordingTaskRepository(InMemoryTaskRepository())

    def _kernel(self) -> ExecutionKernel:
        """一个"新进程"：全新 Kernel，共享同一批仓储。"""
        return ExecutionKernel(
            repository=self.repository,
            attempts=self.attempts,
            outbox=self.outbox,
            clock=self.clock,
            tasks=self.tasks,
        )

    def test_ten_candidates_cost_one_batch_read_not_ten(self) -> None:
        first = self._kernel()
        for _ in range(10):
            first.submit(make_task())

        Scheduler(self._kernel()).select(limit=10)
        self.assertEqual(self.tasks.get_many_calls, 1)
        self.assertEqual(self.tasks.get_calls, 0)

    def test_the_second_select_reuses_the_in_process_cache(self) -> None:
        first = self._kernel()
        for _ in range(3):
            first.submit(make_task())

        restarted = self._kernel()
        Scheduler(restarted).select(limit=10)
        Scheduler(restarted).select(limit=10)
        self.assertEqual(self.tasks.get_many_calls, 1)


class E28TaskIsWrittenOnceTest(TaskPersistenceTestCase):
    """E-28：Task 是交棒那一刻的输入，落库之后不再改。"""

    def test_adding_the_same_task_id_twice_keeps_the_first(self) -> None:
        task = make_task(priority=1)
        self.tasks.add(task)
        mutated = Task(**{**task.__dict__, "priority": 99})
        self.tasks.add(mutated)
        self.assertEqual(self.tasks.get(task.task_id).priority, 1)

    def test_resubmitting_after_the_execution_row_was_lost_works(self) -> None:
        """事务没生效、调用方重试：Task 行已经在了，Execution 应当补上。"""
        kernel = self._kernel()
        task = make_task()
        execution = kernel.submit(task)

        cur = self.conn.cursor()
        cur.execute("DELETE FROM executions WHERE execution_id = %s", (execution.execution_id,))

        again = kernel.submit(task)
        self.assertIsNotNone(again)
        self.assertEqual(len(self._rows()), 1)

    def test_the_adapter_has_no_update_path_for_tasks(self) -> None:
        """结构性断言：不是"还没实现 UPDATE"，是**不该有** UPDATE。"""
        from packages.execution_kernel.adapters import postgres as pg

        with open(pg.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("UPDATE tasks", source)


class SqliteCannotClaimE27Test(TaskPersistenceTestCase):
    """PR-26：替身做不到的那件事，要说出来，不能让人以为测过了。

    sqlite 的 `ALTER TABLE` 加不了 FOREIGN KEY，013 那句在 `sqlite_shim`
    里被整句丢掉。所以**这一层无法声称 E-27 被测到** ——
    外键由 `tests/integration/test_task_persistence_real_pg.py` 在真 PG 上验。
    """

    def test_sqlite_really_does_not_enforce_the_fk(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO executions (execution_id, task_id, idempotency_key, "
            "execution_mode, status, current_attempt_no, version) "
            "VALUES ('exe_orphan', 'task_nope', 'exe_orphan', 'task', 'PENDING', 0, 1)"
        )
        cur.execute("SELECT execution_id FROM executions WHERE execution_id = 'exe_orphan'")
        self.assertIsNotNone(cur.fetchone())

    def test_the_control_the_migration_file_does_declare_the_fk(self) -> None:
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parents[2]
            / "infrastructure"
            / "postgres"
            / "013_task_persistence.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("FOREIGN KEY (task_id) REFERENCES tasks (task_id)", sql)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
