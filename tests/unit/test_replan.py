"""M76 · REPLAN 是一条真的边。

--------------------------------------------------------------------------
洞的形状：机制齐全，但从没人走过

`ActionType.REPLAN` 这一整套都写好了：

    packages/agent_domain/intelligence/action.py    REPLAN = "replan"
    packages/agent_runtime/reducer.py               PLAN_INVALIDATED → current_plan = None
    packages/agent_runtime/loop.py                  REPLAN → 失效 → StepOutcome.REPLANNED
    packages/agent_runtime/task_factory.py          REPLAN: None（不产生 Task）

但**没有任何 DecisionEngine 产出它，也没有任何测试走过它**。
全仓唯一提到 REPLAN 的用例在 `test_child_run.py` 里，
而且只是把它当作"普通活不是派生"的一个控制组枚举值。

--------------------------------------------------------------------------
为什么它值得单独一轮

`packages/agent_domain/business/derive.py` 里写着：

    ·  ·  Agent 下一步可能还要 REPLAN（Step 全绿但目标没达成）

这句话是 **B-7**（Run 的终态只能由 Runtime 声明，不能从 Step 派生）的
一条论据 —— 也就是说，**一条不变量的论据，依赖一个从未被触发的机制**。

这不是"少了个功能"，是"文档声称的机制其实没接线"。
而它不报错：整条链路编译得过、测试全绿、没人问过它跑没跑过。

--------------------------------------------------------------------------
补的时候抓到的真 bug

REPLAN 分支**不消耗预算**：`self.steps += 1` 只在执行完成那条路径上，
于是 `steps >= budget` 永远不成立 ——
一个一直返回 REPLAN 的引擎会让 Run **永远跑下去**，既不终止也不报错。

这与 L-7 是同一族病（"一个永远被拒的 Run 会永远空转，且没有任何报错"）：
**不消耗预算的分支，就是一条可以无限走的分支。**
而 REPLAN 恰恰是唯一一个"既不产生 Task、也不产生终态"的出口 ——
它是整个循环里最容易变成空转的那一条。

新增不变量 **I-10：重规划必须吃预算。**
"""
from __future__ import annotations

import unittest
from typing import Any, Mapping

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


class VaryingPlanner:
    """每一轮都给出**形状不同**的计划。

    I-10 的用例**必须**用它：如果用 `ScriptedPlanner`（每次都给同一份计划），
    那么 I-12（重规划必须真的换一份计划）会先把 Run 判死 ——
    于是那两条用例测到的其实是 I-12，不是 I-10。

    测试也要问一句"它到底在测哪条不变量"。
    """

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id=f"n{self.calls}", name=f"attempt-{self.calls}"),
            ),
        )


class AlwaysReplan:
    """一个**永远不满意**的引擎：每一步都要重规划。

    它不代表任何智能 —— 它代表"目标永远达不成"这种最坏情况。
    I-10 要守的就是它。
    """

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, state: State) -> Decision:
        self.calls += 1
        return Decision(
            run_id=state.run_id,
            selected_action=Action(
                run_id=state.run_id, action_type=ActionType.REPLAN
            ),
            rationale="goal not satisfied yet",
        )


class TestTheReplanEdgeIsWired(LoopTestBase):
    """这条边的两端：失效 Observation → 清空计划 → 下一轮重新规划。"""

    def _replan_once(self) -> tuple[AgentLoop, State]:
        loop = self._loop([])
        state = loop.start("hi")
        replan = Action(run_id=state.run_id, action_type=ActionType.REPLAN)
        loop.decision_engine = ScriptedDecisionEngine([replan])
        return loop, state

    def test_the_outcome_is_replanned(self):
        loop, _ = self._replan_once()
        self.assertEqual(loop.step(), StepOutcome.REPLANNED)

    def test_the_plan_is_invalidated_not_left_in_place(self):
        """`current_plan` 必须回到 None —— 否则下一轮不会重新规划。"""
        loop, state = self._replan_once()
        loop.step()
        self.assertIsNone(state.current_plan)

    def test_it_goes_through_an_observation(self):
        """I-3：Loop 不许直接 `state.current_plan = None`。"""
        loop, state = self._replan_once()
        loop.step()
        kinds = [o.kind for o in state.observations]
        self.assertIn("plan.invalidated", kinds)

    def test_the_loop_really_plans_again(self):
        """要害：失效之后**真的**重新规划了，不是只记了个状态。"""
        loop, _ = self._replan_once()
        loop.step()
        self.assertEqual(self.planner.calls, 1, "the first step plans once")
        loop.step()
        self.assertEqual(
            self.planner.calls,
            2,
            "a replanned loop must plan again — otherwise 'replanning' "
            "is only a flag nobody acts on",
        )


class TestReplanningIsBounded(LoopTestBase):
    """I-10：重规划**必须吃预算** —— 这是本轮抓到的那个 bug。"""

    def test_an_engine_that_never_converges_still_terminates(self):
        """一直 REPLAN 的引擎：Run 必须走到**终态**，不是永远跑下去。

        这条用例在 I-10 之前会**挂住**（无限循环直到测试超时），
        因为它既不产生终态也不消耗预算。
        """
        engine = AlwaysReplan()
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=3),
            # ⚠️ 必须是 **VaryingPlanner**：用 ScriptedPlanner 的话 I-12 会
            # 先把 Run 判死，这两条用例就测不到"重规划吃预算"了。
            planner=VaryingPlanner(),
            decision_engine=engine,
            # ⚠️ 预算取的是 `state.goal.budget.max_steps or config.max_steps`：
            # 前者优先。只给 config 是不够的 —— 解释器默认的 6 会盖掉它。
            config=AgentLoopConfig(max_steps=3),
        )
        state = loop.start("hi")

        seen: list[StepOutcome] = []
        # 上限刻意远大于预算：这条用例要证明的是"它会停"，
        # 而不是"它恰好在第 N 步停"。
        for _ in range(50):
            seen.append(loop.step())
            # ⚠️ 终态看 `agent_run`，**不是** `state.runtime_status`：
            # 后者只在 FINISH 那条路径上被 Reducer 改成 FINISHED（reducer.py:81），
            # 预算耗尽走的是 `_declare_terminal`，不经过 Observation ——
            # 于是 State 仍写着 RUNNING，而 Run 其实已经 FAILED。
            # （这个不一致另登记为**空洞 238**，不在本轮治。）
            if loop.agent_run.is_terminal:
                break

        self.assertIn(
            StepOutcome.BUDGET_EXHAUSTED,
            seen,
            "an engine that always replans must still hit the budget — "
            "otherwise the run spins forever without a terminal state",
        )
        self.assertTrue(loop.agent_run.is_terminal)

    def test_it_does_not_take_more_calls_than_the_budget_allows(self):
        """而且不能超额：预算是 3，就不该走到第 10 轮还活着。"""
        engine = AlwaysReplan()
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=3),
            # ⚠️ 必须是 **VaryingPlanner**：用 ScriptedPlanner 的话 I-12 会
            # 先把 Run 判死，这两条用例就测不到"重规划吃预算"了。
            planner=VaryingPlanner(),
            decision_engine=engine,
            # ⚠️ 预算取的是 `state.goal.budget.max_steps or config.max_steps`：
            # 前者优先。只给 config 是不够的 —— 解释器默认的 6 会盖掉它。
            config=AgentLoopConfig(max_steps=3),
        )
        state = loop.start("hi")

        calls = 0
        while not loop.agent_run.is_terminal and calls < 50:
            loop.step()
            calls += 1

        self.assertLessEqual(calls, 4, f"budget is 3 but it ran {calls} rounds")
        # 预算耗尽那一轮在 `decide()` **之前**就返回了（loop.py:748 的判据
        # 在 `_advance` 开头），所以引擎被问的次数 = 预算，不是轮数。
        self.assertEqual(
            engine.calls,
            3,
            "once the budget is gone, the loop must stop asking the engine",
        )


class TestTheBudgetIsNotJustAlwaysFiring(LoopTestBase):
    """控制组：上一条不是因为"预算永远触发"才通过的。"""

    def test_a_normal_run_still_finishes_with_the_same_budget(self):
        loop = self._loop([], max_steps=3)
        state = loop.start("2+3=?")
        self.assertEqual(loop.step(), StepOutcome.FINISHED)
        self.assertEqual(state.runtime_status, "FINISHED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
