"""M25：子 Run 派生（`native:skill` 与 `agent_runtime:agent_delegation`）。

    D-1  派生键 = 父 Execution 的 execution_id（E-21 跨 Attempt 稳定）。
         重试/重排队**不得**开出第二条子 Run —— M24 那个"第二个 Run"在子 Run 上的重演
    D-2  ChildRunRequest 的必填字段在构造时校验，不留"半个请求"
    D-3  只能关**正在等的那一条**子 Run；拿错 id 直接报错，不静默放行
    D-4  `SuspensionReason.CHILD_AGENT` / `CHILD_SKILL` 必须真的被设置。
         它们从 M15 起就冻结在基线里，但直到 M25 才有代码去设置 ——
         概念冻结了而实现从没跟上，且没有一条测试断言过"谁设置了它"

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.events.event import (
    CHILD_RUN_COMPLETED,
    CHILD_RUN_FAILED,
)
from packages.agent_domain.execution import ExecutorType
from packages.agent_domain.execution.execution import (
    ExecutionStatus,
    SuspensionReason,
)
from packages.agent_domain.execution.task import TaskType
from packages.agent_domain.intelligence.action import Action, ActionType, RiskLevel
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.assembly import RuntimeStack, assemble_runtime_stack
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunKind,
    ChildRunRegistry,
    ChildRunRegistryPort,
    ChildRunRequest,
    ChildRunUnavailable,
    InProcessChildRunSpawner,
    child_run_kind_of,
)
from packages.agent_runtime.executors import (
    AgentDelegationExecutor,
    LLMCallExecutor,
    SkillExecutor,
    TaskTypeRouter,
    ToolCallExecutor,
)
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.tool_runtime import ToolRuntime
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
    Scheduler,
    Worker,
    WorkerConfig,
)
from packages.execution_kernel.worker import ExecutorError

from .helpers import make_task


# ---------------------------------------------------------------- 装配


@dataclass
class ScriptedInterpreter:
    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Goal:
        return Goal(
            run_id=str(context.get("run_id", "")),
            objective="delegate something",
            success_criteria=("child run finished",),
            budget=Budget(max_steps=8),
        )


@dataclass
class ScriptedPlanner:
    def plan(self, state: State) -> Plan:
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n0", name="delegate"),),
        )


@dataclass
class ScriptedDecisionEngine:
    """按脚本吐 Decision；用完就 FINISH。"""

    script: list[Action]
    index: int = 0

    def decide(self, state: State) -> Decision:
        if self.index >= len(self.script):
            return Decision(
                run_id=state.run_id,
                selected_action=Action(
                    run_id=state.run_id, action_type=ActionType.FINISH
                ),
                rationale="nothing left to do",
            )
        action = self.script[self.index]
        self.index += 1
        # 脚本里的 Action 是 run_id 未知的占位（先造脚本、后 start），
        # 这里按**当前 Run** 重写 —— Decision 与 Action 必须同属一条 Run。
        action = replace(action, run_id=state.run_id)
        return Decision(
            run_id=state.run_id,
            selected_action=action,
            confidence_signal=0.95,
            rationale="scripted",
        )


class FakeLLM:
    def complete(self, prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        return {"text": "child says hi"}


def echo_tool(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return dict(args)


def _tool_runtime() -> ToolRuntime:
    from packages.agent_runtime.tool_runtime import (
        FunctionInvoker,
        ToolRegistry as NewRegistry,
        ToolSpec,
    )

    registry = NewRegistry()
    registry.register(ToolSpec(name="echo", version="1"), FunctionInvoker(echo_tool))
    return ToolRuntime(registry)


class ChildRunTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=self.clock,
        )
        self.gateway = _gateway()
        self.tool_runtime = _tool_runtime()
        self.worker = Worker(
            kernel=self.kernel,
            scheduler=Scheduler(self.kernel),
            executors={
                "native": TaskTypeRouter(
                    {
                        TaskType.TOOL_CALL: ToolCallExecutor(self.tool_runtime),
                        TaskType.SKILL: SkillExecutor(),
                        TaskType.HUMAN_APPROVAL: SkillExecutor(),
                    },
                    executor_type="native",
                ),
                "http": TaskTypeRouter(
                    {TaskType.LLM_CALL: LLMCallExecutor(self.gateway)},
                    executor_type="http",
                ),
                "agent_runtime": TaskTypeRouter(
                    {TaskType.AGENT_DELEGATION: AgentDelegationExecutor()},
                    executor_type="agent_runtime",
                ),
            },
            config=WorkerConfig(
                worker_id="w1",
                lease_ttl=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=10),
            ),
        )

    def _child_stack_factory(self):
        """子 Run 工厂：签名与 ControlPlane.factory 完全一致。"""
        seen: list[str] = []

        def factory(agent_id: str, approvals: Any = None) -> RuntimeStack:
            seen.append(agent_id)
            return assemble_runtime_stack(
                agent_id=agent_id,
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=ScriptedDecisionEngine([]),
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=ExecutionKernel(
                    repository=InMemoryExecutionRepository(),
                    attempts=InMemoryAttemptRepository(),
                    outbox=InMemoryOutbox(),
                    clock=ManualClock(),
                ),
            )

        factory.seen = seen                                  # type: ignore[attr-defined]
        return factory

    def _loop(self, action: Action, *, spawner=None) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([action]),
            config=AgentLoopConfig(max_steps=6),
            spawner=spawner,
        )

    def _suspended(self):
        """Kernel 里所有 SUSPENDED 的 Execution —— 事实源，不是 `pending_child`。"""
        return [
            e
            for e in self.kernel.repository.all()
            if e.status is ExecutionStatus.SUSPENDED
        ]

    def _delegation(self, run_id: str = "run_1", **payload) -> Action:
        return Action(
            run_id=run_id,
            action_type=ActionType.AGENT_DELEGATION,
            payload={"agent_id": "researcher", "instruction": "go find out", **payload},
        )

    def _skill(self, run_id: str = "run_1", **payload) -> Action:
        return Action(
            run_id=run_id,
            action_type=ActionType.SKILL_CALL,
            payload={"skill": "financial_analysis", **payload},
            risk_level=RiskLevel.MEDIUM,
        )

    def _request(self, **kwargs: Any) -> ChildRunRequest:
        """派生请求。`action` 与 `parent_task_id` 是必填（D-2 / S-1），
        这里补默认值，好让每条测试只操心它想测的那一个字段。"""
        kwargs.setdefault("action", self._delegation())
        kwargs.setdefault("parent_task_id", "task_1")
        return ChildRunRequest(**kwargs)

    def _handle(self, child_run_id: str = "child_1", **kwargs: Any) -> Any:
        """派生句柄。同理补上必填项。"""
        kwargs.setdefault("action", self._delegation())
        kwargs.setdefault("parent_task_id", "task_1")
        return ChildRunHandle(
            child_run_id=child_run_id,
            kind=ChildRunKind.AGENT,
            parent_run_id="run_1",
            parent_execution_id="exec_1",
            target="researcher",
            **kwargs,
        )


def _gateway():
    from packages.agent_runtime.executors import legacy_gateway

    return legacy_gateway(FakeLLM(), model_id="fake")


# ---------------------------------------------------------------- D-1 / D-2


class ChildRunRequestTest(unittest.TestCase):
    def test_kind_of_maps_the_two_child_actions(self) -> None:
        self.assertIs(
            child_run_kind_of(ActionType.AGENT_DELEGATION), ChildRunKind.AGENT
        )
        self.assertIs(child_run_kind_of(ActionType.SKILL_CALL), ChildRunKind.SKILL)

    def test_the_control_kind_of_returns_none_for_ordinary_work(self) -> None:
        """控制组：普通活不是派生 —— 上一条不是因为它对所有类型都返回了东西。"""
        for action_type in (
            ActionType.TOOL_CALL,
            ActionType.LLM_CALL,
            ActionType.FINISH,
            ActionType.REPLAN,
        ):
            self.assertIsNone(child_run_kind_of(action_type), action_type)

    def test_d2_the_derivation_key_is_required(self) -> None:
        """D-2/D-1：没有 `parent_execution_id` 就直接拒绝 ——
        它是派生键，缺了它重试就会开出第二条子 Run。"""
        with self.assertRaises(InvariantViolation) as cm:
            ChildRunRequest(
                parent_run_id="run_1",
                parent_execution_id="",
                kind=ChildRunKind.AGENT,
                target="researcher",
            )
        self.assertIn("parent_execution_id", str(cm.exception))

    def test_s1_the_action_is_required_because_it_carries_the_undo(self) -> None:
        """S-1：没有 `action` 就拒绝 —— 它挂着 `compensation`（逆操作声明）。

        M26 之前 `ChildRunRequest` 不带 Action，于是子 Run 的派生记录
        **无法登记撤销**：父 Run 恢复之后拿到子 Run 的结果时，
        手上没有逆操作声明，`SagaCoordinator` 静默跳过，账本缺一条。
        """
        with self.assertRaises(InvariantViolation) as cm:
            ChildRunRequest(
                parent_run_id="run_1",
                parent_execution_id="exec_1",
                parent_task_id="task_1",
                kind=ChildRunKind.AGENT,
                target="researcher",
                action=None,
            )
        self.assertIn("compensation declaration", str(cm.exception))

    def test_d2_target_is_required(self) -> None:
        with self.assertRaises(InvariantViolation):
            ChildRunRequest(
                parent_run_id="run_1",
                parent_execution_id="exec_1",
                parent_task_id="task_1",
                kind=ChildRunKind.SKILL,
                target="",
                action=Action(
                    run_id="run_1", action_type=ActionType.SKILL_CALL
                ),
            )

    def test_the_control_a_complete_request_is_accepted(self) -> None:
        """控制组：字段齐了就能造 —— 上两条不是因为构造函数坏了。"""
        request = ChildRunRequest(
            parent_run_id="run_1",
            parent_execution_id="exec_1",
            parent_task_id="task_1",
            kind=ChildRunKind.AGENT,
            target="researcher",
            action=Action(run_id="run_1", action_type=ActionType.AGENT_DELEGATION),
        )
        self.assertEqual(request.kind, ChildRunKind.AGENT)


class D1IdempotentSpawnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ChildRunRegistry()

    def _handle(self, run_id: str = "child_1") -> Any:
        return ChildRunHandle(
            child_run_id=run_id,
            kind=ChildRunKind.AGENT,
            parent_run_id="run_1",
            parent_execution_id="exec_1",
            target="researcher",
            # S-1 / D-2：派生记录必须带着"引起它的那个 Action"与父 Task id
            action=Action(
                run_id="run_1", action_type=ActionType.AGENT_DELEGATION
            ),
            parent_task_id="task_1",
        )

    def test_d1_a_second_bind_returns_the_first(self) -> None:
        """D-1：同一个父 Execution 再派生一次，拿回**第一条**。"""
        first = self.registry.bind(self._handle("child_1"))
        second = self.registry.bind(self._handle("child_2"))
        self.assertEqual(second.child_run_id, "child_1")
        self.assertEqual(len(self.registry), 1)

    def test_d1_the_control_different_keys_produce_different_children(self) -> None:
        """控制组：不同的父 Execution 确实各派生一条 —— 上一条不是因为它只肯存一条。"""
        from packages.agent_runtime.delegation import ChildRunHandle

        self.registry.bind(self._handle("child_1"))
        self.registry.bind(
            ChildRunHandle(
                child_run_id="child_2",
                kind=ChildRunKind.AGENT,
                parent_run_id="run_1",
                parent_execution_id="exec_2",
                target="researcher",
                action=Action(
                    run_id="run_1", action_type=ActionType.AGENT_DELEGATION
                ),
                parent_task_id="task_2",
            )
        )
        self.assertEqual(len(self.registry), 2)
        self.assertEqual(len(self.registry.children_of("run_1")), 2)


class SpawnerTest(ChildRunTestBase):
    def test_the_spawner_really_starts_a_run(self) -> None:
        """派生不是"登记一笔"，是**真的起了一条 Run**。"""
        factory = self._child_stack_factory()
        spawner = InProcessChildRunSpawner(factory=factory)
        handle = spawner.spawn(
            self._request(
                parent_run_id="run_1",
                parent_execution_id="exec_1",
                kind=ChildRunKind.AGENT,
                target="researcher",
                instruction="go",
            )
        )
        self.assertTrue(handle.child_run_id)
        self.assertEqual(factory.seen, ["researcher"])

    def test_d1_a_retry_does_not_spawn_a_second_child(self) -> None:
        """D-1 的终局：父 Task 被重跑（Attempt #2 / Recovery 重排队）时，
        工厂**不再被调用第二次**。"""
        factory = self._child_stack_factory()
        spawner = InProcessChildRunSpawner(factory=factory)
        request = self._request(
            parent_run_id="run_1",
            parent_execution_id="exec_1",
            kind=ChildRunKind.AGENT,
            target="researcher",
            instruction="go",
        )
        first = spawner.spawn(request)
        second = spawner.spawn(request)
        self.assertEqual(first.child_run_id, second.child_run_id)
        self.assertEqual(factory.seen, ["researcher"])

    def test_skill_target_is_namespaced(self) -> None:
        """技能走同一条路，但 target 带 `skill:` 前缀 ——
        免得一个叫 `researcher` 的技能撞上一个叫 `researcher` 的 agent。"""
        factory = self._child_stack_factory()
        spawner = InProcessChildRunSpawner(factory=factory)
        spawner.spawn(
            self._request(
                parent_run_id="run_1",
                parent_execution_id="exec_1",
                kind=ChildRunKind.SKILL,
                target="financial_analysis",
            )
        )
        self.assertEqual(factory.seen, ["skill:financial_analysis"])

    def test_the_whole_cycle_parent_child_parent(self) -> None:
        """端到端：派生 → 驱动子 Run 到终态 → 结果回到父 Run 的 State。

        只有这条跑通，"派生"才不只是个机制而是个能力。
        缺了 `drive()` 这一环就仍是"指到空气" —— 子 Run 起得来、没人跑它，
        父 Run 永远等一个不会来的结果。
        """
        factory = self._child_stack_factory()
        spawner = InProcessChildRunSpawner(factory=factory)
        loop = self._loop(self._delegation(), spawner=spawner)
        state = loop.start("delegate it")

        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        child_id = loop.pending_child.child_run_id
        assert child_id is not None

        child_state = spawner.drive(child_id)
        self.assertIsNotNone(child_state)

        self.assertIs(
            loop.child_completed(child_id, {"answer": "42"}), StepOutcome.EXECUTED
        )
        # 结果真的进了父 Run 的 State —— 否则父 Agent 会以为这一步没发生过
        kinds = [o.kind for o in loop.state.observations]
        self.assertIn("child_run.finished", kinds)

    def test_the_control_a_remote_child_cannot_be_driven(self) -> None:
        """控制组：跨进程时子 Run 不在这台机器上 —— `drive()` 如实说做不到。

        假装跑过一次然后返回，比直接报错坏得多：父 Run 会拿到一个
        根本没人执行过的结果，而且**没有任何痕迹**表明它没跑。
        """
        spawner = InProcessChildRunSpawner(factory=lambda a, p: None)
        with self.assertRaises(ChildRunUnavailable) as cm:
            spawner.drive("child_somewhere_else")
        self.assertIn("not driven by this process", str(cm.exception))

    def test_a_spawner_without_a_factory_says_so(self) -> None:
        with self.assertRaises(ChildRunUnavailable) as cm:
            InProcessChildRunSpawner(factory=None).spawn(
                self._request(
                    parent_run_id="run_1",
                    parent_execution_id="exec_1",
                    kind=ChildRunKind.AGENT,
                    target="researcher",
                )
            )
        self.assertIn("no child-run factory", str(cm.exception))


# ---------------------------------------------------------------- D-4：挂起原因真的被设置


class LoopDelegationTest(ChildRunTestBase):
    def test_d4_delegation_suspends_with_child_agent(self) -> None:
        """D-4：`SuspensionReason.CHILD_AGENT` 终于**真的被设置了**。

        它从 M15 起就冻结在基线里，而直到 M25 之前全仓库没有任何一处设置它 ——
        和 A-3（幂等键接到 Redis）是同一种病：概念冻结了，实现从没跟上，
        因为没有一条测试断言"谁设置了它"。这条就是那条测试。
        """
        factory = self._child_stack_factory()
        loop = self._loop(
            self._delegation(), spawner=InProcessChildRunSpawner(factory=factory)
        )
        state = loop.start("delegate it")
        action = self._delegation(run_id=state.run_id)

        outcome = loop.step()

        self.assertIs(outcome, StepOutcome.WAITING_CHILD)
        self.assertIsNotNone(loop.pending_child)

        suspended = self._suspended()
        self.assertEqual(len(suspended), 1)
        execution = suspended[0]
        self.assertEqual(
            execution.suspension.reason, SuspensionReason.CHILD_AGENT
        )
        self.assertEqual(
            execution.suspension.wait_condition["child_run_id"],
            loop.pending_child.child_run_id,
        )

    def test_d4_skill_suspends_with_child_skill(self) -> None:
        factory = self._child_stack_factory()
        loop = self._loop(self._skill(), spawner=InProcessChildRunSpawner(factory=factory))
        state = loop.start("run the skill")
        outcome = loop.step()
        self.assertIs(outcome, StepOutcome.WAITING_CHILD)
        self.assertEqual(
            self._suspended()[0].suspension.reason, SuspensionReason.CHILD_SKILL
        )

    def test_the_control_an_ordinary_action_does_not_suspend_for_a_child(self) -> None:
        """控制组：TOOL_CALL 不走派生分支 —— 上两条不是因为 step() 一律挂起。"""
        factory = self._child_stack_factory()
        loop = self._loop(
            Action(
                run_id="run_1",
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "echo", "args": {"a": 1}},
            ),
            spawner=InProcessChildRunSpawner(factory=factory),
        )
        state = loop.start("call a tool")
        outcome = loop.step()
        self.assertIsNot(outcome, StepOutcome.WAITING_CHILD)
        self.assertEqual(factory.seen, [])

    def test_child_completed_closes_the_suspension(self) -> None:
        factory = self._child_stack_factory()
        loop = self._loop(
            self._delegation(), spawner=InProcessChildRunSpawner(factory=factory)
        )
        state = loop.start("delegate it")
        loop.step()
        handle = loop.pending_child
        assert handle is not None
        execution_id = handle.parent_execution_id

        outcome = loop.child_completed(handle.child_run_id, {"answer": "42"})

        self.assertIs(outcome, StepOutcome.EXECUTED)
        self.assertEqual(
            self.kernel.status_of(execution_id), ExecutionStatus.COMPLETED
        )
        self.assertIsNone(loop.pending_child)

    def test_child_failed_reports_failure(self) -> None:
        factory = self._child_stack_factory()
        loop = self._loop(
            self._delegation(), spawner=InProcessChildRunSpawner(factory=factory)
        )
        state = loop.start("delegate it")
        loop.step()
        handle = loop.pending_child
        assert handle is not None

        outcome = loop.child_failed(handle.child_run_id, reason="child budget out")
        self.assertIs(outcome, StepOutcome.FAILED)
        self.assertIsNone(loop.pending_child)

    def test_d3_a_stranger_cannot_close_the_gate(self) -> None:
        """D-3：拿错的 child_run_id 关不掉闸门 —— 静默放行会让父 Run 少一步。"""
        factory = self._child_stack_factory()
        loop = self._loop(
            self._delegation(), spawner=InProcessChildRunSpawner(factory=factory)
        )
        state = loop.start("delegate it")
        loop.step()
        with self.assertRaises(InvariantViolation) as cm:
            loop.child_completed("child_someone_else")
        self.assertIn("D-3", str(cm.exception))
        self.assertIsNotNone(loop.pending_child)

    def test_without_a_spawner_the_task_falls_through_to_the_worker(self) -> None:
        """没有派生器 → 派给 Worker → 被安全网点名拒绝，而**不是**静默成功。

        这条同时是 `AgentDelegationExecutor` 的控制组：
        它证明那条拒绝信息真的会被走到。
        """
        loop = self._loop(self._delegation())
        state = loop.start("delegate it")
        outcome = loop.step()
        self.assertIs(outcome, StepOutcome.FAILED)
        self.assertIsNone(loop.pending_child)


class SafetyNetExecutorTest(ChildRunTestBase):
    def _execute(self, executor, task_type: TaskType) -> ExecutorError:
        task = make_task(task_type=task_type, executor_type=ExecutorType.NATIVE)
        ctx = self.kernel  # 占位：execute() 会在做任何事之前就抛
        with self.assertRaises(ExecutorError) as cm:
            executor.execute(task, ctx)                      # type: ignore[arg-type]
        return cm.exception

    def test_skill_names_its_real_owner(self) -> None:
        """报错必须点名真正的主人（父 Loop / CHILD_SKILL），
        不能退化成 `payload.tool is required` 那种把人引向 payload 的话。"""
        err = self._execute(SkillExecutor(), TaskType.SKILL)
        self.assertEqual(err.code, "SKILL_NOT_WORKER_EXECUTABLE")
        self.assertIn("child run", err.message)
        self.assertIn("second skill run", err.message)

    def test_delegation_names_its_real_owner(self) -> None:
        err = self._execute(AgentDelegationExecutor(), TaskType.AGENT_DELEGATION)
        self.assertEqual(err.code, "DELEGATION_NOT_WORKER_EXECUTABLE")
        self.assertIn("child AgentRun", err.message)
        self.assertIn("second child run", err.message)

    def test_the_control_the_message_is_not_a_payload_error(self) -> None:
        """控制组：M23 修掉的那类伪装（路由错报成载荷错）没有回来。

        两条报错里都**不该**出现 `payload` 这个词 ——
        一旦出现，排障的人又会去查"payload 是谁填的"。
        """
        for executor, task_type in (
            (SkillExecutor(), TaskType.SKILL),
            (AgentDelegationExecutor(), TaskType.AGENT_DELEGATION),
        ):
            err = self._execute(executor, task_type)
            self.assertNotIn("payload", err.message)
            self.assertNotIn("BAD_PAYLOAD", err.code)

# ---------------------------------------------------------------- 空洞 209
class ChildRunCompletionEventTest(ChildRunTestBase):
    """空洞 209：子 Run 进入终态时**自己说出来**。

    M30 之前这个事件根本不存在。后果是 `AgentLoop.child_completed()`
    只有测试直接调用过它，生产路径上**没有任何人**来叫醒父 Run ——
    父 Execution 会一直 SUSPENDED，而且不报错。

    为什么不能让父 Run 去问（轮询）：

        · 父 Run 每次被推进都要多一次查询；
        · 更要命的是 **父 Run 不被推进时永远没人问** ——
          而它恰恰在等子 Run，按 D-5 根本不会自己往前走。

    所以是子 Run 主动广播，而且**走 Outbox 而不是直接发 Kafka**：
    "子 Run 已终态"这个状态写 与 "通知父 Run"这个事件写必须在同一个事务里
    （X-3），否则进程在中间崩掉会留下一条永远不会被叫醒的父 Execution。
    """

    def _factory_recording_outboxes(self, outboxes: list[Any]) -> Any:
        """子 Run 工厂，但把每条子 Run 的 Outbox 收集起来 ——
        子 Run 用的是**自己的** Kernel，父 Run 的 Outbox 里不会有它的事件。"""

        def factory(agent_id: str, approvals: Any = None) -> RuntimeStack:
            outbox = InMemoryOutbox()
            outboxes.append(outbox)
            return assemble_runtime_stack(
                agent_id=agent_id,
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=ScriptedDecisionEngine([]),
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=ExecutionKernel(
                    repository=InMemoryExecutionRepository(),
                    attempts=InMemoryAttemptRepository(),
                    outbox=outbox,
                    clock=ManualClock(),
                ),
            )

        return factory

    def _spawn_child(self) -> tuple[Any, list[Any], str, Any, Any]:
        """派生但**不驱动**。返回 (父 loop, outboxes, child_run_id, 子 stack, spawner)。"""
        outboxes: list[Any] = []
        spawner = InProcessChildRunSpawner(
            factory=self._factory_recording_outboxes(outboxes)
        )
        loop = self._loop(self._delegation(), spawner=spawner)
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        child_id = loop.pending_child.child_run_id
        return loop, outboxes, child_id, spawner.stack_for(child_id), spawner

    def _spawn_and_finish_child(self) -> tuple[Any, list[Any], str, Any]:
        """派生 + 跑到终态。返回 (父 loop, outboxes, child_run_id, 子 stack)。"""
        loop, outboxes, child_id, child_stack, spawner = self._spawn_child()
        spawner.drive(child_id)
        return loop, outboxes, child_id, child_stack

    def _child_events(self, outboxes: list[Any]) -> list[Any]:
        return [
            e
            for ob in outboxes
            for e in ob.all()
            if e.event_type in (CHILD_RUN_COMPLETED, CHILD_RUN_FAILED)
        ]

    def test_a_child_run_announces_its_own_completion(self) -> None:
        """子 Run 跑到终态 → Outbox 里有一条 `child_run.completed`。"""
        _, outboxes, _, _ = self._spawn_and_finish_child()
        self.assertEqual(len(self._child_events(outboxes)), 1)
        self.assertEqual(
            self._child_events(outboxes)[0].event_type, CHILD_RUN_COMPLETED
        )

    def test_the_event_carries_everything_the_wake_path_needs(self) -> None:
        """唤醒路径重建父 Run 需要 parent_run_id；关闸门需要 parent_execution_id。

        事件里只带 child_run_id 是不够的：快照里存的是"我在等哪一条"（R-6），
        而**恢复父 Run**要用的是父 Run 自己的 id。两个都得在。
        """
        loop, outboxes, child_id, _ = self._spawn_and_finish_child()
        event = self._child_events(outboxes)[0]
        self.assertEqual(event.payload["child_run_id"], child_id)
        self.assertEqual(event.payload["parent_run_id"], loop.state.run_id)
        self.assertTrue(event.payload["parent_execution_id"])
        self.assertEqual(event.payload["child_kind"], "agent")
        self.assertEqual(event.payload["status"], "completed")

    def test_a_result_comes_back_with_the_event(self) -> None:
        """结果必须跟着事件走 —— 唤醒路径在**另一个进程**，它读不到子 Run 的内存。"""
        _, outboxes, _, _ = self._spawn_and_finish_child()
        result = self._child_events(outboxes)[0].payload["result"]
        self.assertEqual(result["status"], "completed")
        self.assertIn("steps", result)

    def test_a_failed_child_says_failed_not_completed(self) -> None:
        """失败是**另一个**事件类型：父 Run 对两者处理完全不同
        （completed 直接关闸门，failed 交给 Kernel 判要不要重试）。

        这里直接调 `_declare_terminal` 是白盒，但它是 FAILED 的**唯一出口**（B-7），
        钉在这里就是钉在真正的分叉点上。派生之后**不驱动**，
        因为一条已经 COMPLETED 的 Run 再声明 FAILED 会被 B-3 挡下。
        """
        _, outboxes, child_id, child_stack = self._spawn_child()[:4]
        child_stack.loop._declare_terminal(AgentRunStatus.FAILED, reason="test")
        failed = [
            e for e in self._child_events(outboxes) if e.event_type == CHILD_RUN_FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["child_run_id"], child_id)

    def test_the_control_a_top_level_run_announces_nothing(self) -> None:
        """控制组：不是任何人的子 Run → **不发**事件。

        发一个 `parent_run_id=""` 的 `child_run.completed`，
        会让消费端去恢复一个空 id 的 Run，报出来的错会和真正的故障混在一起。
        """
        outboxes: list[Any] = []
        spawner = InProcessChildRunSpawner(
            factory=self._factory_recording_outboxes(outboxes)
        )
        # 顶层 Run：脚本为空 → 第一步就 FINISH → 进入终态
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
            config=AgentLoopConfig(max_steps=6),
            spawner=spawner,
        )
        loop.start("just finish")
        loop.run()
        self.assertEqual(self._child_events([self.kernel.outbox]), [])
        self.assertEqual(self._child_events(outboxes), [])

    def test_the_control_the_parent_outbox_has_no_child_event(self) -> None:
        """控制组：完成事件写在**子 Run 自己的** Outbox 里，不在父 Run 的。

        两条 Run 各有各的 Kernel（子 Run 甚至可能在别的机器上），
        所以"父 Run 的 Outbox 里没有它"不是 bug，是**这条路径的形状**。
        断言它，是为了防止有人图省事把事件塞进父 Kernel ——
        那样单进程下能过，跨进程立刻失效，而且一样不报错。
        """
        _, outboxes, _, _ = self._spawn_and_finish_child()
        self.assertEqual(self._child_events([self.kernel.outbox]), [])
        self.assertEqual(len(self._child_events(outboxes)), 1)


if __name__ == "__main__":
    unittest.main()
