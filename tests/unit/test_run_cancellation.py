"""M33 / 空洞 221：一条 Run 到底能不能被叫停。

--------------------------------------------------------------------------
这个洞的形状

D-13 处理的是"父 Run 已终态、子 Run 还在跑"的孤儿。
可那个状态在 M33 之前**只能靠直接改快照造出来** ——
换句话说：一条 Run 在 AgentOS 里根本不能被叫停。

取消只能从外部发生（kill -9、手工改库、把策略改成 REQUIRE_APPROVAL 再驳回）。
于是"把一条正在跑的 Run 停下来"这个动作的实现者是**运维**，不是系统：
它能派生子 Run（M25）、能挂起等人审批（M18），
唯独没有"算了，别跑了"。

而 D-13 那种局面 —— 父已终态、子在跑 ——
恰恰是取消缺位时**唯一**会发生的局面。

--------------------------------------------------------------------------
    B-8  取消是 Run 级动作，必须有入口，且必须说得清**谁**叫停、**为什么**
          （A-8 同款：匿名取消进不了审计）
    B-9  取消**级联**到我正在等的那条子 Run；顺序先子后父；
          叫停之后必须把它结掉（否则 sweep 永远捞它）
    B-10 终态 Run 不可取消（B-3 的另一半）；取消之后必须落终态快照（R-1）

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.errors import IllegalTransition, InvariantViolation
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.loop import StepOutcome
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator

from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)

from .test_delegation_compensation import LedgerWorld, _delegation


class CancelWorld(LedgerWorld):
    """复用 M32 那个"父子都真跑、账本只有一本"的世界。

    之所以能直接用它：取消要验证的正是**同一组**东西 ——
    子 Run 有没有真的停、账本有没有记、快照有没有落。
    另搭一个世界就意味着"一个真跑起来的父子世界"有了第二个定义。
    """

    def _waker(self) -> ChildRunWaker:
        return ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),

            driver=InProcessRunDriver(recovery=self.recovery),
        )

    def _traces(self, kind: str) -> list[Any]:
        loop = getattr(self, "_loop", None)
        assert loop is not None
        return [e for e in loop.trace.entries if e.kind == kind]


# ---------------------------------------------------------------- B-8


class B8ThereIsAnEntryPointTest(CancelWorld):
    def test_a_run_can_be_stopped(self) -> None:
        """B-8 的主断言：有入口，而且停是真的停了。"""
        loop, _child = self._spawned()
        self._loop = loop

        outcome = loop.cancel(reason="user changed their mind", by="alice")

        self.assertIs(outcome, StepOutcome.CANCELLED)
        assert loop.agent_run is not None
        self.assertIs(loop.agent_run.status, AgentRunStatus.CANCELLED)

    def test_reason_is_required(self) -> None:
        """说不出为什么 = 事后没人回答得了为什么这一条跑了一半。"""
        loop, _child = self._spawned()
        with self.assertRaises(InvariantViolation) as cm:
            loop.cancel(reason="", by="alice")
        self.assertIn("B-8", str(cm.exception))

    def test_by_is_required(self) -> None:
        """A-8 同款：匿名取消进不了审计。"""
        loop, _child = self._spawned()
        with self.assertRaises(InvariantViolation) as cm:
            loop.cancel(reason="because", by="")
        self.assertIn("B-8", str(cm.exception))

    def test_the_cancellation_is_a_fact_in_the_state(self) -> None:
        """不留这条 Observation，恢复出来的 Run 不知道自己被叫停过。"""
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="alice")

        last = loop.state.observations[-1]
        self.assertEqual(last.kind, "run.cancelled")
        self.assertEqual(last.content["by"], "alice")
        self.assertEqual(last.content["reason"], "because")

    def test_the_trace_says_who_stopped_it(self) -> None:
        """审计的第一入口是 Trace，不是 State。"""
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="bob")

        entries = self._traces("run.cancelled")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].payload["by"], "bob")
        self.assertEqual(entries[0].payload["reason"], "because")

    def test_the_gated_execution_is_really_dead(self) -> None:
        """不判死它，那条 SUSPENDED 会永远挂着 —— 它等的人不会来了。"""
        loop, _child = self._spawned()
        self._loop = loop
        execution_id = loop.pending_child.parent_execution_id  # type: ignore[union-attr]

        loop.cancel(reason="because", by="alice")

        self.assertIs(self.kernel.status_of(execution_id), ExecutionStatus.CANCELLED)

    def test_it_is_not_marked_failed(self) -> None:
        """判成 FAILED 会让排障的人去找"它为什么失败"（PR-19 同款）。"""
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="alice")

        kinds = [e.kind for e in loop.trace.entries]
        self.assertNotIn("execution.failed", kinds)

    def test_a_run_that_is_waiting_for_nobody_can_also_be_stopped(self) -> None:
        """控制组：没有在等子 Run 的 Run 同样叫得停（B-9 不是唯一路径）。"""
        loop = self._parent()
        self._loop = loop

        outcome = loop.cancel(reason="never mind", by="alice")

        self.assertIs(outcome, StepOutcome.CANCELLED)
        assert loop.agent_run is not None
        self.assertIs(loop.agent_run.status, AgentRunStatus.CANCELLED)


# ---------------------------------------------------------------- B-9


class B9CascadeTest(CancelWorld):
    def test_the_child_is_stopped_too(self) -> None:
        """B-9 的主断言：停止不是停一半。

        父 Run 判了 CANCELLED 而子 Run 还在跑的话，它继续花钱、
        继续产生副作用，而它的结果再也交不回来。
        """
        loop, child_id = self._spawned()
        self._loop = loop

        loop.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertEqual(handle.status, "cancelled")
        self.assertTrue(handle.is_finished)

    def test_the_child_knows_it_was_cancelled(self) -> None:
        """进程内那条子 Run 是**自己**被叫停的 —— 它会关自己的闸门、记自己的账。"""
        loop, child_id = self._spawned()
        self._loop = loop

        loop.cancel(reason="because", by="alice")

        # 从 spawner 手里拿回那条真的 stack
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        stack = spawner.stack_for(child_id)
        assert stack is not None
        self.assertIs(stack.loop.agent_run.status, AgentRunStatus.CANCELLED)

    def test_the_cascade_is_recursive(self) -> None:
        """孙 Run 也要停 —— 靠的是子 Run **自己**走一遍 `cancel()`。"""
        loop, child_id = self._spawned()
        self._loop = loop

        # 让"researcher"这条子 Run 也派一次活，于是有一条孙 Run
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        child_stack = spawner.stack_for(child_id)
        assert child_stack is not None
        child_stack.loop.decision_engine = ScriptedDecisionEngine([_delegation("writer")])
        child_stack.loop.spawner = InProcessChildRunSpawner(
            factory=self._factory, registry=self.registry
        )
        self.assertIs(child_stack.loop.step(), StepOutcome.WAITING_CHILD)
        grandchild_id = child_stack.loop.pending_child.child_run_id  # type: ignore[union-attr]

        loop.cancel(reason="because", by="alice")

        grandchild = self.registry.for_child(grandchild_id)
        assert grandchild is not None
        self.assertEqual(grandchild.status, "cancelled")

    def test_the_child_is_closed_out(self) -> None:
        """B-9 的第三半：叫停之后必须把它结掉。

        不结的话它永远留在 `undelivered()` 里，兜底扫每轮捞它一次，
        而父 Run 已终态 ⟹ 每轮记一条 D-13 孤儿。
        """
        loop, child_id = self._spawned()
        self._loop = loop

        loop.cancel(reason="because", by="alice")

        # 先钉住"它确实终态了"，再钉住"它不再被扫出来"。
        # 少了前一句，这条断言在"压根没叫停子 Run"时也绿 ——
        # `undelivered()` 只捞"已终态未交付"，没终态的自然不在里面。
        # 那种绿是**空过**，比红更危险。
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_finished)
        self.assertTrue(handle.is_delivered)

        self.assertEqual(
            [h.child_run_id for h in self.registry.undelivered()],
            [],
            "已叫停的子 Run 不该再被扫出来",
        )
        self.assertEqual(self._waker().sweep().total, 0)

    def test_the_ledger_knows_the_delegation_may_have_left_something(self) -> None:
        """D-12：级联取消之后，这次委派的副作用仍然是一笔"存疑"。"""
        loop, _child = self._spawned()
        self._loop = loop

        loop.cancel(reason="because", by="alice")

        unresolved = self.compensations.unresolved_for("run_parent")
        self.assertEqual(len(unresolved), 1)
        self.assertIn("D-12", unresolved[0].reason)
        self.assertIn("cancelled", unresolved[0].reason)

    def test_a_spawner_that_cannot_stop_what_it_started_is_refused(self) -> None:
        """PR-26 同款：起得出却叫不停的 spawner 必须喊出来，不许跳过。"""
        loop, _child = self._spawned()
        self._loop = loop

        class _SpawnOnlySpawner:
            """只有 `spawn` —— 正是 M33 之前所有 spawner 的形状。"""

            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.registry = inner.registry

            def spawn(self, request: Any) -> Any:
                return self._inner.spawn(request)

        loop.spawner = _SpawnOnlySpawner(loop.spawner)

        with self.assertRaises(InvariantViolation) as cm:
            loop.cancel(reason="because", by="alice")
        self.assertIn("B-9", str(cm.exception))

    def test_cancelling_does_not_undo_anything(self) -> None:
        """控制组：S-15 —— 取消不自动回滚。取消是"到此为止"，不是"撤销重来"。

        判据：账本里没有任何一条被真正执行过的撤销
        （`compensation.done` 只在撤销动作跑完时才记）。
        """
        loop, _child = self._spawned()
        self._loop = loop

        loop.cancel(reason="because", by="alice")

        self.assertEqual(self._traces("compensation.done"), [])


# ---------------------------------------------------------------- B-10


class B10TerminalIsTerminalTest(CancelWorld):
    def test_a_completed_run_cannot_be_cancelled(self) -> None:
        """B-3 的另一半：终态不可改终态。

        静默返回"取消成功"会骗人 —— 调用方以为自己按停了，
        而那条 Run 其实早就停了，而且可能是 COMPLETED。
        """
        loop = self._parent([])          # 空脚本：没有动作要做 → FINISHED
        self._loop = loop
        loop.run()
        assert loop.agent_run is not None
        self.assertIs(loop.agent_run.status, AgentRunStatus.COMPLETED)
        self.assertTrue(loop.agent_run.is_terminal)

        with self.assertRaises(InvariantViolation) as cm:
            loop.cancel(reason="too late", by="alice")
        self.assertIn("B-10", str(cm.exception))

    def test_a_cancelled_run_cannot_be_cancelled_twice(self) -> None:
        """控制组：第二次取消必须是"叫不停"，不是"再停一次"。"""
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="alice")

        with self.assertRaises(InvariantViolation) as cm:
            loop.cancel(reason="again", by="bob")
        self.assertIn("B-10", str(cm.exception))

    def test_a_cancelled_run_leaves_a_terminal_snapshot(self) -> None:
        """R-1：终态也要落快照。

        少了这一步，取消只改了内存：重启后恢复出来的是一条 RUNNING 的 Run
        （快照还停在挂起那一帧），它会被继续推进 —— 取消等于没发生。
        """
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="alice")

        snap = self.snapshots.latest("run_parent")
        assert snap is not None
        self.assertEqual(snap.status, AgentRunStatus.CANCELLED.value)

    def test_a_cancelled_run_cannot_be_recovered(self) -> None:
        """R-3：终态 Run 不重建。取消之后它不该再被推进。"""
        loop, _child = self._spawned()
        self._loop = loop
        loop.cancel(reason="because", by="alice")

        with self.assertRaises(IllegalTransition):
            self.recovery.rebuild("run_parent")


# ------------------------------------------------------- 跨进程那条路


class CancellingWithoutTheChildInMemoryTest(CancelWorld):
    """子 Run 在**别的进程**里：stack 不在内存，只有登记处。

    这条路必须也能走通 —— 而且它走的正是生产里真正会发生的那条：
    跨进程部署下父 Run 手里只有 `child_run_id`，没有子 Run 的对象。
    """

    def test_the_registry_is_enough(self) -> None:
        """叫不停它的对象，也要叫得停它的**记录**（X-5：PG 是唯一 Truth）。

        D-14 之后"叫得停它的记录"指的是**请求写进去了**，
        不是"终态被改写了" —— 终态只有那条子 Run 自己能写。
        """
        loop, child_id = self._spawned()
        self._loop = loop

        # 抹掉进程内的 stack，模拟"子 Run 在别的进程里"
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        spawner._stacks.clear()

        loop.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_cancel_requested)
        self.assertIn("because", handle.cancel_reason)
        self.assertEqual(handle.cancel_requested_by, "alice")

        assert loop.agent_run is not None
        self.assertIs(loop.agent_run.status, AgentRunStatus.CANCELLED)

    def test_the_parent_does_not_declare_the_childs_outcome(self) -> None:
        """D-14：父 Run **不替**子 Run 宣告终态。

        这一条是空洞 224 的脸 A 的回归测试。旧实现在这里把登记处写成
        `cancelled`，于是那条还在跑的子 Run 跑完时撞 B-3 抛异常，
        真实结果（含 S-1 的撤销参数）丢失。
        """
        loop, child_id = self._spawned()
        self._loop = loop
        spawner = loop.spawner
        assert isinstance(spawner, InProcessChildRunSpawner)
        spawner._stacks.clear()

        loop.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_finished)
        self.assertNotEqual(handle.status, "cancelled")

    def test_a_fresh_parent_still_cascades(self) -> None:
        """从快照重建出来的父 Run（spawner 是新的）照样能级联。

        M32 里"父 Run 已终态"只能靠改快照造出来，本轮之后它是真路径：
        重建 → 叫停 → 请求照样落到登记处。
        """
        loop, child_id = self._spawned()
        self._loop = loop
        self._make_snapshot()

        rebuilt = self.recovery.rebuild("run_parent").loop
        self.assertIsNotNone(rebuilt.pending_child)

        rebuilt.cancel(reason="because", by="alice")

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertTrue(handle.is_cancel_requested)

    def _make_snapshot(self) -> None:
        snap = self.snapshots.latest("run_parent")
        if snap is None:
            stack = assemble_runtime_stack(
                agent_id="parent",
                interpreter=ScriptedInterpreter(),
                planner=ScriptedPlanner(),
                decision_engine=self.decisions,
                gateway=_gateway(),
                tool_runtime=_tool_runtime(),
                kernel=self.kernel,
                snapshots=self.snapshots,
                compensations=InMemoryCompensationStore(),
                spawner=InProcessChildRunSpawner(
                    factory=self._factory, registry=self.registry
                ),
            )
            stack.loop.start("go", run_id="run_parent")
            stack.loop.step()
            self.snapshots.save(stack.loop.capture(reason="waiting for child"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
