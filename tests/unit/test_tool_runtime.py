"""M15 阶段 10：Tool Runtime（基线 §24 / §16）。

验证的重点是**边界有没有守住**：

    T-1  Tool Runtime 不做业务重试（防 3×3 双重重试）
    T-2  side_effect ≠ READ 的工具必须带 idempotency_key，漏传即硬拒绝
    T-3  Input Validation 失败是 PERMANENT
    T-4  Policy / Guardrail 不在 Tool Runtime 里（Harness 才是唯一拦截点）
    T-5  版本显式解析；默认版本不随注册顺序漂移
    T-6  不关心底层协议
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.execution.retry import FailureClass
from packages.agent_domain.execution.task import ExecutorType, Task, TaskType
from packages.agent_runtime.executors import ToolCallExecutor
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    SandboxedCommandInvoker,
    SandboxProfile,
    SideEffect,
    ToolCall,
    ToolExecutionError,
    ToolNotFoundError,
    ToolProtocol,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
    ToolSpec,
    ToolValidationError,
)
from packages.execution_kernel.inmemory import (
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import ExecutorError, Worker, WorkerConfig

from .test_agent_loop import ToolRegistry as LegacyRegistry
from .test_agent_loop import calculator


# ---------------------------------------------------------------- 测试替身


@dataclass
class RecordingInvoker:
    """记录每次调用，用来验证 T-1（不被重试）与 T-2（key 透传）。"""

    result: Mapping[str, Any] | None = None
    error: Exception | None = None
    calls: list[ToolCall] = None                          # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def invoke(self, call: ToolCall) -> Mapping[str, Any]:
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return dict(self.result or {"ok": True})


def spec(name: str, version: str = "1.0.0", **kw) -> ToolSpec:
    return ToolSpec(name=name, version=version, **kw)


def registry_with(name: str, version: str, invoker, **kw) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(spec(name, version, **kw), invoker)
    return reg


# ============================================================ Resolution（T-5）


class ResolutionTest(unittest.TestCase):
    def test_missing_tool_is_permanent(self) -> None:
        runtime = ToolRuntime(ToolRegistry())
        with self.assertRaises(ToolNotFoundError):
            runtime.call("nope", {})

    def test_t5_default_version_is_the_first_registered(self) -> None:
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        reg.register(spec("q", "2.0.0"), RecordingInvoker())
        self.assertEqual(reg.default_version("q"), "1.0.0")
        self.assertEqual(reg.resolve("q").version, "1.0.0")

    def test_t5_switching_default_is_explicit(self) -> None:
        """换默认版本必须明说 —— 工具升级是业务事件，不能让注册顺序决定。"""
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        reg.register(spec("q", "2.0.0"), RecordingInvoker(), make_default=True)
        self.assertEqual(reg.default_version("q"), "2.0.0")

    def test_t5_later_registration_does_not_move_the_default(self) -> None:
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        reg.register(spec("q", "2.0.0"), RecordingInvoker())
        reg.register(spec("q", "3.0.0"), RecordingInvoker())
        self.assertEqual(reg.default_version("q"), "1.0.0")

    def test_explicit_version_wins(self) -> None:
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        reg.register(spec("q", "2.0.0"), RecordingInvoker())
        self.assertEqual(reg.resolve("q", "2.0.0").version, "2.0.0")

    def test_unknown_version_is_not_found(self) -> None:
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        with self.assertRaises(ToolNotFoundError):
            reg.resolve("q", "9.9.9")

    def test_duplicate_registration_is_rejected(self) -> None:
        """热加载时无声覆盖一个工具版本是最难排查的事故之一。"""
        reg = ToolRegistry()
        reg.register(spec("q", "1.0.0"), RecordingInvoker())
        with self.assertRaises(ValueError):
            reg.register(spec("q", "1.0.0"), RecordingInvoker())

    def test_result_records_the_actual_version(self) -> None:
        """请求 latest 时"请求的版本"和"实际跑的版本"是两个值。

        记成请求值，审计就无从追溯 —— 和 Model Gateway 的 model_id 是同一个坑。
        """
        reg = ToolRegistry()
        reg.register(spec("q", "2.0.0"), RecordingInvoker())
        result = ToolRuntime(reg).call("q", {})
        self.assertEqual(result.version, "2.0.0")


# ============================================================ Validation（T-3）


class ValidationTest(unittest.TestCase):
    def _runtime(self) -> ToolRuntime:
        schema = {
            "required": ["expr"],
            "properties": {"expr": {"type": "string"}, "n": {"type": "integer"}},
        }
        return ToolRuntime(registry_with("calc", "1.0.0", RecordingInvoker(), input_schema=schema))

    def test_missing_required_field(self) -> None:
        with self.assertRaises(ToolValidationError) as cm:
            self._runtime().call("calc", {})
        self.assertIn("missing required field: expr", cm.exception.problems)

    def test_wrong_type(self) -> None:
        with self.assertRaises(ToolValidationError) as cm:
            self._runtime().call("calc", {"expr": 42})
        self.assertTrue(any("expected string" in p for p in cm.exception.problems))

    def test_boolean_is_not_an_integer(self) -> None:
        """Python 里 bool 是 int 的子类，但 JSON 里它们不是一回事。"""
        with self.assertRaises(ToolValidationError):
            self._runtime().call("calc", {"expr": "1+1", "n": True})

    def test_valid_input_passes(self) -> None:
        result = self._runtime().call("calc", {"expr": "1+1", "n": 3})
        self.assertEqual(result.output, {"ok": True})

    def test_non_mapping_args(self) -> None:
        with self.assertRaises(ToolValidationError):
            self._runtime().call("calc", "not-a-mapping")        # type: ignore[arg-type]


# ============================================================ T-2 幂等键


class IdempotencyTest(unittest.TestCase):
    def test_t2_write_tool_without_key_is_rejected(self) -> None:
        """漏传去重键的后果是重复下单 / 重复扣款 —— 这是最贵的一类 bug，所以硬拒绝。"""
        runtime = ToolRuntime(
            registry_with("place_order", "1.0.0", RecordingInvoker(), side_effect=SideEffect.WRITE)
        )
        with self.assertRaises(ToolExecutionError) as cm:
            runtime.call("place_order", {"sku": "X"})
        self.assertEqual(cm.exception.code, "IDEMPOTENCY_KEY_REQUIRED")
        self.assertFalse(cm.exception.retryable)   # 重试也没 key

    def test_t2_unknown_side_effect_also_requires_key(self) -> None:
        runtime = ToolRuntime(
            registry_with("opaque", "1.0.0", RecordingInvoker(), side_effect=SideEffect.UNKNOWN)
        )
        with self.assertRaises(ToolExecutionError):
            runtime.call("opaque", {})

    def test_read_tool_does_not_require_key(self) -> None:
        runtime = ToolRuntime(
            registry_with("calc", "1.0.0", RecordingInvoker(), side_effect=SideEffect.READ)
        )
        self.assertEqual(runtime.call("calc", {}).idempotency_key, "")

    def test_key_is_passed_through_to_the_invoker(self) -> None:
        """§16：Tool Runtime 负责透传，外部系统按 key 去重。"""
        invoker = RecordingInvoker()
        runtime = ToolRuntime(
            registry_with("place_order", "1.0.0", invoker, side_effect=SideEffect.WRITE)
        )
        runtime.call("place_order", {"sku": "X"}, idempotency_key="exec_123")
        self.assertEqual(invoker.calls[0].idempotency_key, "exec_123")

    def test_function_invoker_can_receive_the_key(self) -> None:
        seen: dict[str, Any] = {}

        def place(args: Mapping[str, Any], *, idempotency_key: str = "") -> Mapping[str, Any]:
            seen["key"] = idempotency_key
            return {"order_id": "o1"}

        reg = ToolRegistry()
        reg.register(
            spec("place_order", "1.0.0", side_effect=SideEffect.WRITE),
            FunctionInvoker(place, pass_idempotency_key=True),
        )
        result = ToolRuntime(reg).call("place_order", {}, idempotency_key="exec_9")
        self.assertEqual(seen["key"], "exec_9")
        self.assertEqual(result.idempotency_key, "exec_9")


# ============================================================ T-1 / T-4 / T-6


class BoundaryTest(unittest.TestCase):
    def test_t1_runtime_does_not_retry(self) -> None:
        """T-1：失败就是失败，重试交给 Kernel。这里再试一层就是 3×3 双重重试。"""
        invoker = RecordingInvoker(error=RuntimeError("boom"))
        runtime = ToolRuntime(registry_with("flaky", "1.0.0", invoker))
        with self.assertRaises(ToolExecutionError):
            runtime.call("flaky", {})
        self.assertEqual(len(invoker.calls), 1)          # 只调了一次

    def test_t1_failure_is_transient_by_default(self) -> None:
        invoker = RecordingInvoker(error=RuntimeError("boom"))
        runtime = ToolRuntime(registry_with("flaky", "1.0.0", invoker))
        with self.assertRaises(ToolExecutionError) as cm:
            runtime.call("flaky", {})
        self.assertTrue(cm.exception.retryable)          # 让 Kernel 去决定重不重试

    def test_t4_runtime_does_not_judge_policy(self) -> None:
        """T-4 的可断言形式：Runtime 字段里没有 policy / guardrail / rate limit。"""
        names = {f.name for f in fields(ToolRuntime)}
        for forbidden in ("policy", "guardrail", "rate_limit", "harness"):
            self.assertNotIn(forbidden, names)

    def test_t4_tool_runtime_does_not_import_harness(self) -> None:
        """包级别边界：连 import 都没有，自然不可能自己判准入。"""
        import pathlib

        import packages.agent_runtime.tool_runtime as tr

        for path in sorted(pathlib.Path(tr.__file__).parent.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("import ") or s.startswith("from "):
                    self.assertNotIn("agent_harness", s, f"{path.name}: {s}")
                    self.assertNotIn("execution_kernel", s, f"{path.name}: {s}")

    def test_t6_protocol_is_just_a_field(self) -> None:
        """T-6：换个协议不需要换 Runtime —— 只换 Invoker。

        ⚠️ M101 收窄了它**一处**：`protocol=SANDBOX` 必须配一个
        `SandboxedInvoker` —— 声明了沙箱就得真的跑在沙箱里（见下一条）。
        """
        for protocol in ToolProtocol:
            if protocol is ToolProtocol.SANDBOX:
                continue
            reg = ToolRegistry()
            reg.register(spec("t", "1.0.0", protocol=protocol), RecordingInvoker())
            result = ToolRuntime(reg).call("t", {})
            self.assertIs(result.protocol, protocol)

    def test_a_declared_sandbox_needs_a_real_sandbox_invoker(self) -> None:
        """M101：`protocol=sandbox` 配裸 Invoker 是句谎话 —— 注册期拒绝。"""
        reg = ToolRegistry()
        with self.assertRaises(ValueError) as ctx:
            reg.register(
                spec("t", "1.0.0", protocol=ToolProtocol.SANDBOX), RecordingInvoker()
            )
        self.assertIn("sandbox", str(ctx.exception).lower())

        # 配了真的沙箱 Invoker 就能注册（协议字段本身照常保留）
        reg.register(
            spec("t", "1.0.0", protocol=ToolProtocol.SANDBOX),
            SandboxedCommandInvoker(profile=SandboxProfile()),
        )
        self.assertTrue(reg.has("t"))


class TimeoutTest(unittest.TestCase):
    def test_effective_timeout_is_the_smallest(self) -> None:
        invoker = RecordingInvoker()
        reg = ToolRegistry()
        reg.register(spec("slow", "1.0.0", timeout=timedelta(seconds=10)), invoker)
        runtime = ToolRuntime(reg, default_timeout=timedelta(seconds=60))
        runtime.call("slow", {}, timeout=timedelta(seconds=5))
        self.assertEqual(invoker.calls[0].timeout, timedelta(seconds=5))

    def test_spec_timeout_caps_the_default(self) -> None:
        invoker = RecordingInvoker()
        reg = ToolRegistry()
        reg.register(spec("slow", "1.0.0", timeout=timedelta(seconds=10)), invoker)
        runtime = ToolRuntime(reg, default_timeout=timedelta(seconds=60))
        runtime.call("slow", {})
        self.assertEqual(invoker.calls[0].timeout, timedelta(seconds=10))


# ============================================================ Executor 翻译 + Kernel


@dataclass
class FakeContext:
    task: Task
    idempotency_key: str = "exec_fake"
    heartbeats: int = 0

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def check_cancelled(self) -> bool:
        return False


class ExecutorTranslationTest(unittest.TestCase):
    def test_tool_not_found_is_permanent(self) -> None:
        executor = ToolCallExecutor(ToolRuntime(ToolRegistry()))
        with self.assertRaises(ExecutorError) as cm:
            executor.execute(self._task("nope"), self._ctx())
        self.assertIs(cm.exception.failure_class, FailureClass.PERMANENT)

    def test_validation_failure_is_permanent(self) -> None:
        reg = ToolRegistry()
        reg.register(
            spec("calc", "1.0.0", input_schema={"required": ["expr"]}),
            RecordingInvoker(),
        )
        executor = ToolCallExecutor(ToolRuntime(reg))
        with self.assertRaises(ExecutorError) as cm:
            executor.execute(self._task("calc", {}), self._ctx())
        self.assertIs(cm.exception.failure_class, FailureClass.PERMANENT)

    def test_execution_failure_is_transient(self) -> None:
        reg = ToolRegistry()
        reg.register(spec("flaky", "1.0.0"), RecordingInvoker(error=RuntimeError("boom")))
        executor = ToolCallExecutor(ToolRuntime(reg))
        with self.assertRaises(ExecutorError) as cm:
            executor.execute(self._task("flaky"), self._ctx())
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT)

    def test_idempotency_key_comes_from_the_execution_context(self) -> None:
        """§16 全链路：Kernel 的 key → ExecutionContext → ToolRuntime → Invoker。"""
        invoker = RecordingInvoker()
        reg = ToolRegistry()
        reg.register(spec("place_order", "1.0.0", side_effect=SideEffect.WRITE), invoker)
        executor = ToolCallExecutor(ToolRuntime(reg))
        result = executor.execute(self._task("place_order"), self._ctx(key="exec_77"))
        self.assertEqual(invoker.calls[0].idempotency_key, "exec_77")
        self.assertEqual(result["idempotency_key"], "exec_77")

    def test_legacy_registry_still_works(self) -> None:
        """旧的进程内 ToolRegistry 走同一条代码路径（不留第二条）。"""
        legacy = LegacyRegistry()
        legacy.register("calculator", calculator)
        executor = ToolCallExecutor(legacy)
        result = executor.execute(self._task("calculator", {"expr": "6*7"}), self._ctx())
        self.assertEqual(result["result"]["value"], 42)
        self.assertIsInstance(executor.runtime, ToolRuntime)

    # ------------------------------------------------------------ 装配
    def _task(self, tool: str, args: Mapping[str, Any] | None = None) -> Task:
        return Task(
            run_id="run_tr",
            step_id="step_tr",
            task_type=TaskType.TOOL_CALL,
            executor_type=ExecutorType.NATIVE,
            payload={"tool": tool, "args": args if args is not None else {"expr": "1+1"}},
            timeout=timedelta(seconds=30),
        )

    def _ctx(self, *, key: str = "exec_fake") -> FakeContext:
        return FakeContext(self._task("x"), idempotency_key=key)


class KernelIntegrationTest(unittest.TestCase):
    """T-1 在 Kernel 侧的表现：工具失败只产生 1 个 Attempt，不是 N 个。"""

    def test_one_failed_tool_call_is_one_attempt(self) -> None:
        invoker = RecordingInvoker(error=RuntimeError("boom"))
        reg = ToolRegistry()
        reg.register(spec("flaky", "1.0.0"), invoker)

        clock = ManualClock()
        kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )
        worker = Worker(
            kernel=kernel,
            scheduler=Scheduler(kernel),
            executors={"native": ToolCallExecutor(ToolRuntime(reg))},
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )
        execution = kernel.submit(
            Task(
                run_id="run_tr",
                step_id="step_tr",
                task_type=TaskType.TOOL_CALL,
                payload={"tool": "flaky", "args": {}},
                timeout=timedelta(seconds=30),
            )
        )
        worker.run_once(limit=1)

        self.assertEqual(len(invoker.calls), 1)                    # Runtime 没有自己重试
        self.assertEqual(
            len(kernel.attempts.list_by_execution(execution.execution_id)), 1
        )                                                          # Kernel 也只记了一个


if __name__ == "__main__":
    unittest.main()
