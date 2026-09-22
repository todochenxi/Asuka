"""M22：组合根 + `apps/worker` + 进程入口。

    PR-14  组合根唯一：除了 `apps/_bootstrap.py`，没有第二个地方知道客户端长什么样
    PR-15  客户端必须惰性导入 —— 顶层 import 会让 490 个测试在 import 阶段全红
    PR-16  配置不许静默兜底：缺 PG 是"变错"，必须拒绝启动；缺 Redis 是"变慢"，允许
    PR-17  Worker 不另设领地表 —— Lease 本身就是领地
    PR-18  Scheduler 不是部署单元：派活 = worker tick 内的一次 Atomic Claim

每条不变量配一个控制组。
"""
from __future__ import annotations

import re
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import apps._bootstrap as bootstrap
from apps._bootstrap import (
    ConfigurationError,
    RuntimeConfig,
    build_executors,
    build_outbox_publisher,
    build_worker,
)
from apps._runtime import ManualStop, ProcessState
from apps.worker import WorkerApp, WorkerProcessConfig
from packages.agent_domain.execution import ExecutionStatus
from packages.execution_kernel import (
    ExecutionKernel,
    KernelConfig,
    ManualClock,
    Scheduler,
    Worker,
    WorkerConfig,
)
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
)
from packages.execution_kernel.worker import WorkerOutcome

from .helpers import make_task
from .sqlite_shim import connect, load_schema_sql

ROOT = Path(__file__).resolve().parents[2]
_NO_SLEEP = lambda _seconds: None  # noqa: E731

#: PR-14：这些**客户端**只许出现在组合根里，而且是惰性出现。
#: 客户端有很多个（每个进程都可能自己连一次库），所以必须单点。
_CLIENT_PATTERN = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(psycopg2?|redis|kafka|confluent_kafka|sqlalchemy)\b",
    re.MULTILINE,
)

#: PR-22：这些**框架**只许出现在 `apps/api/app.py`。
#: 框架只有一个（只有 HTTP 进程需要它），所以单点即可，但同样要被断言。
_FRAMEWORK_PATTERN = re.compile(
    r"^\s*(?:import|from)\s+(fastapi|uvicorn|starlette|pydantic)\b",
    re.MULTILINE,
)


def _python_sources() -> list[Path]:
    out: list[Path] = []
    # examples/ 也在扫描范围内：它和 apps/ 一样是会被真正 import 的代码，
    # 不一样的是它更容易被人当成"随便写写的地方"。
    for folder in ("apps", "packages", "examples"):
        out.extend(sorted((ROOT / folder).rglob("*.py")))
    return out


def _base_env(**overrides: str) -> dict[str, str]:
    env = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# PR-14 / PR-15：组合根唯一 + 惰性导入
# ---------------------------------------------------------------------------


class CompositionRootTest(unittest.TestCase):
    def test_pr14_no_module_statically_imports_a_client(self) -> None:
        """整个代码库对 psycopg / redis / kafka 零静态依赖。

        所以"647 个测试在没有装任何数据库客户端的机器上也能跑"是**事实**，
        不是"我们尽量不依赖"。
        """
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in _python_sources()
            if _CLIENT_PATTERN.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_pr22_framework_binding_is_single_sited(self) -> None:
        """PR-22：fastapi / uvicorn 只出现在 `apps/api/app.py` 一处。

        客户端（PR-14）与框架（PR-22）分开判，是因为它们的风险不同：
        客户端有很多个，每个进程都可能自己连一次库，所以必须收到组合根；
        框架只有一个，只有 HTTP 进程需要它，所以单点即可 ——
        但"单点"这件事同样要被断言，不能因为是"只有一个"就不检查。
        """
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in _python_sources()
            if _FRAMEWORK_PATTERN.search(path.read_text(encoding="utf-8"))
            and path.relative_to(ROOT).as_posix() != "apps/api/app.py"
        ]
        self.assertEqual(offenders, [])

    def test_pr22_the_control_the_scanners_really_see_the_framework(self) -> None:
        """控制组：扫描器不是因为写错了正则才什么都没扫到。"""
        sample = "import os\nimport fastapi\nfrom uvicorn import run\n"
        self.assertEqual(sorted(_FRAMEWORK_PATTERN.findall(sample)), ["fastapi", "uvicorn"])

    def test_pr22_the_route_file_really_does_bind_the_framework(self) -> None:
        """控制组：路由文件**确实**绑定了框架 —— 上一条不是因为文件空才通过。

        fastapi 是显式 import；uvicorn 走 `importlib`（PR-15 的惰性要求），
        所以扫描器看不到它 —— 这里直接查源码，别让"扫描器看不见"
        变成"其实根本没有"。
        """
        source = (ROOT / "apps" / "api" / "app.py").read_text(encoding="utf-8")
        self.assertIn("fastapi", _FRAMEWORK_PATTERN.findall(source))
        self.assertIn('importlib.import_module("uvicorn")', source)

    def test_pr14_the_control_the_scanner_really_matches(self) -> None:
        """控制组：扫描器不是因为写错了正则才什么都没扫到。"""
        sample = "import os\nimport psycopg\nfrom redis import Redis\n"
        self.assertEqual(
            _CLIENT_PATTERN.findall(sample), ["psycopg", "redis"]
        )

    def test_pr15_bootstrap_has_no_client_in_its_namespace(self) -> None:
        """PR-15：组合根 import 成功，但 psycopg / redis / kafka 都不在它身上。

        顶层 import 的话，`import apps._bootstrap` 会在没装客户端的机器上直接炸。
        """
        for name in ("psycopg", "psycopg2", "redis", "kafka"):
            self.assertFalse(hasattr(bootstrap, name), name)

    def test_pr15_the_control_a_missing_client_gives_an_actionable_error(self) -> None:
        """控制组：缺客户端时给的是"能照着做"的提示，不是裸 ImportError。"""
        with self.assertRaises(ConfigurationError) as ctx:
            bootstrap._import_client("no_such_client_xyz", "pip install whatever")
        self.assertIn("pip install whatever", str(ctx.exception))
        self.assertIn("no_such_client_xyz", str(ctx.exception))


# ---------------------------------------------------------------------------
# PR-16：配置不许静默兜底
# ---------------------------------------------------------------------------


class RuntimeConfigTest(unittest.TestCase):
    def test_pr16_a_missing_pg_dsn_refuses_to_start(self) -> None:
        """缺 PG = 变错（什么都不持久却看起来在跑）→ 拒绝启动。"""
        with self.assertRaises(ConfigurationError) as ctx:
            RuntimeConfig.from_env({})
        self.assertIn("AGENTOS_PG_DSN", str(ctx.exception))

    def test_pr16_the_control_a_configured_dsn_is_accepted(self) -> None:
        """控制组：上一条不是"from_env 永远抛"。"""
        config = RuntimeConfig.from_env(_base_env())
        self.assertEqual(config.pg_dsn, "postgresql://localhost/agentos")

    def test_pr16_a_missing_redis_url_is_allowed(self) -> None:
        """缺 Redis = 变慢（Lease 索引没了，退回 PG 全扫）→ 允许缺省。"""
        config = RuntimeConfig.from_env(_base_env())
        self.assertEqual(config.redis_url, "")
        self.assertIsNone(bootstrap.redis_client(config))

    def test_pr16_the_control_a_configured_redis_is_used(self) -> None:
        """控制组：上一条的 None 不是"redis 分支压根没实现"。"""
        config = RuntimeConfig.from_env(_base_env(AGENTOS_REDIS_URL="redis://x:6379/0"))
        self.assertEqual(config.redis_url, "redis://x:6379/0")
        with self.assertRaises(ConfigurationError):
            bootstrap.redis_client(config)      # 走到惰性导入才失败 → 分支是活的

    def test_pr16_a_missing_kafka_broker_refuses_to_start(self) -> None:
        """Outbox publisher 没有 broker 就不是"降级"，是没法干活。"""
        config = RuntimeConfig.from_env(_base_env())
        with self.assertRaises(ConfigurationError) as ctx:
            bootstrap.kafka_producer(config)
        self.assertIn("AGENTOS_KAFKA_BROKERS", str(ctx.exception))

    def test_pr16_heartbeat_longer_than_the_lease_is_refused_at_config_time(self) -> None:
        """心跳 ≥ 租约要在**配置期**就炸，而不是等第一次心跳才暴露。"""
        with self.assertRaises(ConfigurationError) as ctx:
            RuntimeConfig.from_env(
                _base_env(
                    AGENTOS_HEARTBEAT_SECONDS="40",
                    AGENTOS_LEASE_TTL_SECONDS="30",
                )
            )
        self.assertIn("must be shorter", str(ctx.exception))

    def test_pr16_the_control_the_same_pair_is_fine_when_ordered(self) -> None:
        config = RuntimeConfig.from_env(
            _base_env(AGENTOS_HEARTBEAT_SECONDS="10", AGENTOS_LEASE_TTL_SECONDS="30")
        )
        self.assertLess(config.heartbeat_interval, config.lease_ttl)

    def test_pr16_there_is_no_default_executor(self) -> None:
        """没有声明执行器提供方就拒绝启动 —— 而不是返回一张空表。

        空表会让每个 Task 以 `EXECUTOR_NOT_FOUND`（PERMANENT）失败，
        报错看起来像业务坏了，排查方向从一开始就错。
        """
        config = RuntimeConfig.from_env(_base_env())
        with self.assertRaises(ConfigurationError) as ctx:
            build_executors(config)
        self.assertIn("AGENTOS_EXECUTOR_PROVIDER", str(ctx.exception))


# ---------------------------------------------------------------------------
# apps/worker：第一个真正产出东西的进程
# ---------------------------------------------------------------------------


class RecordingExecutor:
    """把跑过的 Task 记下来，然后成功返回。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, task: Any, ctx: Any) -> Mapping[str, Any]:
        self.calls.append(task.task_id)
        return {"echo": task.task_id}


class ExplodingExecutor:
    def execute(self, task: Any, ctx: Any) -> Mapping[str, Any]:
        raise RuntimeError("tool exploded")


def worker_harness(executor, *, poll_limit: int = 1):
    conn = connect(schema_sql=load_schema_sql("001_kernel.sql"))
    clock = ManualClock()
    kernel = ExecutionKernel(
        repository=PostgresExecutionRepository(conn),
        outbox=PostgresOutboxStore(conn),
        clock=clock,
        attempts=PostgresAttemptRepository(conn),
        config=KernelConfig(default_lease_ttl=timedelta(seconds=30)),
    )
    worker = Worker(
        kernel=kernel,
        scheduler=Scheduler(kernel),
        executors={"native": executor},
        config=WorkerConfig(
            worker_id="w1",
            lease_ttl=timedelta(seconds=30),
            heartbeat_interval=timedelta(seconds=10),
            poll_limit=poll_limit,
        ),
    )
    app = WorkerApp(
        worker=worker,
        config=WorkerProcessConfig(poll_limit=poll_limit),
        clock=clock,
        sleep=_NO_SLEEP,
    )
    return app, kernel, clock, conn


class WorkerProcessTest(unittest.TestCase):
    def test_a_task_is_actually_executed_end_to_end(self) -> None:
        """第一次：一个 Task 被一个**进程**真正跑完了。

        不是 `worker.run_once()` 被调用了一次（那在 test_worker.py 里早就有），
        而是 `ProcessRuntime` 驱动它、PG 落库、状态走到 COMPLETED。
        """
        executor = RecordingExecutor()
        app, kernel, _clock, conn = worker_harness(executor)
        self.addCleanup(conn.close)

        execution = kernel.submit(make_task())
        report = app.run(max_ticks=3)

        self.assertEqual(report.work, 1)
        self.assertEqual(executor.calls, [execution.task_id])
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.COMPLETED)

    def test_the_control_without_the_process_nothing_runs(self) -> None:
        """控制组：光有 Worker 不跑，Task 就一直是 PENDING。

        所以上一条的 COMPLETED 是进程跑出来的，不是提交时就完成的。
        """
        executor = RecordingExecutor()
        app, kernel, _clock, conn = worker_harness(executor)
        self.addCleanup(conn.close)

        execution = kernel.submit(make_task())
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.PENDING)
        self.assertEqual(executor.calls, [])

    def test_a_failing_task_counts_as_work_not_as_idle(self) -> None:
        """失败也是"处理过"，不能当成空闲去退避。

        否则一个持续失败的 Task 会让 worker 越睡越久，
        而队列看起来是"有活的"。
        """
        app, kernel, _clock, conn = worker_harness(ExplodingExecutor())
        self.addCleanup(conn.close)

        sleeps: list[float] = []
        app.runtime.sleep = sleeps.append
        execution = kernel.submit(make_task())
        app.run(max_ticks=1)

        self.assertEqual(app.runtime.work, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(
            kernel.status_of(execution.execution_id), ExecutionStatus.FAILED
        )

    def test_pr17_the_worker_holds_no_territory_of_its_own(self) -> None:
        """PR-17：Worker 没有领地表，所以退出时也没有东西要归还。

        Lease 本身就是领地（Atomic Claim + fencing_token），
        再开一张表等于给同一个问题两套答案。
        """
        app, _kernel, _clock, conn = worker_harness(RecordingExecutor())
        self.addCleanup(conn.close)
        self.assertIsNone(app.runtime.on_drain)

    def test_pr18_scheduling_is_not_a_separate_process(self) -> None:
        """PR-18：派活发生在 worker 自己的 tick 里，没有第二个进程参与。

        `apps/scheduler/` 不在磁盘上，而且不该出现：
        独立 scheduler 是一个中心（它挂了全员停摆），
        且它要通知 worker 就必须走消息 —— 那正是 §36 禁止的"Kafka 当任务队列"。
        """
        self.assertFalse((ROOT / "apps" / "scheduler").exists())
        app, _kernel, _clock, conn = worker_harness(RecordingExecutor())
        self.addCleanup(conn.close)
        # 派活能力是 Kernel 的一项，被 worker 直接调用，不是被远程调用
        self.assertIs(app.worker.scheduler.kernel, app.worker.kernel)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


class EntrypointTest(unittest.TestCase):
    def _run(self, build, *, max_ticks: int | None = None) -> int:
        from apps._entrypoint import run_process

        return run_process(build, max_ticks=max_ticks)

    def test_a_stopped_process_exits_zero(self) -> None:
        app, _kernel, _clock, conn = worker_harness(RecordingExecutor())
        self.addCleanup(conn.close)
        self.assertEqual(self._run(lambda: app, max_ticks=3), 0)

    def test_a_failed_process_exits_non_zero(self) -> None:
        """PR-8：撑不住了必须让编排层看见 —— 退出码是它唯一看得懂的语言。"""

        class _Broken:
            def run(self, *, max_ticks=None):
                from apps._runtime import ProcessReport

                return ProcessReport(name="x", state=ProcessState.FAILED, last_error="boom")

        self.assertEqual(self._run(lambda: _Broken()), 1)

    def test_a_configuration_error_exits_before_anything_starts(self) -> None:
        """配置错了就别起来 —— 起来一个连不上库的进程比起不来危险。"""

        def _bad():
            raise ConfigurationError("AGENTOS_PG_DSN is required")

        self.assertEqual(self._run(_bad), 2)

    def test_the_control_a_crash_during_run_is_not_swallowed(self) -> None:
        """控制组：崩溃不以 0 退出 —— 返回 0 的崩溃会被编排层当成正常结束。"""

        class _Crashing:
            def run(self, *, max_ticks=None):
                raise RuntimeError("unexpected")

        self.assertEqual(self._run(lambda: _Crashing()), 1)

    def test_pr7_the_backoff_cap_holds_for_a_very_long_idle_run(self) -> None:
        """PR-7 的例外：上限必须截在**指数之前**，不是之后。

        写成 `min(idle_sleep * 2 ** streak, cap)` 时，空队列跑一晚上
        streak 到几万，`2 ** 几万` 先抛 OverflowError ——
        进程会在**最闲的时候**崩在一个跟业务毫无关系的地方。
        """
        from apps._runtime import ProcessRuntime

        sleeps: list[float] = []
        runtime = ProcessRuntime(
            name="idle", idle_sleep=0.05, max_idle_sleep=2.0, sleep=sleeps.append
        )
        runtime.run(lambda: 0, max_ticks=1200)

        self.assertEqual(len(sleeps), 1200)
        self.assertEqual(max(sleeps), 2.0)
        self.assertEqual(runtime.idle_streak, 1200)

    def test_builders_do_not_register_signal_handlers(self) -> None:
        """组合根不偷偷改全局状态：不传 signal 就是 ManualStop，不是 SignalStop。"""
        conn = connect(schema_sql=load_schema_sql("001_kernel.sql", "006_outbox_delivery.sql"))
        self.addCleanup(conn.close)
        config = RuntimeConfig.from_env(
            _base_env(AGENTOS_KAFKA_BROKERS="localhost:9092")
        )
        app = build_outbox_publisher(config, conn=conn, producer=object())
        self.assertIsInstance(app.runtime.signal, ManualStop)


# ---------------------------------------------------------------------------
# 组合根装配
# ---------------------------------------------------------------------------


class BuildWorkerTest(unittest.TestCase):
    def test_build_worker_wires_the_pg_backed_kernel(self) -> None:
        """组合根接的是 PG 实现，不是内存实现 —— 这是它唯一该做的事。"""
        conn = connect(schema_sql=load_schema_sql("001_kernel.sql"))
        self.addCleanup(conn.close)
        config = RuntimeConfig.from_env(_base_env())
        worker = build_worker(
            config,
            executors={"native": RecordingExecutor()},
            kernel=bootstrap.build_kernel(config, conn=conn, redis=None),
        )

        self.assertIsInstance(worker.kernel.repository, PostgresExecutionRepository)
        self.assertIsInstance(worker.kernel.outbox, PostgresOutboxStore)
        self.assertEqual(worker.config.lease_ttl, timedelta(seconds=30))
        self.assertIn("native", worker.executors)

    def test_worker_outcomes_are_visible_on_the_worker(self) -> None:
        """执行结果留在 worker 上，便于入口 / 探针读。"""
        app, kernel, _clock, conn = worker_harness(ExplodingExecutor())
        self.addCleanup(conn.close)
        execution = kernel.submit(make_task())
        app.run(max_ticks=1)
        self.assertEqual(
            app.worker.last_outcomes[execution.execution_id], WorkerOutcome.FAILED
        )
