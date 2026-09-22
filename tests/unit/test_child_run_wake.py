"""M30 / 空洞 209：子 Run 的结果**交回**父 Run（"认得回来"那一半）。

    D-6  终态与非终态由 `completed_at` 区分，且两者必须同进同退（SQL 侧有 CHECK）
    D-7  结果不可能在产生之前被交付；也**不可能交付两次**
    D-8  交回结果之后必须落一个新的可恢复点 —— 否则父 Run 每次重建都还是
         "在等那条子 Run"，`step()` 永远 `WAITING_CHILD`（不报错，只是永远走不下去）

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.events.event import (
    CHILD_RUN_CANCELLED,
    CHILD_RUN_COMPLETED,
    CHILD_RUN_FAILED,
    new_event,
)
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import (
    ChildRunWaker,
    ChildWakeOutcome,
    ChildWakeSweepResult,
)
from packages.agent_runtime.child_wait import WaitExpirySweepResult
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunKind,
    ChildRunRegistry,
    InProcessChildRunSpawner,
)
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.agent_runtime.recovery import (
    InMemoryRunSnapshotStore,
    RunRecovery,
)
from packages.agent_runtime.saga import (
    InMemoryCompensationStore,
    SagaCoordinator,
)
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)
from packages.execution_kernel.consumers import InMemoryProcessedEventStore

from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

try:  # pragma: no cover - apps 层只在完整安装下可用
    from apps._runtime import ManualStop
    from apps.child_run_consumer import ChildRunConsumerApp, ChildRunConsumerConfig
    _APPS = True
except Exception:  # pragma: no cover
    _APPS = False


# ---------------------------------------------------------------- 结果 / 交付


def _handle(child_run_id: str = "child_1", **kwargs: Any) -> ChildRunHandle:
    kwargs.setdefault(
        "action",
        Action(run_id="run_parent", action_type=ActionType.AGENT_DELEGATION),
    )
    kwargs.setdefault("parent_task_id", "task_1")
    kwargs.setdefault("parent_execution_id", "exec_1")
    return ChildRunHandle(
        child_run_id=child_run_id,
        kind=ChildRunKind.AGENT,
        parent_run_id="run_parent",
        target="researcher",
        **kwargs,
    )


class ChildRunResultTest(unittest.TestCase):
    """D-6 / D-7：结果的产生与交付。登记处是**内存版**。"""

    def setUp(self) -> None:
        self.registry = ChildRunRegistry()
        self.registry.bind(_handle())

    def test_mark_finished_records_the_result(self) -> None:
        handle = self.registry.mark_finished("child_1", "completed", {"n": 1})
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "completed")
        self.assertEqual(dict(handle.result), {"n": 1})
        self.assertIsNotNone(handle.completed_at)

    def test_a_repeated_completion_is_idempotent(self) -> None:
        """at-least-once 下同一条事件会来第二次 —— 第二次必须什么都不改。"""
        first = self.registry.mark_finished("child_1", "completed", {"n": 1})
        second = self.registry.mark_finished("child_1", "completed", {"n": 999})
        self.assertEqual(second.completed_at, first.completed_at)
        self.assertEqual(dict(second.result), {"n": 1})

    def test_a_conflicting_terminal_status_is_refused(self) -> None:
        """B-3：终态子 Run 不可能变成另一个终态。

        静默接受的后果是父 Run 会拿到**第二种说法**，
        而 `child_runs` 里那一行到底发生过什么就再也说不清了。
        """
        self.registry.mark_finished("child_1", "completed", {})
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.mark_finished("child_1", "failed", {})
        self.assertIn("B-3", str(cm.exception))

    def test_an_unregistered_child_run_cannot_be_finished(self) -> None:
        """控制组方向：结果只能写给**登记过**的子 Run。"""
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.mark_finished("nobody", "completed", {})
        self.assertIn("D-2", str(cm.exception))

    def test_delivery_before_a_result_exists_is_refused(self) -> None:
        """D-7：不能交付一个还没有的结果。"""
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.mark_delivered("child_1")
        self.assertIn("D-7", str(cm.exception))

    def test_delivery_happens_exactly_once(self) -> None:
        self.registry.mark_finished("child_1", "completed", {})
        self.assertTrue(self.registry.mark_delivered("child_1"))
        self.assertFalse(self.registry.mark_delivered("child_1"))

    def test_undelivered_lists_finished_but_not_yet_delivered(self) -> None:
        """A-12 兜底扫的输入。三条组合：未终态 / 已交付 / 待交付。"""
        self.registry.bind(_handle("child_2", parent_execution_id="exec_2"))
        self.registry.bind(_handle("child_3", parent_execution_id="exec_3"))
        self.registry.mark_finished("child_1", "completed", {})
        self.registry.mark_finished("child_2", "completed", {})
        self.registry.mark_delivered("child_2")
        # child_3 还没跑完
        self.assertEqual(
            [h.child_run_id for h in self.registry.undelivered()], ["child_1"]
        )

    def test_the_control_all_three_indexes_see_the_new_version(self) -> None:
        """控制组：更新过的 handle 必须从**三个**入口都读得到。

        只更新一个索引就会出现"按 execution 查到旧的、按 child 查到新的" ——
        那是两个答案，而排障时没有人会想到去两边各查一次。
        """
        updated = self.registry.mark_finished("child_1", "completed", {"n": 7})
        self.assertTrue(self.registry.for_child("child_1").is_finished)
        self.assertTrue(self.registry.for_execution("exec_1").is_finished)
        self.assertTrue(self.registry.children_of("run_parent")[0].is_finished)
        self.assertEqual(
            dict(self.registry.children_of("run_parent")[0].result), {"n": 7}
        )
        del updated


# ---------------------------------------------------------------- 唤醒


@dataclass
class _FakeLLM:
    def complete(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {"text": "child says hi"}


class WakeWorld(unittest.TestCase):
    """一个**真跑起来**的父子世界：父 Run 派生子 Run，子 Run 跑到终态。

    刻意不手搓快照与 handle：手搓的那份会让"唤醒"退化成几个对象互相赋值，
    而真跑一遍才会撞上闸门、快照、D-3 这些真正会出事的地方（PR-28）。
    """

    def setUp(self) -> None:
        self.registry = ChildRunRegistry()
        self.snapshots = InMemoryRunSnapshotStore()
        self.kernel = ExecutionKernel(
            repository=InMemoryExecutionRepository(),
            attempts=InMemoryAttemptRepository(),
            outbox=InMemoryOutbox(),
            clock=ManualClock(),
        )
        self.recovery = RunRecovery(
            snapshots=self.snapshots, factory=self._factory, approvals=None
        )
        # D-13：唤醒路径的账本必须与栈里那条是**同一个**，否则孤儿副作用
        # 记在一处、父 Run 的撤销读另一处（A-12 里"变错"的那半）。
        self.compensations = InMemoryCompensationStore()
        self.waker = ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        """与 ControlPlane.factory 同签名。父子共用 kernel / registry / snapshots ——
        生产里它们也共用**同一个连接**（`build_child_run_consumer`）。"""
        spawner = InProcessChildRunSpawner(
            factory=self._factory, approvals=approvals, registry=self.registry
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=spawner,
        )

    def _parent(self, run_id: str = "run_parent") -> AgentLoop:
        """父 Run：脚本里第一步就是一次委派。"""
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine(
                [
                    Action(
                        run_id=run_id,
                        action_type=ActionType.AGENT_DELEGATION,
                        payload={"agent_id": "researcher"},
                    )
                ]
            ),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id=run_id)
        return stack.loop

    def _spawned(self, run_id: str = "run_parent") -> tuple[AgentLoop, str]:
        """派生但**不驱动**子 Run。返回 (父 loop, child_run_id)。"""
        self._stacks = {}
        loop = self._parent(run_id)
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        child_id = loop.pending_child.child_run_id
        assert loop.spawner is not None
        self._stacks[child_id] = loop.spawner.stack_for(child_id)
        return loop, child_id

    def _stack_of(self, child_id: str) -> Any:
        return self._stacks[child_id]

    def _latest(self) -> Any:
        """父 Run 的**最新一份**快照。

        D-27 之后唤醒会把父 Run 推到终态，而 R-3 禁止恢复终态 Run ——
        于是 `recovery.rebuild()` 在这条路径上不再可用。
        但"它现在停在哪个可恢复点上"这件事**本来就该问存储**：
        快照是事实，`rebuild()` 是"再来一次"的能力，问事实不该被它挡住。
        """
        snapshot = self.snapshots.latest("run_parent")
        assert snapshot is not None, "父 Run 一份快照都没落"
        return snapshot


class ChildRunWakerTest(WakeWorld):
    def test_the_result_lands_in_the_registry_when_the_child_finishes(self) -> None:
        """X-5：结果在**成为事实的那一刻**落登记处，不只是捎在事件里。"""
        _loop, child_id = self._spawned()
        stack = self._stack_of(child_id)
        stack.loop.run()
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "completed")

    def test_wake_delivers_the_result_to_the_parent(self) -> None:
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)
        self.assertIsNone(self._latest().pending_child_id, "父 Run 不该还在等")
        # D-27：而且它已经**不在"能推一步"那一格**上停着了 ——
        # 交回结果的人自己把它推到了下一个阻塞点（这里就是终态）。
        self.assertTrue(self._latest().is_terminal)

    def test_d8_the_parent_can_be_driven_on_after_the_wake(self) -> None:
        """D-8 最要紧的那一条：交回结果之后父 Run 必须**还能往前走**。

        不落新快照的话，重建出来的父 Run 仍写着 `pending_child_id`，
        而 `step()` 一见它就返回 `WAITING_CHILD` ——
        一个已经拿到结果却永远走不下去的 Run，且不报错。

        ------------------------------------------------------------------
        D-27 把"还能往前走"升级成了"**已经往前走了**"

        之前这一条靠 `rebuild()` 再 `step()` 一次来证明"走得动"：
        那是 D-8 的判据 —— **有**一个不含 `pending_child` 的可恢复点。
        空洞 217 补上推进之后，多了一条更强的判据：
        最新那份快照本身就是推进之后落的（D-28），
        所以它既不含 `pending_child`，也应该已经是终态。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)

        latest = self._latest()
        self.assertIsNone(latest.pending_child_id, "重建出来不该再挂在子 Run 上")
        self.assertTrue(latest.is_terminal, "D-27：已经推到终态，不是停在'能推一步'")
        # D-8 本身仍然成立：这份快照是**交付之后**新落的一份，不是挂起时那份。
        self.assertGreater(len(self.snapshots.list_for("run_parent")), 1)

    def test_a_second_wake_is_a_no_op(self) -> None:
        """重复投递：第二次什么也不做，且**不产生第二次副作用**。"""
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)
        before = len(self.snapshots.list_for("run_parent"))
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(len(self.snapshots.list_for("run_parent")), before)

    def test_a_wake_whose_mark_was_lost_is_still_a_no_op(self) -> None:
        """那一个瞬间：交付成功了，`mark_delivered` 还没写进去就崩了。

        重投时登记处说"没交付过"，但重建出来的父 Run 已经不在等它 ——
        第二道判据（`pending_child is None`）必须接住，否则会二次交付。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)
        # 模拟崩溃：`delivered_at` 被回滚了，PG 里只有交付的后果
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.registry._store(replace(handle, delivered_at=None))

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)

    def test_sweep_wakes_a_parent_without_any_event(self) -> None:
        """A-12：Kafka 丢了也只等于**变慢**。

        把事件这一整条路拿掉，只留 PG —— 兜底扫照样把父 Run 叫醒。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.assertEqual(self.waker.sweep().delivered, (child_id,))
        self.assertIsNone(self._latest().pending_child_id)
        self.assertTrue(self._latest().is_terminal, "D-27：兜底扫那条路也把父 Run 推走了")
        self.assertEqual(self.waker.sweep().total, 0, "扫第二次不该再交付")

    def test_an_unregistered_child_run_is_refused(self) -> None:
        """事件说"这条子 Run 完了"，登记处说"没这个人" —— 事实层面矛盾。"""
        with self.assertRaises(LookupError) as cm:
            self.waker.wake("never_bound")
        self.assertIn("D-2", str(cm.exception))

    def test_a_child_without_a_result_is_refused(self) -> None:
        """D-6：事件到了而结果没落库 ⟹ X-3 已经破了（写不在同一个事务里）。

        这条必须**红着**，不是"等下一次" —— 放过去会让父 Run 拿到一个
        PG 里并不存在的结论。
        """
        _loop, child_id = self._spawned()
        with self.assertRaises(InvariantViolation) as cm:
            self.waker.wake(child_id)
        self.assertIn("D-6", str(cm.exception))

    def test_a_cancelled_child_is_not_reported_as_success(self) -> None:
        """第三种终态（S-15）：取消 ≠ 完成。父这一步要走失败那条路。

        派生之后**不驱动** —— 已经 COMPLETED 的 Run 再声明别的终态会被 B-3 挡下。
        """
        _loop, child_id = self._spawned()
        stack = self._stack_of(child_id)
        stack.loop._declare_terminal(AgentRunStatus.CANCELLED, reason="test cancelled")
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertEqual(handle.status, "cancelled")
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)
        self.assertIsNone(self._latest().pending_child_id)


# ---------------------------------------------------------------- 消费进程


@unittest.skipUnless(_APPS, "apps layer unavailable")
class ChildRunConsumerTest(unittest.TestCase):
    """`apps/child_run_consumer`：什么时候处理、什么时候才许说"消费完了"。"""

    def setUp(self) -> None:
        self.commits: list[int] = []
        self.woken: list[str] = []
        self.sweeps: list[int] = []
        self.expiry_sweeps: list[tuple] = []

        class FakeConsumer:
            def __init__(outer, events):  # noqa: N803
                outer.events = list(events)

            def subscribe(outer, topics):  # noqa: N803
                outer.subscribed = list(topics)

            def poll(outer, timeout=None):  # noqa: N803
                return list(outer.events)

            def commit(outer):  # noqa: N803
                self.commits.append(1)

        class FakeWaker:
            def wake(outer, child_run_id):  # noqa: N803
                self.woken.append(child_run_id)
                return ChildWakeOutcome.DELIVERED

            def sweep(outer, limit):  # noqa: N803
                self.sweeps.append(limit)
                return ChildWakeSweepResult()

        class FakeExpirer:
            def sweep(outer, now, limit):  # noqa: N803
                self.expiry_sweeps.append((now, limit))
                return WaitExpirySweepResult()

        self.consumer_cls = FakeConsumer
        self.waker = FakeWaker()
        self.expirer = FakeExpirer()

    def _app(self, events, **kwargs: Any) -> Any:
        return ChildRunConsumerApp(
            consumer=self.consumer_cls(events),
            waker=self.waker,
            processed=InMemoryProcessedEventStore(),
            expirer=self.expirer,
            config=ChildRunConsumerConfig(**kwargs),
            signal=ManualStop(),
            sleep=lambda _s: None,
        )

    def _child_event(self, child_run_id: str = "child_1") -> Any:
        return new_event(
            aggregate_type="run",
            aggregate_id="run_parent",
            event_type=CHILD_RUN_COMPLETED,
            payload={"child_run_id": child_run_id},
        )

    def test_only_child_run_events_are_handled(self) -> None:
        """白名单：同一个 topic 里会有几十种事件，不能靠"payload 里有没有"去猜。"""
        other = new_event(
            aggregate_type="execution",
            aggregate_id="exec_1",
            event_type="execution.completed",
            payload={"child_run_id": "child_1"},
        )
        app = self._app([other, self._child_event()])
        self.assertEqual(app.tick(), 1)
        self.assertEqual(self.woken, ["child_1"])

    def test_the_offset_is_committed_only_after_the_tick_commits(self) -> None:
        """顺序：PG 事务提交 → 才 commit offset。

        反过来是**丢处理**：Kafka 说消费完了，PG 说从没发生过，两边都不报错。
        """
        app = self._app([self._child_event()])
        app.runtime.run(app.tick, max_ticks=1)
        self.assertEqual(len(self.commits), 1)

    def test_a_failed_tick_commits_nothing(self) -> None:
        """控制组：事务回滚了就不许说"消费完了"。"""
        app = self._app([self._child_event()])
        original = app.tick

        def boom() -> int:
            original()
            raise RuntimeError("pg exploded after the handler ran")

        app.runtime.run(boom, max_ticks=1)
        self.assertEqual(self.commits, [], "回滚了还提交 offset = 永久丢处理")

    def test_the_sweep_fires_on_the_nth_tick(self) -> None:
        """PR-11 同款：计数必须活在跨 tick 的对象上，否则安全网一次都不触发。"""
        app = self._app([], sweep_every=3)
        app.runtime.run(app.tick, max_ticks=3)
        self.assertEqual(len(self.sweeps), 1)
        app.runtime.run(app.tick, max_ticks=2)
        self.assertEqual(len(self.sweeps), 1, "第 5 轮不该再扫")

    def test_an_event_without_a_child_run_id_is_refused(self) -> None:
        """一条说不出"哪条子 Run 完了"的完成事件：静默跳过 = 父 Run 永远挂着，
        而事件登记表上还写着"已处理"。"""
        app = self._app([self._child_event(child_run_id="")])
        with self.assertRaises(ValueError) as cm:
            app.tick()
        self.assertIn("child_run_id", str(cm.exception))

    def test_a_repeated_event_is_not_delivered_twice(self) -> None:
        event = self._child_event()
        app = self._app([event])
        app.tick()
        app.tick()
        self.assertEqual(self.woken, ["child_1"])

    def test_the_whitelist_covers_all_three_outcomes(self) -> None:
        """取消是**第三种**终态（S-15），漏掉它就永远没人叫醒父 Run。"""
        self.assertEqual(
            sorted(__import__(
                "apps.child_run_consumer.app", fromlist=["CHILD_RUN_EVENTS"]
            ).CHILD_RUN_EVENTS),
            sorted([CHILD_RUN_COMPLETED, CHILD_RUN_FAILED, CHILD_RUN_CANCELLED]),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
