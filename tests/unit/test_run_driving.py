"""M42 / 空洞 217：解开阻塞的人必须把 Run **往前推**（D-27）。

--------------------------------------------------------------------------
这个洞的形状，与它之前那些都不一样

空洞 229 是"**没人解开**"（父 Run 挂在一条死掉的子 Run 上）。
这一轮是"**解开了没人推**"：

    ChildRunWaker.wake()          交付结果 → 落快照 → 标记交付 → return
    ChildRunWaitExpirer.expire()  关闸门   → 落快照 → 标记到期 → return

两者都把父 Run 从"在等"变成"**可以被推一步**"，然后就撒手了。
`child_wait.py` 里那句注释写得毫不含糊：

    父 Run 从此可以被推一步 … 把下一步留给 step()

"留给 step()"就是**留给调用方**。跨进程部署里那个调用方不存在：
没有任何一个进程的 tick 会去问"有哪些 Run 刚刚被解开"。
界面上那条 Run 永远显示"运行中"，且没有任何报错。

所以它和 229 是**同一个现象**（一条永远走不下去的 Run），
病因却是第二个：那边没人解开，这边解开了没人推。

--------------------------------------------------------------------------
    D-27  谁解开一条 Run 的阻塞，谁就得把它推到下一个阻塞点或终态。
          "现在可以被推一步"不是一种状态 —— 它是把一件必须做的事
          推给了一个**不存在的调用方**
    D-28  推进之后必须落一个新的可恢复点。终态那条路径（`_declare_terminal`）
          自己**不落**快照，少了它"这个 Run 跑完了"在存储里根本不存在，
          而 R-3 会被绕开（一条已结束的 Run 被重新装载出来继续走）
    D-29  推进排在"标记处置完成"**之前**。推进是这条链路上最容易崩的一步
          （它在调模型、调工具、动外部世界），排反了崩溃就等于
          兜底扫再也不会碰它 —— 又是那个"永远走不下去"的形状
    D-30  父 Run COMPLETED ≠ "没人接过"。S-16 已经在那一刻把账本结案成
          NOT_NEEDED；此时再记一条 D-13 孤儿，账本会同时写着
          "不需要撤销"与"没人负责这笔副作用" —— 两句互相矛盾的话

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.business.run import AgentRunStatus
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.child_wake import ChildWakeOutcome
from packages.agent_runtime.child_wait import WaitExpiryOutcome
from packages.agent_runtime.driving import (
    DriveOutcome,
    InProcessRunDriver,
)
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.agent_runtime.saga import SagaCoordinator

from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)
from .test_child_wait_deadline import WaitWorld, _compensable


class _ExplodingDriver:
    """一步也推不动的驱动方：模拟"推进"这件事本身炸了。

    它不是"没有接上驱动方"（那是不给 `driver`，构造时就该炸），
    而是"接上了、但这一步崩了" —— 模型网关 500、工具超时、
    进程被 OOM kill，都是这个形状。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def drive(self, run_id: str) -> Any:
        self.calls.append(run_id)
        raise RuntimeError("model gateway is down")


class _CountingDriver:
    """只数它被叫了几次，什么都不推。用来验"推进是幂等的"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def drive(self, run_id: str) -> Any:
        self.calls.append(run_id)
        from packages.agent_runtime.driving import DriveResult

        return DriveResult(run_id=run_id, outcome=DriveOutcome.ADVANCED)


class DriveWorld(WaitWorld):
    """`WaitWorld` + 一个**脚本可换**的父 Run。

    `WakeWorld._parent()` 把脚本写死成一次委派，而 D-27 要验的恰恰是
    "推到**下一个阻塞点**"那一支 —— 那需要**两次**委派。
    """

    def setUp(self) -> None:
        super().setUp()
        self.script: list[Action] = [_delegation("researcher")]
        self.decisions = ScriptedDecisionEngine(list(self.script))

    def _factory(self, agent_id: str, approvals: Any = None) -> Any:
        """与 `WakeWorld` 唯一的区别：父 Run 的决策引擎是**共享**的那一个。

        `rebuild()` 会用这个工厂造一条全新的栈。给子 Run 一个空脚本没问题，
        但父 Run 必须拿到**同一份**脚本 —— 否则"推进"推的是一个
        "下一步无事可做"的 Run，"停在下一个阻塞点"这一支永远测不到
        （把测试写成"它果然一口气跑完了"）。
        """
        from packages.agent_runtime.assembly import assemble_runtime_stack
        from packages.agent_runtime.delegation import InProcessChildRunSpawner

        engine = self.decisions if agent_id == "parent" else ScriptedDecisionEngine([])
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=engine,
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, approvals=approvals, registry=self.registry
            ),
        )

    def _parent(
        self, script: list[Action] | None = None, run_id: str = "run_parent"
    ) -> AgentLoop:
        from packages.agent_runtime.assembly import assemble_runtime_stack
        from packages.agent_runtime.delegation import InProcessChildRunSpawner

        self.script = [
            replace(a, run_id=run_id) if a.run_id == "run_parent" else a
            for a in (script or self.script)
        ]
        self.decisions = ScriptedDecisionEngine(list(self.script))
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=self.decisions,
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

    def _spawned(
        self, script: list[Action] | None = None, run_id: str = "run_parent"
    ) -> tuple[AgentLoop, str]:
        self._stacks = {}
        loop = self._parent(script, run_id=run_id)
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        child_id = loop.pending_child.child_run_id
        assert loop.spawner is not None
        self._stacks[child_id] = loop.spawner.stack_for(child_id)
        return loop, child_id

    def _stack_of(self, child_id: str) -> Any:
        return self._stacks[child_id]

    def _latest(self) -> Any:
        snapshot = self.snapshots.latest("run_parent")
        assert snapshot is not None, "父 Run 一份快照都没落"
        return snapshot

    def _orphans(self) -> list[Any]:
        return [
            r
            for r in self.compensations._by_id.values()  # type: ignore[attr-access]
            if "D-13" in (r.reason or "")
        ]


def _delegation(target: str = "researcher") -> Action:
    return Action(
        run_id="run_parent",
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": target},
    )


# ---------------------------------------------------------------- 端口


class TheDriverIsNotOptionalTest(unittest.TestCase):
    """`driver` **没有默认值** —— 没接上必须在装配时炸，不许静默退化。

    给默认值 = "解开了但没人推"变成一种**合法的装配**。那正是空洞 217 的
    形状：不报错，只是父 Run 停在"可以被推一步"那一格永远不动。
    与 `saga` 同款理由（空洞 212~214）：漏接不报错，只是东西消失。
    """

    def setUp(self) -> None:
        from packages.agent_runtime.child_wake import ChildRunWaker
        from packages.agent_runtime.child_wait import ChildRunWaitExpirer
        from packages.agent_runtime.delegation import ChildRunRegistry
        from packages.agent_runtime.recovery import (
            InMemoryRunSnapshotStore,
            RunRecovery,
        )
        from packages.agent_runtime.saga import InMemoryCompensationStore

        self.kwargs = dict(
            registry=ChildRunRegistry(),
            recovery=RunRecovery(
                snapshots=InMemoryRunSnapshotStore(),
                factory=lambda *a, **k: None,
                approvals=None,
            ),
            saga=SagaCoordinator(store=InMemoryCompensationStore()),
        )
        self.waker_cls = ChildRunWaker
        self.expirer_cls = ChildRunWaitExpirer

    def test_a_waker_cannot_be_built_without_a_driver(self) -> None:
        with self.assertRaises(TypeError):
            self.waker_cls(**self.kwargs)  # type: ignore[arg-type]

    def test_an_expirer_cannot_be_built_without_a_driver(self) -> None:
        with self.assertRaises(TypeError):
            self.expirer_cls(**self.kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- D-27


class D27TheUnblockerDrivesTest(DriveWorld):
    """交回结果 / 关掉闸门的那个人，必须**自己**把父 Run 推走。"""

    def test_the_parent_reaches_a_terminal_state_with_nobody_calling_step(self) -> None:
        """这一条是整个文件的地基：没有任何人调 `step()`，它自己跑完了。

        D-27 之前，`wake()` 返回之后最新那份快照的 status 还是 running ——
        那条 Run 从此再也不会前进一步，而界面上显示"运行中"。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)
        self.assertIs(self._latest().status, AgentRunStatus.COMPLETED.value)

    def test_the_expiry_path_drives_it_too(self) -> None:
        """D-27 的第二个副本：到期器也不许"解开就走"。

        两条路径各写一份推进就会有两个"一个 Run 什么时候算走完了"的答案
        （B-7），所以它们共用同一个 `RunDriver` —— 这里验的是
        到期那一支**确实**接上了。
        """
        _loop, child_id = self._gone()

        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.EXPIRED)
        self.assertTrue(self._latest().is_terminal)

    def test_it_stops_at_the_next_blocking_point_when_there_is_one(self) -> None:
        """D-27 说的是"下一个阻塞点**或**终态" —— 这一支是前者。

        两步委派：第一步的结果回来之后，父 Run 该走到第二步并**再次挂起**
        （在等第二条子 Run），而不是"推完了但停在半路"。
        """
        loop, first = self._spawned([_delegation("researcher"), _delegation("writer")])
        self._stack_of(first).loop.run()
        self.waker.wake(first)

        latest = self._latest()
        self.assertIs(latest.status, AgentRunStatus.SUSPENDED.value)
        self.assertIsNotNone(latest.pending_child_id, "停在第二个阻塞点上")
        self.assertNotEqual(latest.pending_child_id, first)

        # 而且它真的能继续：第二条子 Run 跑完之后父 Run 才进终态
        second = latest.pending_child_id
        self.registry.mark_finished(second, "completed", {"summary": "写完了"})
        self.waker.wake(second)
        self.assertIs(self._latest().status, AgentRunStatus.COMPLETED.value)

    def test_the_result_says_where_the_run_stopped(self) -> None:
        """`DriveResult.last_outcome`：停在哪必须说得出来。

        "停了"有三种（挂起 / 完成 / 预算耗尽），
        只返回一个"推进成功"会把它们并成一个数（PR-19）。
        """
        _loop, child_id = self._spawned([_delegation("researcher"), _delegation("writer")])
        self._stack_of(child_id).loop.run()

        driver = InProcessRunDriver(recovery=self.recovery)
        result = driver.drive("run_parent")
        self.assertIs(result.outcome, DriveOutcome.ADVANCED)
        self.assertEqual(result.last_outcome, StepOutcome.WAITING_CHILD.value)

    def test_a_terminal_run_is_not_driven(self) -> None:
        """R-3：终态没有可推的东西。这不是错误，是"它已经结束了"。

        做成异常会让唤醒路径上多一条 `try`，而那条 `try` 里什么也做不了 ——
        那正是 D-27 要消灭的"假装交给了谁"。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)
        self.assertTrue(self._latest().is_terminal)

        driver = InProcessRunDriver(recovery=self.recovery)
        result = driver.drive("run_parent")
        self.assertIs(result.outcome, DriveOutcome.TERMINAL)
        self.assertEqual(result.last_outcome, "")


# ---------------------------------------------------------------- D-28


class D28ANewRecoverablePointTest(DriveWorld):
    """推进之后必须落一个新的可恢复点 —— 终态那条路径自己不落。"""

    def test_a_snapshot_is_saved_after_the_drive(self) -> None:
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        before = len(self.snapshots.list_for("run_parent"))

        self.waker.wake(child_id)

        self.assertGreater(len(self.snapshots.list_for("run_parent")), before)

    def test_the_reason_says_where_it_stopped(self) -> None:
        """快照的 `reason` 不许只写"推过了"。

        运维查"这条 Run 为什么停在这"看的就是这一列。
        写成 `driven` 等于什么都没说（PR-19 那一类）。
        """
        _loop, child_id = self._spawned([_delegation("researcher"), _delegation("writer")])
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)

        self.assertIn("stopped at", self._latest().reason)
        self.assertIn(StepOutcome.WAITING_CHILD.value, self._latest().reason)

    def test_r3_is_not_routed_around(self) -> None:
        """**没有**这份快照会发生什么：一条已经结束的 Run 被重新装载出来继续走。

        `_declare_terminal()` 自己不落快照，于是最新那份仍然是挂起时那一帧
        （`is_terminal` 为 False）。下一次 `rebuild()` 会**成功** ——
        而它装载出来的是一条早就跑完的 Run。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)
        self.assertTrue(self._latest().is_terminal, "前提：这份快照是终态的")

        # 把这份终态快照抹掉，回到 D-28 之前的样子
        terminal = self._latest()
        self.snapshots._items.remove(terminal)
        self.assertFalse(self._latest().is_terminal, "前提：回到'最新快照不是终态'")

        rebuilt = self.recovery.rebuild("run_parent")
        self.assertIsNotNone(rebuilt, "一条已经结束的 Run 被重新装载出来了 —— R-3 被绕开")


# ---------------------------------------------------------------- D-29


class D29DriveBeforeTheMarkTest(DriveWorld):
    """推进排在"标记处置完成"之前 —— 崩在这一步只等于下一轮再来一次。"""

    def test_a_crash_in_the_drive_leaves_the_child_undelivered(self) -> None:
        """那条子 Run 必须**留在** `undelivered()` 里。

        排反了的话，`delivered_at` 已经写了 ⟹ 兜底扫再也不会碰它
        ⟹ 父 Run 停在半路，界面显示"运行中" —— 又回到 217 那个形状。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.driver = _ExplodingDriver()

        with self.assertRaises(RuntimeError):
            self.waker.wake(child_id)

        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.assertFalse(handle.is_delivered)
        self.assertIn(
            child_id, [h.child_run_id for h in self.registry.undelivered()],
            "下一轮兜底扫必须还能扫到它",
        )

    def test_the_control_a_clean_drive_marks_it_delivered(self) -> None:
        """控制组：没崩的时候它确实被结掉了（不留着每轮重扫）。"""
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)
        self.assertEqual([h.child_run_id for h in self.registry.undelivered()], [])

    def test_the_expiry_path_keeps_it_in_the_overdue_queue_too(self) -> None:
        """D-29 的第二个副本：崩在推进上，这条派生必须留在 `overdue()` 里。"""
        _loop, child_id = self._gone()
        self.expirer.driver = _ExplodingDriver()

        with self.assertRaises(RuntimeError):
            self.expirer.expire(child_id)

        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertIn(child_id, [h.child_run_id for h in self.registry.overdue(now)])

    def test_the_retry_after_a_crash_drives_the_parent(self) -> None:
        """D-27 在"已经解开过"那一支：上一轮崩在推进上，重扫时**也要推**。

        那一支的入口是"父 Run 已经不等它了"（闸门上一轮就关了），
        于是它很容易被写成"不用再管" —— 而那正是崩过的那一轮
        唯一会被再扫到的路径。
        """
        _loop, child_id = self._gone()
        self.expirer.driver = _ExplodingDriver()
        with self.assertRaises(RuntimeError):
            self.expirer.expire(child_id)
        # 崩在推进上：闸门关了、快照落了，但 `wait_expired_at` 还没写
        self.assertFalse(self.registry.for_child(child_id).is_wait_expired)

        counting = _CountingDriver()
        self.expirer.driver = counting
        self.assertIs(self.expirer.expire(child_id), WaitExpiryOutcome.ALREADY_EXPIRED)
        self.assertEqual(counting.calls, ["run_parent"], "重扫时也得推一把（D-27）")


# ---------------------------------------------------------------- D-30


class D30CompletedIsNotUnclaimedTest(DriveWorld):
    """父 Run COMPLETED ≠ "没人接过" —— 其余终态仍然是孤儿。"""

    def test_a_completed_parent_means_already_delivered(self) -> None:
        """那一瞬间：推进跑完了、`mark_delivered` 还没写进去就崩了。

        下一轮兜底扫会再撞上来。那时不许把它说成 D-13 孤儿 ——
        S-16 已经在父 Run 完成那一刻把账本结案成 NOT_NEEDED，
        再记一条"没人负责这笔副作用"就是让账本自相矛盾。

        ------------------------------------------------------------------
        断言"没有孤儿"必须是**有内容的**

        用不带逆操作声明的委派来验"没记孤儿"是假绿（S-1：没有可记的账）。
        所以这一支也要用带 `compensation` 的委派，并且顺带钉住
        "账本不是空的，是被结案了" —— 空的和结案的是两回事，
        后者才说明"有人接过"。
        """
        _loop, child_id = self._spawned([_compensable("run_parent")])
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)
        # 模拟崩溃：`delivered_at` 被回滚了
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.registry._store(replace(handle, delivered_at=None))

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(self._orphans(), [], "跑完的父 Run 不该留下孤儿账")

        rows = list(self.compensations._by_id.values())  # type: ignore[attr-access]
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].status, CompensationStatus.NOT_NEEDED)

    def test_a_cancelled_parent_is_still_an_orphan(self) -> None:
        """控制组：D-30 只豁免 COMPLETED。

        父 Run 被叫停 ⟹ 它**没有**等到这条子 Run 的结果 ⟹
        那笔副作用真的没人认领（S-15：取消不自动撤销，账本还开着）。
        把它也算成"已交付"，孤儿就从账本里消失了。

        ------------------------------------------------------------------
        这一支必须用**带逆操作声明**的委派

        S-1：账本只记"有可记的账"。没声明 `compensation` 的 Action
        `record()` 返回 None，于是"有没有记孤儿"这条断言会**假绿** ——
        这是空洞 212~214 反复踩过的那个坑。
        """
        loop, child_id = self._spawned([_compensable("run_parent")])
        # 子 Run 跑到**别的机器**上去了：父 Run 叫不停它，只能登记意图（D-14）。
        # 于是取消路径不会替它结掉交付（B-9 第三半只结"它说自己停了"那一支）。
        del loop.spawner._stacks[child_id]  # type: ignore[union-attr]
        loop.cancel(reason="user pressed stop", by="u1")
        self.assertIs(self._latest().status, AgentRunStatus.CANCELLED.value)

        # 它后来说自己跑完了（脸 B：取消改不动既成事实，D-15）
        self.registry.mark_finished(child_id, "completed", {"summary": "done"})

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.PARENT_TERMINAL)
        self.assertEqual(len(self._orphans()), 1, "取消的父 Run 必须留下孤儿账")


# ---------------------------------------------------------------- 幂等


class DrivingIsIdempotentTest(DriveWorld):
    """推进可以被重试 —— 兜底扫每轮都会撞上同一批。"""

    def test_driving_a_run_that_was_already_driven_changes_nothing(self) -> None:
        """兜底扫每轮都会撞上同一批 —— 第二次进来必须是**空的**。

        第二次进来父 Run 已经是终态 ⟹ `DriveOutcome.TERMINAL`，不推、
        不多落一份快照。否则每扫一轮就多一份"推过了"的快照，
        而它们的 `reason` 全都一样 —— 看板上是噪音，排查时是干扰。
        """
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        self.waker.wake(child_id)  # 这一轮里已经推过了
        before = len(self.snapshots.list_for("run_parent"))

        result = InProcessRunDriver(recovery=self.recovery).drive("run_parent")

        self.assertIs(result.outcome, DriveOutcome.TERMINAL)
        self.assertEqual(len(self.snapshots.list_for("run_parent")), before)

    def test_a_wake_that_already_drove_still_marks_it_delivered(self) -> None:
        """`pending_child is None` 那一支：推一把是幂等的，然后照常结掉。"""
        _loop, child_id = self._spawned()
        self._stack_of(child_id).loop.run()
        counting = _CountingDriver()
        self.waker.driver = counting
        self.waker.wake(child_id)

        # 崩溃在同一瞬间：推进过了、mark 没写
        handle = self.registry.for_child(child_id)
        assert handle is not None
        self.registry._store(replace(handle, delivered_at=None))

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.ALREADY_DELIVERED)
        self.assertEqual(len(counting.calls), 2, "两次都推了 —— 推一把是幂等的")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
