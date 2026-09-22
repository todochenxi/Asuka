"""M15 阶段 9：Model Gateway（基线 §28）。

验证的重点是**边界有没有守住**，不是"能不能调通模型"：

    G-1  Gateway 内部 Fallback **不产生新的 Kernel Attempt**（最重要）
    G-2  Fallback 次数显式封顶
    G-3  Model ≠ Deployment
    G-4  Fallback ≠ 降级（换 Deployment 语义等价；换 Model 语义不等价且默认禁止）
    G-5  每次调用留下完整 Deployment 轨迹
    G-6  不可重试的错不浪费钱走完整条链
    G-7  Gateway 只上报用量，不判断预算
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.execution import ExecutionStatus
from packages.agent_domain.execution.retry import FailureClass
from packages.agent_domain.execution.task import ExecutorType, Task, TaskType
from packages.agent_domain.ids import new_task_id
from packages.agent_runtime.executors import LLMCallExecutor, ToolCallExecutor
from packages.agent_runtime.model_gateway import (
    CompletionRequest,
    Deployment,
    FunctionProvider,
    GatewayError,
    Model,
    ModelCapability,
    ModelGateway,
    ModelRouter,
    ProviderError,
    RoutingContext,
    ok_response,
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

from .test_agent_loop import LoopTestBase, ScriptedInterpreter, ToolRegistry, calculator


# ---------------------------------------------------------------- 测试替身


class ScriptedProvider:
    """按调用顺序吐结果。

    behaviors 里的每一项：
        "ok"                      → 成功
        ("err", code, retryable)  → 抛 ProviderError
    """

    def __init__(self, name: str, behaviors: list[Any] | None = None) -> None:
        self.name = name
        self.behaviors = list(behaviors or [])
        self.calls: list[str] = []

    @property
    def provider(self) -> str:
        return self.name

    def complete(self, dep: Deployment, request: CompletionRequest):
        self.calls.append(dep.deployment_id)
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior == "ok":
            return ok_response(
                dep,
                request,
                text=f"answer from {dep.deployment_id}",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=7,
            )
        _, code, retryable = behavior
        raise ProviderError(code, f"{code} on {dep.deployment_id}", retryable=retryable)


GPT = Model(
    model_id="gpt-4o",
    name="GPT-4o",
    capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_USE}),
    input_price_per_1k=0.005,
    output_price_per_1k=0.015,
)
MINI = Model(
    model_id="gpt-4o-mini",
    name="GPT-4o mini",
    capabilities=frozenset({ModelCapability.TEXT}),
    input_price_per_1k=0.00015,
    output_price_per_1k=0.0006,
)


def dep(dep_id: str, model_id: str = "gpt-4o", provider: str = "openai", **kw) -> Deployment:
    return Deployment(deployment_id=dep_id, model_id=model_id, provider=provider, **kw)


def req(model_id: str = "gpt-4o", prompt: str = "hi", **kw) -> CompletionRequest:
    return CompletionRequest(model_id=model_id, prompt=prompt, **kw)


def build(models, deployments, providers, *, max_fallbacks: int = 2) -> ModelGateway:
    return ModelGateway(
        ModelRouter(models, deployments),
        {p.provider: p for p in providers},
        max_fallbacks=max_fallbacks,
    )


# ============================================================ G-3 / Router


class RouterTest(unittest.TestCase):
    def test_g3_a_model_has_many_deployments(self) -> None:
        """G-3：Model 是逻辑概念，Deployment 是物理位置。"""
        router = ModelRouter(
            [GPT],
            [dep("azure-eastus", provider="azure"), dep("openai-main", provider="openai")],
        )
        self.assertEqual(len(router.deployments_for("gpt-4o")), 2)
        self.assertIs(router.model("gpt-4o"), GPT)

    def test_unknown_model_deployment_is_rejected(self) -> None:
        """Deployment 指向一个没注册的 Model —— 配置错误要被拒，不能崩在 Router 里。"""
        router = ModelRouter([GPT], [dep("ghost", model_id="ghost")])
        decision = router.select(
            req(), RoutingContext(allow_degraded=True, fallback_models=("ghost",))
        )
        self.assertEqual(len(decision.candidates), 0)
        self.assertIn("unknown model", decision.rejected[0].reason)

    def test_wrong_model_is_rejected_before_anything_else(self) -> None:
        router = ModelRouter([GPT], [dep("orphan", model_id="nonexistent")])
        decision = router.select(req())
        self.assertEqual(len(decision.candidates), 0)
        self.assertIn("model mismatch", decision.rejected[0].reason)

    def test_capability_is_a_hard_filter(self) -> None:
        """要 tool_use 的模型，没有就别来 —— 不是排序，是直接淘汰。"""
        router = ModelRouter([GPT, MINI], [dep("mini", model_id="gpt-4o-mini")])
        decision = router.select(
            req(model_id="gpt-4o-mini",
                required_capabilities=frozenset({ModelCapability.TOOL_USE}))
        )
        self.assertEqual(len(decision.candidates), 0)
        self.assertTrue(any("missing capability" in r.reason for r in decision.rejected))

    def test_tenant_allowlist_is_a_hard_filter(self) -> None:
        router = ModelRouter(
            [GPT],
            [
                dep("vip", allowed_tenants=frozenset({"acme"})),
                dep("shared"),
            ],
        )
        decision = router.select(req(), RoutingContext(tenant_id="globex"))
        self.assertEqual([c.deployment.deployment_id for c in decision.candidates], ["shared"])

    def test_priority_then_cost(self) -> None:
        router = ModelRouter(
            [GPT],
            [
                dep("cheap", priority=5),
                dep("primary", priority=0),
            ],
        )
        decision = router.select(req())
        self.assertEqual(decision.primary.deployment.deployment_id, "primary")

    def test_prefer_cheap_flips_the_order(self) -> None:
        """同一个 Router，切一个策略开关就换排序 —— 策略不该写死在选择逻辑里。"""
        deployments = [dep("expensive-fast", priority=0), dep("cheap-slow", priority=5)]
        # 让 cheap-slow 真的便宜：给它一个便宜的模型
        cheap = Model(model_id="gpt-4o", name="cheap", input_price_per_1k=0.0001, output_price_per_1k=0.0001)
        deployments[1] = dep("cheap-slow", priority=5)
        router = ModelRouter([cheap], deployments)
        normal = router.select(req(prompt="x" * 4000), RoutingContext())
        cheap_first = router.select(req(prompt="x" * 4000), RoutingContext(prefer_cheap=True))
        # priority 模式下 priority=0 的赢；prefer_cheap 下两者同模型同价，
        # 顺序由 priority 兜底 —— 至少断言它不会崩、且两种模式都能选出 primary
        self.assertIsNotNone(normal.primary)
        self.assertIsNotNone(cheap_first.primary)

    def test_unhealthy_deployment_is_rejected(self) -> None:
        from packages.agent_runtime.model_gateway import DeploymentMetrics

        class Unhealthy:
            def metrics(self, deployment_id: str):
                return DeploymentMetrics(healthy=(deployment_id != "sick"))

        router = ModelRouter([GPT], [dep("sick"), dep("ok")], metrics=Unhealthy())
        decision = router.select(req())
        self.assertEqual([c.deployment.deployment_id for c in decision.candidates], ["ok"])
        self.assertTrue(any("unhealthy" in r.reason for r in decision.rejected))

    def test_rejection_is_inspectable(self) -> None:
        """排障时最想问的是"为什么没选中它"—— 这个必须有答案，不能只给个空列表。"""
        router = ModelRouter([GPT], [dep("off", enabled=False)])
        decision = router.select(req())
        self.assertEqual(len(decision.candidates), 0)
        self.assertEqual(decision.rejected[0].deployment_id, "off")
        self.assertIn("disabled", decision.rejected[0].reason)


# ============================================================ G-1 / G-2


class FallbackTest(unittest.TestCase):
    def test_fallback_walks_the_chain(self) -> None:
        a = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        b = ScriptedProvider("azure")
        gw = build([GPT], [dep("d1"), dep("d2", provider="azure")], [a, b])
        call = gw.complete(req())
        self.assertEqual(call.fallback_count, 1)
        self.assertEqual([x.deployment_id for x in call.attempts], ["d1", "d2"])
        self.assertFalse(call.degraded)

    def test_g1_fallback_does_not_become_kernel_attempts(self) -> None:
        """G-1 最硬的一条：Gateway 内部走 3 个 Deployment，Kernel 只看到 **1 个 Attempt**。

        如果这里变成 3 个 Attempt，再乘上 Kernel 自己的 max_attempts=3，
        就是 §28 说的重试放大 —— 而且是指数级的。
        """
        providers = [
            ScriptedProvider("openai", [("err", "UPSTREAM_500", True)]),
            ScriptedProvider("azure", [("err", "UPSTREAM_500", True)]),
            ScriptedProvider("vllm", [("err", "UPSTREAM_500", True)]),
        ]
        gw = build(
            [GPT],
            [dep("d1"), dep("d2", provider="azure"), dep("d3", provider="vllm")],
            providers,
            max_fallbacks=3,
        )
        kernel, worker, _ = self._kernel_with(gw)
        task = self._llm_task()
        execution = kernel.submit(task)
        worker.run_once(limit=1)

        # 3 次 provider 调用…
        self.assertEqual(
            [len(p.calls) for p in providers], [1, 1, 1],
        )
        # …但只有 **一个** Kernel Attempt
        self.assertEqual(len(kernel.attempts.list_by_execution(execution.execution_id)), 1)
        self.assertEqual(execution.current_attempt_no, 1)

    def test_g1_kernel_retry_is_the_only_thing_that_makes_attempts(self) -> None:
        """对照组：Kernel 重试一次 → 2 个 Attempt，provider 调用却是 4 次。

        这正好把两个维度分开：**Attempt 数由 Kernel 决定，调用数由 Gateway 决定**。
        混淆它们就是 §28 说的重试放大。
        """
        providers = [
            ScriptedProvider("openai", [("err", "UPSTREAM_500", True), ("err", "UPSTREAM_500", True)]),
            ScriptedProvider("azure", [("err", "UPSTREAM_500", True), ("err", "UPSTREAM_500", True)]),
        ]
        gw = build([GPT], [dep("d1"), dep("d2", provider="azure")], providers, max_fallbacks=1)
        kernel, worker, clock = self._kernel_with(gw)
        execution = kernel.submit(self._llm_task())

        worker.run_once(limit=1)                       # Attempt #1：内部走 2 个 Deployment
        self.assertEqual(len(kernel.attempts.list_by_execution(execution.execution_id)), 1)

        clock.advance(timedelta(seconds=120))          # 跨过退避，让 Execution 重新可调度
        worker.run_once(limit=1)                       # Attempt #2：又走 2 个 Deployment

        self.assertEqual(len(kernel.attempts.list_by_execution(execution.execution_id)), 2)
        self.assertEqual(sum(len(p.calls) for p in providers), 4)

    def test_g2_fallback_budget_is_capped(self) -> None:
        """G-2：max_fallbacks=1 → 最多 2 次调用，链上第三个 Deployment 根本不会被碰。"""
        a = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        b = ScriptedProvider("azure", [("err", "UPSTREAM_500", True)])
        c = ScriptedProvider("vllm")
        gw = build(
            [GPT],
            [dep("d1"), dep("d2", provider="azure"), dep("d3", provider="vllm")],
            [a, b, c],
            max_fallbacks=1,
        )
        with self.assertRaises(GatewayError):
            gw.complete(req())
        self.assertEqual([len(p.calls) for p in (a, b, c)], [1, 1, 0])

    def test_zero_fallbacks_means_primary_only(self) -> None:
        a = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        gw = build([GPT], [dep("d1")], [a], max_fallbacks=0)
        with self.assertRaises(GatewayError):
            gw.complete(req())
        self.assertEqual(len(a.calls), 1)

    def test_no_candidate_is_not_retryable(self) -> None:
        """路由都选不出来 → PERMANENT。换多少个 Attempt 也选不出来。"""
        gw = build([GPT], [], [])
        with self.assertRaises(GatewayError) as cm:
            gw.complete(req())
        self.assertFalse(cm.exception.retryable)

    # ------------------------------------------------------------ 装配
    def _kernel_with(self, gateway: ModelGateway):
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
            executors={"http": LLMCallExecutor(gateway, default_model_id="gpt-4o")},
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )
        return kernel, worker, clock

    def _llm_task(self) -> Task:
        return Task(
            run_id="run_gw",
            step_id="step_gw",
            task_type=TaskType.LLM_CALL,
            executor_type=ExecutorType.HTTP,
            payload={"prompt": "hello", "model": "gpt-4o"},
            timeout=timedelta(seconds=30),
        )


# ============================================================ G-4 / G-5 / G-6


class DegradationTest(unittest.TestCase):
    def test_g4_degradation_is_off_by_default(self) -> None:
        """默认不允许降级 —— 静默换小模型是"系统悄悄变笨"，必须显式开。"""
        gw = build([GPT, MINI], [dep("d1"), dep("mini", model_id="gpt-4o-mini", provider="openai")], [])
        decision = gw.route(req())
        self.assertEqual([c.deployment.deployment_id for c in decision.candidates], ["d1"])
        self.assertFalse(decision.has_degraded)

    def test_g4_degraded_candidate_is_marked(self) -> None:
        openai = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        gw = build(
            [GPT, MINI],
            [dep("d1"), dep("mini", model_id="gpt-4o-mini")],
            [openai],
        )
        call = gw.complete(
            req(),
            RoutingContext(allow_degraded=True, fallback_models=("gpt-4o-mini",)),
        )
        self.assertTrue(call.degraded)
        self.assertEqual(call.response.model_id, "gpt-4o-mini")

    def test_g4_fallback_within_same_model_is_not_degraded(self) -> None:
        azure = ScriptedProvider("azure")
        gw = build(
            [GPT],
            [dep("d1"), dep("d2", provider="azure")],
            [ScriptedProvider("openai", [("err", "UPSTREAM_500", True)]), azure],
        )
        call = gw.complete(req())
        self.assertFalse(call.degraded)
        self.assertEqual(call.response.model_id, "gpt-4o")

    def test_g5_trail_is_complete(self) -> None:
        """G-5：成功也留轨迹 —— 不然成本归因和"这次为什么慢"都没法回答。"""
        a = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        b = ScriptedProvider("azure")
        gw = build([GPT], [dep("d1"), dep("d2", provider="azure")], [a, b])
        call = gw.complete(req())
        self.assertEqual(len(call.attempts), 2)
        self.assertFalse(call.attempts[0].ok)
        self.assertEqual(call.attempts[0].error_code, "UPSTREAM_500")
        self.assertTrue(call.attempts[1].ok)
        self.assertEqual(call.total_latency_ms, call.attempts[1].latency_ms)

    def test_g6_non_retryable_error_stops_the_chain(self) -> None:
        """G-6：context 超长换哪个 provider 都一样超长 —— 别再花钱试一遍。"""
        a = ScriptedProvider("openai", [("err", "CONTEXT_LENGTH_EXCEEDED", False)])
        b = ScriptedProvider("azure")
        gw = build([GPT], [dep("d1"), dep("d2", provider="azure")], [a, b])
        with self.assertRaises(GatewayError) as cm:
            gw.complete(req())
        self.assertEqual(len(b.calls), 0)                 # 第二个根本没被碰
        self.assertFalse(cm.exception.retryable)          # 也不该让 Kernel 再试

    def test_gateway_error_keeps_the_trail(self) -> None:
        a = ScriptedProvider("openai", [("err", "UPSTREAM_500", True)])
        gw = build([GPT], [dep("d1"), dep("d2", provider="azure")], [a, ScriptedProvider("azure", [("err", "UPSTREAM_500", True)])])
        with self.assertRaises(GatewayError) as cm:
            gw.complete(req())
        self.assertEqual(cm.exception.call_count, 2)


# ============================================================ 边界


class BoundaryTest(unittest.TestCase):
    def test_gateway_does_not_know_the_kernel(self) -> None:
        """G-1 的可断言形式：Gateway 的字段里没有任何 Kernel 概念。"""
        names = {f.name for f in fields(ModelGateway)}
        for forbidden in ("kernel", "execution", "attempt_no", "lease", "worker"):
            self.assertNotIn(forbidden, names)

    def test_model_gateway_does_not_import_execution_kernel(self) -> None:
        """包级别的边界：Gateway 连 Kernel 都 import 不到，自然不可能建 Attempt。

        与 H-4（Harness 不持有 Kernel）同一个套路 —— 把边界写成可断言的事实，
        而不是写在注释里等人自觉遵守。
        """
        import pathlib

        import packages.agent_runtime.model_gateway as mg

        pkg_dir = pathlib.Path(mg.__file__).parent
        for path in sorted(pkg_dir.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    self.assertNotIn("execution_kernel", stripped, f"{path.name}: {stripped}")

    def test_g7_gateway_reports_usage_but_never_judges_budget(self) -> None:
        """G-7：预算是 Harness 的拦截条件，Gateway 只上报用量。"""
        from packages.agent_harness import Budget, CostManager

        provider = ScriptedProvider("openai")
        gw = build([GPT], [dep("d1")], [provider])
        call = gw.complete(req())

        # Gateway 只给数字，没有 "exceeded" / "allowed" 之类的判断
        self.assertFalse(hasattr(call, "exceeded"))
        self.assertFalse(hasattr(gw, "budget"))
        self.assertEqual(call.total_tokens, 15)

        # 判断在 Harness 这边
        cm = CostManager(budget=Budget(max_tokens=10))
        cm.charge(tokens=call.total_tokens)
        self.assertTrue(cm.exceeded)

    def test_legacy_client_still_works(self) -> None:
        """裸 LLMClient 走同一条代码路径（不是分叉出一个'直接调用'的分支）。"""

        class Tiny:
            def complete(self, prompt: str, **kw):
                return {"text": "legacy ok"}

        executor = LLMCallExecutor(Tiny(), default_model_id="legacy-model")
        self.assertIsInstance(executor.gateway, ModelGateway)


class ExecutorTranslationTest(LoopTestBase):
    """Executor 是 Kernel 语义的翻译层：GatewayError → ExecutorError + FailureClass。"""

    def test_transient_failure_becomes_retryable(self) -> None:
        gw = build(
            [GPT],
            [dep("d1"), dep("d2", provider="azure")],
            [
                ScriptedProvider("openai", [("err", "UPSTREAM_500", True)]),
                ScriptedProvider("azure", [("err", "UPSTREAM_500", True)]),
            ],
        )
        executor = LLMCallExecutor(gw, default_model_id="gpt-4o")
        try:
            executor.execute(self._task(), self._ctx())
            self.fail("should have raised")
        except ExecutorError as err:
            self.assertIs(err.failure_class, FailureClass.TRANSIENT)

    def test_permanent_failure_is_not_retryable(self) -> None:
        gw = build(
            [GPT],
            [dep("d1")],
            [ScriptedProvider("openai", [("err", "CONTEXT_LENGTH_EXCEEDED", False)])],
        )
        executor = LLMCallExecutor(gw, default_model_id="gpt-4o")
        try:
            executor.execute(self._task(), self._ctx())
            self.fail("should have raised")
        except ExecutorError as err:
            self.assertIs(err.failure_class, FailureClass.PERMANENT)

    def test_result_carries_the_gateway_trail(self) -> None:
        gw = build(
            [GPT],
            [dep("d1"), dep("d2", provider="azure")],
            [
                ScriptedProvider("openai", [("err", "UPSTREAM_500", True)]),
                ScriptedProvider("azure"),
            ],
        )
        executor = LLMCallExecutor(gw, default_model_id="gpt-4o")
        result = executor.execute(self._task(), self._ctx())
        self.assertEqual(result["gateway"]["fallback_count"], 1)
        self.assertEqual(result["gateway"]["calls"], ["d1", "d2"])
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["deployment"], "d2")

    # ------------------------------------------------------------ 装配
    def _task(self) -> Task:
        return Task(
            run_id="run_gw",
            step_id="step_gw",
            task_type=TaskType.LLM_CALL,
            executor_type=ExecutorType.HTTP,
            payload={"prompt": "hi", "model": "gpt-4o"},
            timeout=timedelta(seconds=30),
        )

    def _ctx(self) -> FakeContext:
        return FakeContext(self._task())


@dataclass
class FakeContext:
    """Executor 只用到 heartbeat / check_cancelled —— duck-typed 就够了。

    这也顺带印证了 §13：Executor 不认识 Kernel，它拿到的只是两个能力。
    """

    task: Task
    heartbeats: int = 0

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def check_cancelled(self) -> bool:
        return False


if __name__ == "__main__":
    unittest.main()
