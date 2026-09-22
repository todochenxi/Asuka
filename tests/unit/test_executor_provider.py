"""M23：把真执行器接进组合根。

    PR-19  分派是两级的：executor_type（传输）× task_type（语义）。
           缺 handler 一律报 EXECUTOR_NOT_FOUND 并**点名两个轴**，
           不许退化成 BAD_PAYLOAD（那是把路由错误伪装成载荷错误）
    PR-20  能力声明有两个维度。`WorkerCapability.task_types` 缺失的话，
           声明干不了的活儿会被派下来炸，而不是留在队列里等能干的人
    PR-21  能力一律从**真实装配出来的表**推导，不另行声明；
           `AGENTOS_TASK_TYPES` 只是配置期的**期望**，用来断言而不是用来声明

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any, Mapping

from apps._bootstrap import ConfigurationError, RuntimeConfig, build_executors, build_worker
from examples.demo_stack import build_model_gateway, build_tool_runtime
from packages.agent_domain.execution import ExecutionStatus, ExecutorType
from packages.agent_domain.execution.task import TaskType
from packages.agent_runtime.executors import (
    ApprovalGateExecutor,
    DeferringExecutor,
    ExecutorCoverage,
    LLMCallExecutor,
    SkillExecutor,
    AgentDelegationExecutor,
    TaskTypeRouter,
    ToolCallExecutor,
    executor_coverage,
)
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
    Scheduler,
    Worker,
    WorkerCapability,
    WorkerConfig,
)
from packages.execution_kernel.worker import ExecutionContext, ExecutorError

from .helpers import make_task

#: 真 provider 的三条环境变量。`examples/demo_stack` 走的是和真实部署
#: **完全相同**的装载路径（importlib + getattr），不是测试旁路。
_ENV = {
    "AGENTOS_PG_DSN": "postgresql://localhost/agentos",
    "AGENTOS_EXECUTOR_PROVIDER": "apps.executor_provider:build_executors",
    "AGENTOS_TOOL_PROVIDER": "examples.demo_stack:build_tool_runtime",
    "AGENTOS_MODEL_PROVIDER": "examples.demo_stack:build_model_gateway",
    "AGENTOS_TASK_TYPES": "tool_call,llm_call,human_approval",
}


def _config(**overrides: str) -> RuntimeConfig:
    env = dict(_ENV)
    env.update(overrides)
    return RuntimeConfig.from_env(env)


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _ctx(task: Any) -> ExecutionContext:
    """Executor 只需要这两个能力；测试里都是空实现。"""
    return ExecutionContext(
        execution_id="exec_1",
        attempt_no=1,
        task=task,
        heartbeat=lambda: None,
        cancellation=_NeverCancelled(),     # type: ignore[arg-type]
        idempotency_key="exec_1",
    )


def _result_of(worker: Worker, execution_id: str) -> Mapping[str, Any]:
    """结果挂在 Attempt 上（E-4：Attempt 是执行的历史，不是 Execution）。"""
    attempts = worker.kernel.attempts.list_by_execution(execution_id)
    return dict(attempts[-1].result or {})


# ---------------------------------------------------------------------------
# PR-19：两级分派
# ---------------------------------------------------------------------------


class TaskTypeRouterTest(unittest.TestCase):
    def test_pr19_routes_on_task_type_within_one_executor_type(self) -> None:
        """同一个 `native` 下，tool_call 与 human_approval 走不同的执行器。"""
        tool = ToolCallExecutor(build_tool_runtime())
        gate = ApprovalGateExecutor()
        router = TaskTypeRouter(
            {TaskType.TOOL_CALL: tool, TaskType.HUMAN_APPROVAL: gate},
            executor_type="native",
        )
        task = make_task(
            task_type=TaskType.TOOL_CALL,
            executor_type=ExecutorType.NATIVE,
            payload={"tool": "echo", "args": {"text": "hi"}},
        )
        result = router.execute(task, _ctx(task))
        self.assertEqual(result["tool"], "echo")
        self.assertEqual(result["result"], {"echo": "hi"})

    def test_pr19_a_missing_handler_names_both_axes(self) -> None:
        """缺 handler 时报 EXECUTOR_NOT_FOUND，且消息里**两个轴都点名**。"""
        router = TaskTypeRouter(
            {TaskType.TOOL_CALL: ToolCallExecutor(build_tool_runtime())},
            executor_type="native",
        )
        task = make_task(task_type=TaskType.SKILL, executor_type=ExecutorType.NATIVE)
        with self.assertRaises(ExecutorError) as cm:
            router.execute(task, _ctx(task))
        self.assertEqual(cm.exception.code, "EXECUTOR_NOT_FOUND")
        message = cm.exception.message
        self.assertIn("task_type='skill'", message)
        self.assertIn("executor_type='native'", message)

    def test_pr19_the_control_without_a_router_the_error_is_a_lie(self) -> None:
        """控制组：没有路由器时，同一个 Task 报的是 BAD_PAYLOAD。

        这是 M23 修掉的那个洞的**原样复现**：路由错误被伪装成载荷错误，
        于是排障的人会去查"payload 是谁填的"。
        """
        bare = ToolCallExecutor(build_tool_runtime())
        task = make_task(task_type=TaskType.HUMAN_APPROVAL, executor_type=ExecutorType.NATIVE)
        with self.assertRaises(ExecutorError) as cm:
            bare.execute(task, _ctx(task))
        self.assertEqual(cm.exception.code, "BAD_PAYLOAD")
        self.assertIn("payload.tool is required", cm.exception.message)

    def test_pr19_the_control_the_lie_is_permanent_so_it_never_retries(self) -> None:
        """控制组的控制组：那个假错误还是 PERMANENT —— 连重试的机会都没有。"""
        bare = ToolCallExecutor(build_tool_runtime())
        task = make_task(task_type=TaskType.HUMAN_APPROVAL, executor_type=ExecutorType.NATIVE)
        with self.assertRaises(ExecutorError) as cm:
            bare.execute(task, _ctx(task))
        self.assertEqual(cm.exception.failure_class.value, "permanent")

    def test_pr19_router_accepts_bare_strings_as_keys(self) -> None:
        """`TaskType` 是 str enum，裸字符串也必须能对上（配置里写的就是字符串）。"""
        router = TaskTypeRouter(
            {"tool_call": ToolCallExecutor(build_tool_runtime())},
            executor_type="native",
        )
        self.assertTrue(router.handles(TaskType.TOOL_CALL))
        self.assertTrue(router.handles("tool_call"))
        self.assertFalse(router.handles("llm_call"))

    def test_pr19_approval_gate_refuses_honestly(self) -> None:
        """闸门不由 Worker 决定 —— 它说的是这件事，而不是 payload 缺字段。"""
        task = make_task(task_type=TaskType.HUMAN_APPROVAL, executor_type=ExecutorType.NATIVE)
        with self.assertRaises(ExecutorError) as cm:
            ApprovalGateExecutor().execute(task, _ctx(task))
        self.assertEqual(cm.exception.code, "GATE_NOT_WORKER_EXECUTABLE")
        self.assertIn("H-4", cm.exception.message)

    def test_pr19_handler_is_reachable_so_l2_stays_assertable(self) -> None:
        """L-2 要穿过路由器才能断言"共享同一批对象"，否则它只是句注释。"""
        gateway = build_model_gateway()
        router = TaskTypeRouter(
            {TaskType.LLM_CALL: LLMCallExecutor(gateway)}, executor_type="http"
        )
        self.assertIs(router.handler(TaskType.LLM_CALL).gateway, gateway)
        self.assertIsNone(router.handler(TaskType.TOOL_CALL))


# ---------------------------------------------------------------------------
# PR-20：能力过滤的第二个维度
# ---------------------------------------------------------------------------


def _kernel_with_tasks(*tasks) -> tuple[ExecutionKernel, Worker, ManualClock]:
    clock = ManualClock()
    kernel = ExecutionKernel(
        repository=InMemoryExecutionRepository(),
        attempts=InMemoryAttemptRepository(),
        outbox=InMemoryOutbox(),
        clock=clock,
    )
    for task in tasks:
        kernel.submit(task)
    return kernel, None, clock  # type: ignore[return-value]


class CapabilityFilteringTest(unittest.TestCase):
    def _build(self, capability, *tasks):
        clock = ManualClock()
        kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )
        for task in tasks:
            kernel.submit(task)
        worker = Worker(
            kernel=kernel,
            scheduler=Scheduler(kernel),
            executors={"native": ToolCallExecutor(build_tool_runtime())},
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
            capability=capability,
        )
        return worker

    def test_pr20_a_task_type_the_worker_cannot_do_is_not_dispatched(self) -> None:
        """声明只干 tool_call → `native:skill` 根本不会被派过来。"""
        skill = make_task(task_type=TaskType.SKILL, executor_type=ExecutorType.NATIVE)
        worker = self._build(
            WorkerCapability(
                executors=frozenset({"native"}),
                task_types=frozenset({"tool_call"}),
            ),
            skill,
        )
        self.assertEqual(worker.dispatch_once(limit=5), [])

    def test_pr20_the_control_without_task_types_it_is_dispatched(self) -> None:
        """控制组：只看 executor_type 的话，同一个 Task **会**被派下来。

        它被派下来之后就会以 BAD_PAYLOAD（PERMANENT）炸掉 ——
        这正是 M23 之前 `native:skill` / `native:human_approval` 的命运。
        """
        skill = make_task(task_type=TaskType.SKILL, executor_type=ExecutorType.NATIVE)
        worker = self._build(
            WorkerCapability(executors=frozenset({"native"})), skill
        )
        self.assertEqual(len(worker.dispatch_once(limit=5)), 1)

    def test_pr20_a_declared_task_type_is_still_dispatched(self) -> None:
        """过滤不能过严：声明了 tool_call，tool_call 就得能派。"""
        tool = make_task(
            task_type=TaskType.TOOL_CALL,
            executor_type=ExecutorType.NATIVE,
            payload={"tool": "echo", "args": {"text": "x"}},
        )
        worker = self._build(
            WorkerCapability(
                executors=frozenset({"native"}),
                task_types=frozenset({"tool_call"}),
            ),
            tool,
        )
        self.assertEqual(len(worker.dispatch_once(limit=5)), 1)

    def test_pr20_executor_type_is_still_checked(self) -> None:
        """新增维度不能顶掉旧维度：`http` 的 Task 依然不会被 native worker 派走。"""
        llm = make_task(task_type=TaskType.LLM_CALL, executor_type=ExecutorType.HTTP)
        worker = self._build(
            WorkerCapability(
                executors=frozenset({"native"}),
                task_types=frozenset({"llm_call"}),
            ),
            llm,
        )
        self.assertEqual(worker.dispatch_once(limit=5), [])


# ---------------------------------------------------------------------------
# 覆盖计算
# ---------------------------------------------------------------------------


class CoverageTest(unittest.TestCase):
    def test_bare_executors_cover_nothing(self) -> None:
        """裸执行器只认 executor_type → 不算覆盖任何 (executor_type, task_type)。

        这里刻意不给它兜底：兜底就等于承认"两个维度可以不对齐"。
        """
        table = {"native": ToolCallExecutor(build_tool_runtime())}
        self.assertEqual(executor_coverage(table).covered, frozenset())

    def test_unrouted_is_computed_from_action_to_task(self) -> None:
        """`unrouted` 来自 ACTION_TO_TASK —— 洞是被算出来的，不是被记住的。"""
        coverage = executor_coverage({})
        self.assertIn(("native", "tool_call"), coverage.unrouted)
        self.assertIn(("native", "human_approval"), coverage.unrouted)
        self.assertIn(("http", "llm_call"), coverage.unrouted)

    def test_m25_the_two_holes_are_now_routed(self) -> None:
        """M25 把 `native:skill` / `agent_runtime:agent_delegation` 补上了。

        这条是从 `test_the_real_table_leaves_only_two_holes` 改过来的 ——
        那条测试当初就是为了"有人补上时它会红"而写，现在它红了。
        """
        coverage = executor_coverage(build_executors(_config()))
        self.assertEqual(coverage.unrouted, frozenset())

    def test_pr24_the_control_routed_is_not_the_same_as_doable(self) -> None:
        """补上之后 unrouted 清零，但**系统仍然跑不了**这三件。

        这是 M25 最想钉住的一点：三个 handler 的全部内容就是拒绝。
        只看 `unrouted` 会把"做不到"读成"做到了" ——
        与 PR-24（版本号漂移）是同一类：**对外报的值与实际能做的事不是同一个**。
        """
        coverage = executor_coverage(build_executors(_config()))
        self.assertEqual(
            coverage.deferred,
            frozenset(
                {
                    ("native", "human_approval"),
                    ("native", "skill"),
                    ("agent_runtime", "agent_delegation"),
                }
            ),
        )
        # 真正"Worker 能干完"的只剩两格
        self.assertFalse(coverage.executable("native", "skill"))
        self.assertFalse(coverage.executable("agent_runtime", "agent_delegation"))
        self.assertTrue(coverage.executable("native", "tool_call"))
        self.assertTrue(coverage.executable("http", "llm_call"))

    def test_deferred_is_said_out_loud_at_startup(self) -> None:
        """拒绝型的格子必须被说出来，而且要带上"主人是谁"。"""
        from apps._bootstrap import describe_coverage

        message = describe_coverage(build_executors(_config()))
        self.assertIn("native:skill", message)
        self.assertIn("agent_runtime:agent_delegation", message)
        self.assertIn("NOT worker-executable", message)
        # 光说"不是 Worker 干的"没用 —— 得说清是谁干的
        self.assertIn("CHILD_SKILL", message)
        self.assertIn("CHILD_AGENT", message)

    def test_the_control_a_really_executable_table_says_nothing(self) -> None:
        """控制组：真的全能干时才一片安静 —— 说明报告不是硬编码总要说话。

        用非拒绝型的 stub 填满每一格，此时 deferred 与 unrouted 都为空。
        """
        from apps._bootstrap import describe_coverage

        class _Stub:
            def execute(self, task, ctx) -> Mapping[str, Any]:
                return {}

        self.assertNotIsInstance(_Stub(), DeferringExecutor)
        stub = _Stub()
        table = {
            "native": TaskTypeRouter(
                {
                    TaskType.TOOL_CALL: stub,
                    TaskType.HUMAN_APPROVAL: stub,
                    TaskType.SKILL: stub,
                },
                executor_type="native",
            ),
            "http": TaskTypeRouter(
                {TaskType.LLM_CALL: stub}, executor_type="http"
            ),
            "agent_runtime": TaskTypeRouter(
                {TaskType.AGENT_DELEGATION: stub}, executor_type="agent_runtime"
            ),
        }
        coverage = executor_coverage(table)
        self.assertEqual(coverage.unrouted, frozenset())
        self.assertEqual(coverage.deferred, frozenset())
        self.assertEqual(describe_coverage(table), "")

    def test_the_control_unrouted_still_says_pending(self) -> None:
        """控制组：连 handler 都没有的格子，报的是 PENDING（那才是对的后果）。"""
        from apps._bootstrap import describe_coverage

        table = {
            "native": TaskTypeRouter(
                {TaskType.TOOL_CALL: ToolCallExecutor(build_tool_runtime())},
                executor_type="native",
            ),
        }
        message = describe_coverage(table)
        self.assertIn("http:llm_call", message)
        self.assertIn("PENDING", message)

    def test_missing_reports_task_types_not_pairs(self) -> None:
        coverage = ExecutorCoverage(covered=frozenset({("native", "tool_call")}))
        self.assertEqual(coverage.missing(["tool_call", "llm_call"]), frozenset({"llm_call"}))


# ---------------------------------------------------------------------------
# PR-21：组合根的覆盖校验与能力推导
# ---------------------------------------------------------------------------


class BootstrapCoverageTest(unittest.TestCase):
    def test_pr21_a_declared_task_type_without_a_handler_refuses_to_start(self) -> None:
        """指望它干 llm_call，表里却没有 → 拒绝启动（PR-16 同一判据）。"""
        with self.assertRaises(ConfigurationError) as cm:
            build_executors(_config(AGENTOS_TASK_TYPES="tool_call,retrieval"))
        self.assertIn("retrieval", str(cm.exception))

    def test_pr21_the_control_the_same_table_starts_when_expectations_match(self) -> None:
        """控制组：同一张表，期望改成表里真有的，就能起来。"""
        table = build_executors(_config(AGENTOS_TASK_TYPES="tool_call,llm_call"))
        self.assertEqual(sorted(table), ["agent_runtime", "http", "native"])

    def test_pr21_capability_is_derived_from_the_real_table(self) -> None:
        """能力不是配出来的，是从装配结果读出来的 —— 两份声明就会漂移。"""
        worker = build_worker(
            _config(),
            executors=build_executors(_config()),
            kernel=_memory_kernel(),
        )
        assert worker.capability is not None
        self.assertEqual(
            worker.capability.task_types,
            frozenset(
                {
                    "tool_call",
                    "human_approval",
                    "llm_call",
                    "skill",
                    "agent_delegation",
                }
            ),
        )

    def test_pr21_no_executors_declared_means_take_the_whole_table(self) -> None:
        """没声明 `AGENTOS_EXECUTORS` 就接下整张表 —— 包括 http。

        默认只接 `{"native"}` 会发生什么：worker 明明装了 `http:llm_call`，
        却因为默认值里没有 http，**永远不派发 LLM 任务**。
        它空转、队列堆积、对外报健康 —— 默认把能力砍掉是 PR-16 那种错的近亲。
        """
        config = _config()
        self.assertEqual(config.executors, frozenset())
        worker = build_worker(
            config, executors=build_executors(config), kernel=_memory_kernel()
        )
        assert worker.capability is not None
        self.assertEqual(
            worker.capability.executors,
            frozenset({"native", "http", "agent_runtime"}),
        )
        self.assertIn("llm_call", worker.capability.task_types)
        self.assertIn("agent_delegation", worker.capability.task_types)

    def test_pr21_the_control_declaring_executors_narrows_capability(self) -> None:
        """控制组：显式声明 `AGENTOS_EXECUTORS=native` 确实会收窄 —— 它没坏。"""
        config = _config(AGENTOS_EXECUTORS="native")
        worker = build_worker(
            config, executors=build_executors(config), kernel=_memory_kernel()
        )
        assert worker.capability is not None
        self.assertEqual(worker.capability.executors, frozenset({"native"}))
        self.assertEqual(
            worker.capability.task_types,
            frozenset({"tool_call", "human_approval", "skill"}),
        )

    def test_pr21_the_control_a_bare_table_derives_no_task_types(self) -> None:
        """控制组：表里是裸执行器 → task_types 为空（= 不限），不是猜一个。"""
        worker = build_worker(
            _config(),
            executors={"native": ToolCallExecutor(build_tool_runtime())},
            kernel=_memory_kernel(),
        )
        assert worker.capability is not None
        self.assertEqual(worker.capability.task_types, frozenset())

    def test_a_provider_returning_an_empty_table_refuses_to_start(self) -> None:
        """空表 = 什么都不会的 worker，它会把每个 Task 永久拒掉还报健康。"""
        with self.assertRaises(ConfigurationError) as cm:
            build_executors(
                _config(AGENTOS_EXECUTOR_PROVIDER="tests.unit.fixtures.empty_provider:build")
            )
        self.assertIn("empty executor table", str(cm.exception))

    def test_tool_provider_must_return_a_real_tool_runtime(self) -> None:
        """返回裸 dict 也能"跑"，但 T-2（WRITE 要去重键）在那条路上不存在。"""
        with self.assertRaises(ConfigurationError) as cm:
            build_executors(
                _config(AGENTOS_TOOL_PROVIDER="tests.unit.fixtures.empty_provider:dict_tools")
            )
        self.assertIn("must return a ToolRuntime", str(cm.exception))


def _memory_kernel() -> ExecutionKernel:
    return ExecutionKernel(
        repository=InMemoryExecutionRepository(),
        attempts=InMemoryAttemptRepository(),
        outbox=InMemoryOutbox(),
        clock=ManualClock(),
    )


# ---------------------------------------------------------------------------
# 端到端：worker 第一次干真活
# ---------------------------------------------------------------------------


class RealExecutionTest(unittest.TestCase):
    def _worker(self) -> Worker:
        config = _config()
        return build_worker(
            config,
            executors=build_executors(config),
            kernel=_memory_kernel(),
            capability=None,
        )

    def test_a_real_tool_call_runs_through_the_real_tool_runtime(self) -> None:
        """不是 RecordingExecutor，是真的 ToolRuntime → 真的工具函数。"""
        worker = self._worker()
        worker.kernel.submit(
            make_task(
                task_type=TaskType.TOOL_CALL,
                executor_type=ExecutorType.NATIVE,
                payload={"tool": "echo", "args": {"text": "hello"}},
            )
        )
        outcomes = worker.run_once(limit=1)
        self.assertEqual(list(outcomes.values()), ["completed"])
        execution_id = next(iter(outcomes))
        execution = worker.kernel.repository.get(execution_id)
        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)
        result = _result_of(worker, execution_id)
        self.assertEqual(result.get("tool"), "echo")
        self.assertEqual(result.get("result"), {"echo": "hello"})

    def test_a_write_tool_carries_the_idempotency_key(self) -> None:
        """T-2 在最浅的那条路上也成立：去重键一路传到工具里。

        `ToolCallExecutor` 用的是 `execution_id`（跨 Attempt 稳定，§16）。
        """
        worker = self._worker()
        worker.kernel.submit(
            make_task(
                task_type=TaskType.TOOL_CALL,
                executor_type=ExecutorType.NATIVE,
                payload={"tool": "note.write", "args": {"text": "persisted"}},
            )
        )
        outcomes = worker.run_once(limit=1)
        execution_id = next(iter(outcomes))
        self.assertEqual(
            _result_of(worker, execution_id).get("idempotency_key"), execution_id
        )

    def test_a_real_llm_call_runs_through_the_real_gateway(self) -> None:
        """LLM 走 HTTP 那一格：真的 Gateway，不是 Fake。"""
        worker = self._worker()
        worker.kernel.submit(
            make_task(
                task_type=TaskType.LLM_CALL,
                executor_type=ExecutorType.HTTP,
                payload={"prompt": "hello", "model": "demo-1"},
            )
        )
        outcomes = worker.run_once(limit=1)
        self.assertEqual(list(outcomes.values()), ["completed"])
        result = _result_of(worker, next(iter(outcomes)))
        self.assertEqual(result.get("model"), "demo-1")
        self.assertIn("demo-1 received", result["response"]["text"])

    def test_an_unknown_tool_fails_permanently_not_by_retrying(self) -> None:
        """工具不存在 = PERMANENT。重试到天亮它也不会出现。"""
        worker = self._worker()
        worker.kernel.submit(
            make_task(
                task_type=TaskType.TOOL_CALL,
                executor_type=ExecutorType.NATIVE,
                payload={"tool": "nope", "args": {}},
            )
        )
        outcomes = worker.run_once(limit=1)
        self.assertEqual(list(outcomes.values()), ["failed"])


if __name__ == "__main__":
    unittest.main()
