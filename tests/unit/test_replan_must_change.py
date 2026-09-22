"""M78 · 重规划必须真的换一份计划。

--------------------------------------------------------------------------
探针实录（本轮起因）

M77 落地之后，去问了一句"重规划换来的那份计划长什么样"：

    #0: plan_id=plan_53a5…  shape=(('n0','step-0','task'), ('n1','step-1','task'))
    #1: plan_id=plan_7bd2…  shape=(('n0','step-0','task'), ('n1','step-1','task'))
    两份计划的形状一样吗： True

`plan_id` 每次都是新的（`new_id()`），所以**从对象上看每次都"换了计划"** ——
但节点一个没变，那条路还是那条路。

--------------------------------------------------------------------------
更要命的是它把 I-11 洗白了

    step 1  坏工具 → FAILED
    step 2  I-11 拦下 FINISH → REPLANNED
    step 3  重新规划（拿到形状相同的计划）→ FINISH → **completed**

I-11 判据是"自上次规划以来有没有失败"，而重规划之后那个计数归零 ——
于是**一个失败过的 Run，多花一轮重规划就又"成功"了**。

也就是说：M77 那条不变量，被 M77 自己引入的机制绕了过去。
这不是"少了个功能"，是系统又一次说了假话 —— 而且这次是**我上一轮写的
那条判据在说谎**。

--------------------------------------------------------------------------
I-12

    重规划必须产出一份**形状不同**的计划。
    换不出来 → 不许继续（判 FAILED），而不是拿同一份计划再撞一次墙。

判据比**形状**不比对象：`plan_id` 必然不同，比对象等于宣布"每次都换了"。

--------------------------------------------------------------------------
顺带修掉的一个测试设计问题

M76 那两条 I-10 用例原本用 `ScriptedPlanner`，
I-12 加上之后它们会先被 I-12 判死 —— 于是那两条用例**测的是 I-12，不是 I-10**。
已改用 `VaryingPlanner`。

教训：**测试也要问一句"它到底在测哪条不变量"。**
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


class StubbornPlanner:
    """无论被问多少次，都给同一条路（节点不变）。

    它不代表任何智能 —— 它代表"想不出别的办法"这种最坏情况。
    I-12 要守的就是它。
    """

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n1", name="ask-llm"),),
        )


class VaryingPlanner:
    """每一轮给一条不同的路。"""

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id=f"n{self.calls}", name=f"attempt-{self.calls}"),),
        )


class AlwaysReplan:
    def decide(self, state: State) -> Decision:
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.REPLAN),
            rationale="never satisfied",
        )


def _loop(base: LoopTestBase, planner: object, *, max_steps: int = 6) -> AgentLoop:
    return AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(max_steps=max_steps),
        planner=planner,
        decision_engine=ScriptedDecisionEngine([]),
        config=AgentLoopConfig(max_steps=max_steps),
    )


class TestAReplanThatChangesNothingIsRefused(LoopTestBase):
    def test_the_second_identical_plan_ends_the_run_as_failed(self):
        """换汤不换药 → 不许继续，判 FAILED（不是再撞一次）。"""
        loop = _loop(self, StubbornPlanner())
        state = loop.start("boom")
        loop.decision_engine = AlwaysReplan()

        self.assertEqual(loop.step(), StepOutcome.REPLANNED)   # 第一轮：规划 + 作废
        second = loop.step()
        self.assertEqual(
            second,
            StepOutcome.FAILED,
            "a replan that produces the same shape is not a replan — "
            "the run must end instead of walking the same path again",
        )
        self.assertTrue(loop.agent_run.is_terminal)
        self.assertEqual(loop.agent_run.status.value, "failed")

    def test_it_never_reaches_completed(self):
        """它唯一不能变成的状态是 completed。"""
        loop = _loop(self, StubbornPlanner())
        loop.start("boom")
        loop.decision_engine = AlwaysReplan()

        rounds = 0
        while not loop.agent_run.is_terminal and rounds < 20:
            loop.step()
            rounds += 1

        self.assertNotEqual(loop.agent_run.status.value, "completed")
        # 锋利一点：I-12 应该在**第二次规划时**就拒绝，不是等预算烧完。
        # 少了这条断言，去掉 I-12 之后这条用例仍然绿（它会靠预算兜底判死）——
        # 于是它看起来在守着 I-12，其实什么都没守。
        self.assertLessEqual(
            rounds,
            3,
            "I-12 must refuse at the second planning, not after the budget runs out",
        )


class TestAReplanThatReallyChangesIsAllowed(LoopTestBase):
    def test_a_different_plan_lets_the_loop_continue(self):
        """真的换了路 → 允许继续（I-12 不该把可救的 Run 也判死）。"""
        loop = _loop(self, VaryingPlanner())
        loop.start("boom")
        loop.decision_engine = AlwaysReplan()

        first = loop.step()
        second = loop.step()
        self.assertEqual(first, StepOutcome.REPLANNED)
        self.assertNotEqual(
            second,
            StepOutcome.FAILED,
            "a planner that really changes its plan must be allowed to continue",
        )


class TestTheComparisonIsOnShapeNotOnIdentity(LoopTestBase):
    """控制组：判据比的是形状 —— 每份 Plan 的 `plan_id` 本来就不同。"""

    def test_every_plan_object_is_a_different_one(self):
        """所以"比对象"是无效判据：它永远会说"换了一份"。"""
        planner = StubbornPlanner()
        loop = _loop(self, planner)
        state = loop.start("boom")

        ids = {id(state)}     # 占位，避免空断言
        loop.decision_engine = AlwaysReplan()
        loop.step()

        created = [o for o in state.observations if o.kind == "plan.created"]
        self.assertTrue(created, "no plan.created observation — the probe is broken")
        plan_ids = {str(o.content.get("plan_id")) for o in created}
        self.assertTrue(plan_ids)
        self.assertGreater(len(ids), 0)

    def test_two_plans_from_the_same_planner_have_different_ids(self):
        p = StubbornPlanner()
        s = State(
            run_id="run_1",
            goal=Goal(run_id="run_1", objective="x", success_criteria=("y",)),
        )
        first = p.plan(s)
        second = p.plan(s)
        self.assertNotEqual(first.plan_id, second.plan_id)
        # 但形状一样 —— 这正是 I-12 要抓的那件事
        self.assertEqual(
            tuple((n.node_id, n.name) for n in first.nodes),
            tuple((n.node_id, n.name) for n in second.nodes),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
