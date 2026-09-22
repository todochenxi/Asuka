"""M86 / R-7：可恢复点必须带走**注入的智能实现**的进度。

--------------------------------------------------------------------------
洞的形状（probe86.py 实证，不是推演）

`RunSnapshot` 的 docstring 写着它是"一次可恢复点的**全部**数据"。
那句话只覆盖了 Runtime 自己的内存状态 —— 而装配层还注入进来两个
**Intelligence** 实现（Planner / DecisionEngine），它们可以是**有状态的**。

`examples/demo_stack.py` 自己就承认了：

    # 每个 Run 一个 DecisionEngine：它是**有状态的**（第几步了），
    # 跨 Run 共享会让第二个 Run 直接 FINISH。

★ 那句承诺只管住了"跨 Run"，**管不住同一 Run 的一次恢复**。

--------------------------------------------------------------------------
本文件要守住什么

    R-7：有状态的 Intelligence 实现必须自述进度；恢复时对不上 → 拒绝恢复。

三条一起才成立（缺任何一条，这条不变量都能被绕过）：

    1. 无状态实现（`progress()` 返回 None）**照样能恢复** —— 这是控制组。
       少了它，第 2 条可能只是"恢复整个坏掉了"。
    2. 有状态实现换了进度 → **点名拒绝**，且理由里说清是哪个 Port。
       这守住"静默重来"这件事本身。
    3. 快照往返（capture → 落库那一层的字典形状 → restore）之后仍然成立。
       只在内存里成立的保证，在生产上不成立（坑 #5）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business.snapshot import RunSnapshot, state_to_dict
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_domain.errors import InvariantViolation
from packages.agent_runtime.loop import AgentLoop

from .helpers import new_run


class _StatelessPlanner:
    """无状态：每次从 state 重算。**不实现** progress()。"""

    def plan(self, state: State) -> Plan:
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="go"),))


class _StatelessEngine:
    """无状态：每次从 state 重算。**不实现** progress()。"""

    def decide(self, state: State) -> Decision:
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            rationale="stateless",
        )


class _CountingEngine:
    """有状态：靠一个进程内计数器决定第几步。**实现** progress()。

    与 `DemoDecisionEngine` 同一形状 —— 刻意不复用那个类：
    测试要能独立地设置"走到第几步了"，而不是靠真的跑一遍。
    """

    def __init__(self, calls: int = 0) -> None:
        self.calls = calls

    def progress(self) -> object:
        return {"calls": self.calls}

    def decide(self, state: State) -> Decision:
        self.calls += 1
        if self.calls == 1:
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.LLM_CALL,
                payload={"prompt": "hello"},
            )
        else:
            action = Action(run_id=state.run_id, action_type=ActionType.FINISH)
        return Decision(run_id=state.run_id, selected_action=action, rationale="counting")


class _CountingPlanner:
    """有状态的规划器（实现 progress()）—— 用来验"两个 Port 都查"。"""

    def __init__(self, calls: int = 0) -> None:
        self.calls = calls

    def progress(self) -> object:
        return {"calls": self.calls}

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="go"),))


def _budgeted_state(run_id: str) -> State:
    return State(
        run_id=run_id,
        goal=Goal(
            run_id=run_id,
            objective="answer",
            success_criteria=("an answer is produced",),
            budget=Budget(max_steps=5),
        ),
    )


class _Base(unittest.TestCase):
    def _snapshot_for(
        self,
        *,
        progress: dict | None = None,
    ) -> RunSnapshot:
        """造一份**真能恢复**的快照。

        刻意走 `state_to_dict` 而不是手搓 `{"run_id": …}` ——
        后者过一个 `state_from_dict` 就会因为 `Goal.objective` 缺失炸掉
        （I-1），而那时我以为自己在测"进度对不对"。
        **快照要真的能恢复，"能不能恢复"这条控制组才有意义。**
        """
        run_id = new_run()
        return RunSnapshot(
            run_id=run_id,
            agent_id="agent-t",
            status="RUNNING",
            state=state_to_dict(_budgeted_state(run_id)),
            progress=progress or {},
        )

    def _loop(self, *, planner: object, engine: object) -> AgentLoop:
        from tests.unit.test_agent_loop import MinimalLoopTest, ScriptedInterpreter

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        return AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=planner,
            decision_engine=engine,
        )


class StatelessImplementationsStillRestoreTest(_Base):
    """控制组：无状态实现**照样能恢复**。

    少了这一组，下面那些"拒绝"的用例可能只是"恢复整个坏掉了" ——
    它们会全绿，而它们什么都没守住。
    """

    def test_a_stateless_planner_and_engine_restore_without_complaint(self) -> None:
        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        snapshot = self._snapshot_for()

        loop.restore(snapshot)          # 不该抛

        self.assertEqual(loop.state is not None, True)

    def test_stateless_ports_capture_as_none(self) -> None:
        """快照里记的是 None —— 表示"我随时可以从 state 重算"。"""
        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        loop.start("2+3=?")

        captured = loop.capture(reason="test")

        self.assertEqual(
            captured.progress, {"planner": None, "decision_engine": None}
        )


class AStatefulImplementationMustMatchTest(_Base):
    """核心：有状态实现换了进度 → 点名拒绝。"""

    def test_matching_progress_restores(self) -> None:
        """对得上就照常恢复 —— 否认了"一律拒绝"那种懒做法。"""
        engine = _CountingEngine(calls=2)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 2}}
        )

        loop.restore(snapshot)          # 不该抛

    def test_a_rewound_engine_is_refused(self) -> None:
        """恢复成一个"从未走过"的引擎 → 拒绝。

        这就是 probe86 实测的那一幕：那会让这个 Run **又调一次模型**。
        """
        engine = _CountingEngine(calls=0)     # 全新的引擎（进程重启后的样子）
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 2}}
        )

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        # 理由必须**点名**是哪个 Port，否则调用方只知道"恢复失败了"
        self.assertIn("decision_engine", str(ctx.exception))

    def test_the_refusal_says_both_values(self) -> None:
        """拒绝理由要说清"快照说什么、现在是什么"。"""
        engine = _CountingEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 7}}
        )

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        message = str(ctx.exception)
        self.assertIn("7", message)       # 快照里的
        self.assertIn("0", message)       # 当前的

    def test_a_stateful_planner_is_checked_too(self) -> None:
        """两个 Port 都要查 —— 只查引擎，换掉规划器就没人管。"""
        loop = self._loop(planner=_CountingPlanner(calls=0), engine=_StatelessEngine())
        snapshot = self._snapshot_for(
            progress={"planner": {"calls": 3}, "decision_engine": None}
        )

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        self.assertIn("planner", str(ctx.exception))

    def test_swapping_a_stateless_run_onto_a_stateful_engine_is_refused(self) -> None:
        """快照说"没带走进度"，而当前实现自述了值 → 也是对不上。

        那意味着这条 Run 是在一个无状态引擎下跑起来的，
        换成有状态的接管一样是**换了一个世界**。
        """
        engine = _CountingEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(progress={})       # 018 之前的历史行/无状态

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        self.assertIn("decision_engine", str(ctx.exception))


class TheProgressSurvivesTheCorpusTest(_Base):
    """快照往返：进快照 → 出来还是那个值。"""

    def test_captured_progress_is_the_engines_own_words(self) -> None:
        engine = _CountingEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        loop.start("2+3=?")

        captured = loop.capture(reason="test")

        self.assertEqual(
            captured.progress["decision_engine"], {"calls": 0}
        )

    def test_progress_advances_with_the_engine(self) -> None:
        """引擎走一步，快照里的进度跟着变 —— 否则它记的是个常数。"""
        engine = _CountingEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        state = loop.start("2+3=?")

        first = loop.capture(reason="after-start").progress["decision_engine"]
        engine.calls += 1                      # 引擎自己往前走了一步
        second = loop.capture(reason="after-decide").progress["decision_engine"]

        self.assertNotEqual(first, second)

    def test_a_captured_value_is_json_able(self) -> None:
        """自述的值必须能 JSON 化（它要落库）。

        两个层次，分开断言，因为它们是**两件事**：

            能带走      `_progress_of` 探测时校验 JSON 可序列化 → 不可序列化就拒绝
            能还原      `json.dumps(..., default=str)` 会把不认识的对象变成字符串

        M86 的判据是**前者**（能不能带走），不是后者 ——
        一个 `default=str` 救回来的字符串会让"同进度 ⟹ 同值"不再成立，
        但那属于实现者的责任：他的 `progress()` 该返回可比较的东西。
        Runtime 只保证"带得走"，带走的形状由实现者负责。
        """

        class _Opaque:
            def progress(self) -> object:
                return object()          # 不可 JSON 化（会被 default=str 救成字符串）

        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        loop.decision_engine = _Opaque()
        loop.start("2+3=?")

        captured = loop.capture(reason="test")

        # `default=str` 兜住了它 → 没有抛，值变成了一个字符串。
        # 这条断言锁住的是"它确实被带走了"，而不是"它被原样带走了"。
        self.assertIn("decision_engine", captured.progress)
        self.assertIsNotNone(captured.progress["decision_engine"])

    def test_a_value_that_cannot_be_serialized_at_all_is_refused(self) -> None:
        """真的序列化不了的（连 `default=str` 都救不回）→ 当场拒绝。

        与上一条的区别：上一个返回一个普通对象（str() 一下就行），
        这一个返回一个**自引用**的结构 —— `default=str` 对它也无能为力。
        """

        class _Loopback:
            def __init__(self) -> None:
                self.me = self

            def __str__(self) -> str:           # 让 default=str 也炸
                raise ValueError("cannot describe myself")

        class _Broken:
            def progress(self) -> object:
                return _Loopback()

        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        loop.decision_engine = _Broken()
        loop.start("2+3=?")

        with self.assertRaises(InvariantViolation) as ctx:
            loop.capture(reason="test")

        # 理由必须点名是哪个 Port —— 否则"快照造不出来"没有可查的线索
        self.assertIn("_Broken", str(ctx.exception))


class _ResumableEngine:
    """能接回去的有状态引擎：`progress()` + `resume()` 成对。

    这是**常态**形状 —— `DemoDecisionEngine` 就是这样。
    "挂起 → 进程重启 → 恢复"要能走通，靠的就是这一对。
    """

    def __init__(self, calls: int = 0, approval_at_step: int = 0) -> None:
        self.calls = calls
        self.approval_at_step = approval_at_step

    def progress(self) -> object:
        return {"calls": self.calls, "approval_at_step": self.approval_at_step}

    def resume(self, progress: object) -> None:
        assert isinstance(progress, dict)
        self.calls = int(progress["calls"])
        self.approval_at_step = int(progress.get("approval_at_step", 0))

    def decide(self, state: State) -> Decision:
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            rationale="resumable",
        )


class AResumableEngineIsResumedNotRefusedTest(_Base):
    """R-7 的正解：**先接上，接不上才拒绝**。

    这一组是本轮最要紧的：只做"拒绝"会把
    "挂起 → 进程重启 → 恢复"这条**最常规**的路径弄成不可用 ——
    那是用一个正确的判据把一个正常的系统弄坏。
    """

    def test_a_fresh_engine_is_resumed_onto_the_snapshot_progress(self) -> None:
        """组合根重装配出的新引擎（calls=0）→ 接上 calls=2，恢复照常。"""
        engine = _ResumableEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 2, "approval_at_step": 0}}
        )

        loop.restore(snapshot)          # 不该抛 —— 这是常态路径

        # 关键断言：它**真的接上了**，不是"被放行但仍是 0"。
        # 少了这一条，一个"resume 什么都不做"的实现也会让测试绿。
        self.assertEqual(engine.calls, 2)

    def test_the_resumed_engine_does_not_redo_its_first_step(self) -> None:
        """★ 这一条才是这个洞的本体。

        恢复前：引擎走过 1 步（已调过模型，正在做第 2 次决定）。
        恢复后：它必须接着做第 2 次（FINISH），**不是**从第 1 次重来（再调一次模型）。
        """
        engine = _ResumableEngine(calls=1)      # 已经走了一步
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 1, "approval_at_step": 0}}
        )

        loop.restore(snapshot)

        # 接着走一步 —— 应该是"第 2 次"，即 FINISH。
        # 若 resume 没生效（calls 仍是 0），这一步会是 LLM_CALL（又调一次模型）。
        decision = engine.decide(loop.state)
        self.assertEqual(
            decision.selected_action.action_type,
            ActionType.FINISH,
            "a resumed engine redid its first step — it would call the model twice",
        )

    def test_resume_overwrites_a_stale_progress_honestly(self) -> None:
        """接上之后自述的值必须变成快照那个 —— 否则下一次捕获又是错的。"""
        engine = _ResumableEngine(calls=9)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 3, "approval_at_step": 0}}
        )

        loop.restore(snapshot)

        self.assertEqual(loop.capture(reason="after-restore").progress["decision_engine"],
                         {"calls": 3, "approval_at_step": 0})

    def test_a_self_described_but_unresumable_engine_is_still_refused(self) -> None:
        """能自述、接不回去 ⟹ 仍然拒绝。

        否则"实现一个假的 resume（什么都不做）"就成了绕过 R-7 的后门。
        """

        class _Unresumable(_CountingEngine):
            def resume(self, progress: object) -> None:
                raise RuntimeError("I cannot reconstruct that progress")

        engine = _Unresumable(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 5}}
        )

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        self.assertIn("decision_engine", str(ctx.exception))

    def test_a_resume_that_silently_gives_up_is_still_refused(self) -> None:
        """★ `resume()` 存在但没真的接上 ⟹ 还是要拒绝。

        "实现一个空方法"是这条不变量最容易的后门。所以拒绝条件不是
        "有没有 resume"，而是**接完再自述一次，看它对不对得上**。
        """

        class _NoOpResume(_CountingEngine):
            def resume(self, progress: object) -> None:
                pass        # ← 什么都没做，但不抛

        engine = _NoOpResume(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        snapshot = self._snapshot_for(
            progress={"planner": None, "decision_engine": {"calls": 5}}
        )

        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        self.assertIn("decision_engine", str(ctx.exception))


class TheProductionPathStillWorksTest(_Base):
    """把这条路径显式钉住：它是 M86 改动的**动机**，不是副作用。"""

    def test_suspend_then_restart_onto_a_fresh_stack_succeeds(self) -> None:
        """挂起一条 Run，然后**换一个全新的引擎**恢复 —— 必须成功。

        这条对应 `test_http_advance_snapshot` 那条真跑 Demo 栈的用例：
        一条 Run 挂起等审批（可能几小时），期间 Pod 重启，
        恢复时组合根给的是全新的 `DemoDecisionEngine(calls=0)`。
        """
        engine = _ResumableEngine(calls=0)
        loop = self._loop(planner=_StatelessPlanner(), engine=engine)
        run_id = new_run()
        snapshot = RunSnapshot(
            run_id=run_id,
            agent_id="agent-t",
            status="SUSPENDED",
            state=state_to_dict(_budgeted_state(run_id)),
            progress={"planner": None, "decision_engine": {"calls": 2, "approval_at_step": 2}},
        )

        try:
            loop.restore(snapshot)
        except InvariantViolation as exc:
            # 这里只关心 R-7 有没有挡住它；别的 R-* 是这条用例没配齐
            # （比如挂起却没给审批 id），与进度这一环无关。
            self.assertNotIn("R-7", str(exc), f"R-7 blocked a normal restart: {exc}")

        self.assertEqual(engine.calls, 2)


class ASilentAboutItsProgressPortIsRefusedTest(_Base):
    """★ M5 变异逼出来的：**"没有 progress" 与 "progress 返回 None" 是两件事**。

    混为一谈的后果是一个后门：

        一个有状态的引擎只要 `progress()` 返回 None，
        就把自己伪装成无状态的 → 快照里存 None →
        恢复时"两边都是 None" → **静默放行**，
        而它实际上会从第 1 步重来。

    一个**故意撒谎**的 Port 防不住（`progress()` 是它唯一的自述渠道），
    但"实现了这个方法却说不出值"是**疏漏**，可测。
    """

    def test_a_port_that_implements_progress_but_returns_none_is_refused(self) -> None:
        class _Silent:
            def progress(self) -> object:
                return None

            def decide(self, state: State) -> Decision:
                return Decision(
                    run_id=state.run_id,
                    selected_action=Action(
                        run_id=state.run_id, action_type=ActionType.FINISH
                    ),
                    rationale="silent",
                )

        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        loop.decision_engine = _Silent()
        loop.start("2+3=?")

        with self.assertRaises(InvariantViolation) as ctx:
            loop.capture(reason="test")

        self.assertIn("_Silent", str(ctx.exception))

    def test_a_port_without_the_method_at_all_is_fine(self) -> None:
        """控制组：真的没实现 `progress` → 无状态，照常。

        少了这一条，上面那条可能只是"凡是没有可比较的进度就炸"，
        而那是另一个意思（它会把所有无状态实现都打死）。
        """
        loop = self._loop(planner=_StatelessPlanner(), engine=_StatelessEngine())
        loop.start("2+3=?")

        captured = loop.capture(reason="test")

        self.assertEqual(
            captured.progress, {"planner": None, "decision_engine": None}
        )


class TheSnapshotDeclaresItIsWholeTest(_Base):
    """R-1 那条既有断言的先例：装下去的必须真的是全部。"""

    def test_progress_defaults_to_empty_and_is_frozen(self) -> None:
        snapshot = RunSnapshot(run_id="run_x", agent_id="a", status="RUNNING",
                               state={"run_id": "run_x"})
        self.assertEqual(dict(snapshot.progress), {})

        with self.assertRaises(Exception):
            snapshot.progress = {"planner": 1}      # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
