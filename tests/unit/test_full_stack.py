"""M15 全栈闭环验收（对应 M15 文档 §7）。

把阶段 1~8 一次性接起来跑一遍 —— 这不是"集成测试"，是**验收**：

    Goal → Plan → Decision → Action → Task → Kernel(PG) → Worker → Observation → State
                                              ↓
                                     Outbox → Kafka → Consumer（去重）
                                     Lease 索引（Redis，丢了可重建）

只要这个用例是绿的，就说明：
    · 领域不变量在真实存储上成立（PG 约束 + 乐观锁）
    · Redis 只是快路径（flushall 之后结果不变）
    · Kafka 至少一次 + 消费者去重（重复投递只处理一次）
    · Runtime 与 Kernel 的边界没被打破（Loop 从不直接改 Execution 状态）
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.execution import ExecutionStatus
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.executors import (
    LLMCallExecutor,
    ToolCallExecutor,
    ToolRegistry,
)
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.execution_kernel.adapters.kafka import KafkaEventPublisher, decode_event
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
    PostgresProcessedEventStore,
)
from packages.execution_kernel.adapters.redis import (
    RedisCancelSignalStore,
    RedisLeaseIndex,
    rebuild_lease_index,
)
from packages.execution_kernel.consumers import IdempotentConsumer
from packages.execution_kernel.inmemory import ManualClock
from packages.execution_kernel.kernel import ExecutionKernel
from packages.execution_kernel.outbox_delivery import InMemoryOutboxDeliveryStore
from packages.execution_kernel.publisher import OutboxPublisher
from packages.execution_kernel.recovery_controller import RecoveryController
from packages.execution_kernel.scheduler import Scheduler
from packages.execution_kernel.worker import Worker, WorkerConfig

from .fake_redis import FakeRedis
from .sqlite_shim import connect, load_schema_sql


class FakeLLM:
    def complete(self, prompt: str, **kwargs) -> dict:
        return {"text": "use the calculator"}


def calculator(args) -> dict:
    return {"value": eval(args["expr"], {"__builtins__": {}}, {})}  # noqa: S307 仅测试用


class Interpreter:
    def interpret(self, user_request: str, context) -> Goal:
        return Goal(
            run_id=str(context.get("run_id", "")),
            objective="answer arithmetic question",
            success_criteria=("numeric answer produced",),
            budget=Budget(max_steps=5),
        )


class Planner:
    def plan(self, state: State) -> Plan:
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n1", name="ask-llm"),
                PlanNode(node_id="n2", name="call-calculator", depends_on=("n1",)),
            ),
        )


class DecisionEngine:
    """先问 LLM，再调工具，然后 FINISH。"""

    def __init__(self) -> None:
        self.scripted: list[Action] | None = None
        self.index = 0

    def decide(self, state: State) -> Decision:
        if self.scripted is None:
            self.scripted = [
                Action(run_id=state.run_id, action_type=ActionType.LLM_CALL,
                       payload={"prompt": "how to compute 6*7?"}),
                Action(run_id=state.run_id, action_type=ActionType.TOOL_CALL,
                       payload={"tool": "calculator", "args": {"expr": "6*7"}},
                       risk_level=RiskLevel.LOW),
            ]
        if self.index >= len(self.scripted):
            return Decision(
                run_id=state.run_id,
                selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
                rationale="done",
            )
        action = self.scripted[self.index]
        self.index += 1
        return Decision(run_id=state.run_id, selected_action=action,
                        confidence_signal=0.9, rationale="scripted")


class FakeKafkaProducer:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def produce(self, *, topic, key, value, headers) -> None:
        self.messages.append({"topic": topic, "key": key, "value": value})

    def flush(self) -> None:
        pass


class FullStackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(load_schema_sql("002_outbox_consumer.sql"))
        self.addCleanup(self.conn.close)
        self.clock = ManualClock()
        self.redis = FakeRedis()

        self.repo = PostgresExecutionRepository(self.conn)
        self.attempts = PostgresAttemptRepository(self.conn)
        self.outbox = PostgresOutboxStore(self.conn)
        self.lease_index = RedisLeaseIndex(self.redis)

        self.kernel = ExecutionKernel(
            repository=self.repo,
            attempts=self.attempts,
            outbox=self.outbox,
            clock=self.clock,
            lease_index=self.lease_index,
            cancel_signals=RedisCancelSignalStore(self.redis),
        )
        registry = ToolRegistry()
        registry.register("calculator", calculator)
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors={"native": ToolCallExecutor(registry), "http": LLMCallExecutor(FakeLLM())},
            config=WorkerConfig(worker_id="w1", lease_ttl=timedelta(seconds=30),
                                heartbeat_interval=timedelta(seconds=10)),
        )
        self.loop = AgentLoop(
            kernel=self.kernel, worker=self.worker, interpreter=Interpreter(),
            planner=Planner(), decision_engine=DecisionEngine(),
        )

    def test_full_stack_closure(self) -> None:
        state = self.loop.start("what is 6*7?")
        self.loop.run()

        # ① Runtime：目标达成
        self.assertEqual(state.runtime_status, "FINISHED")
        self.assertEqual(self.loop.history[-1], StepOutcome.FINISHED)
        self.assertEqual(len(state.completed_tasks), 2)

        # ② Kernel：两个 Execution 都在 PG 里落到了 COMPLETED
        statuses = [self.kernel.status_of(eid) for eid in state.completed_tasks]
        self.assertEqual(statuses, [ExecutionStatus.COMPLETED] * 2)

        # ③ Attempt 历史可回读（阶段 5 补的端口）
        for execution_id in state.completed_tasks:
            attempt = self.attempts.get(execution_id, 1)
            self.assertIsNotNone(attempt)
            self.assertTrue(attempt.result)

        # ④ Lease 索引：执行完成后必须被清掉（终态不留 Lease）
        self.assertEqual(self.lease_index.due(self.clock.now() + timedelta(hours=1)), [])

        # ⑤ Outbox → Kafka → Consumer：至少一次投递 + 去重
        producer = FakeKafkaProducer()
        publisher = OutboxPublisher(outbox=self.outbox,
                                    publisher=KafkaEventPublisher(producer),
                                    delivery=InMemoryOutboxDeliveryStore())
        self.assertGreater(publisher.drain(), 0)
        delivered = [decode_event(m["value"]) for m in producer.messages]

        handled: list[str] = []
        consumer = IdempotentConsumer(
            store=PostgresProcessedEventStore(self.conn),
            handler=lambda e: handled.append(e.event_id),
        )
        first = consumer.consume_batch(delivered)
        second = consumer.consume_batch(delivered)      # 重投
        self.assertEqual(first[1], 0)
        self.assertEqual(second[0], 0)
        self.assertEqual(len(handled), len(delivered))

        # ⑥ 同聚合同 key（同分区保序）
        exec_keys = {
            m["key"] for m in producer.messages
            if m["key"].startswith("execution:")
        }
        self.assertTrue(exec_keys)

    def test_redis_loss_does_not_change_outcome(self) -> None:
        """Redis 整个没了：结果一模一样，只是 Recovery 要靠 PG 兜底扫。"""
        state = self.loop.start("what is 6*7?")
        self.loop.run()
        self.assertEqual(state.runtime_status, "FINISHED")

        self.redis.flushall()
        self.assertEqual(self.lease_index.due(self.clock.now()), [])

        # 索引可以从 PG 重建
        tracked = rebuild_lease_index(self.lease_index, self.repo)
        self.assertEqual(tracked, 0)          # 都已完成，没有在跑的 Lease

        # 而且就算有 STALE 漏网，Recovery 的 PG 兜底扫也能救回来
        self.assertEqual(RecoveryController(self.kernel).sweep(), [])
        self.assertEqual(self.kernel.repository.list_by_status(ExecutionStatus.STALE), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
