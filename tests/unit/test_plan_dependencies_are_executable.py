"""M87 / I-16：Plan 的依赖图必须是**可执行的约束**，不是注释。

--------------------------------------------------------------------------
洞的形状（probe87.py 实证，不是推演）

`PlanNode.depends_on` 被领域层用一次**完整拓扑排序**守着
（`Plan.__post_init__` → 拒绝自依赖 / 拒绝未知依赖 / `assert_acyclic()`），
也被序列化进快照（`snapshot.py`）。而 `plan.py` 的 docstring 写着：

    "Planner 的 Plan Validator 会检查 DAG 环；这里做领域级兜底。"

—— 全仓**没有 Plan Validator**（grep 只命中 `Execution.validate()`）。
更要紧的是：那段时间里 `depends_on` **根本没被执行过**，
因为运行时按下标取节点：

    node = plan.nodes[len(self.steps_of_run)]

两张脸，同一个根因 —— **计划是按位置消费的，不是按意义消费的**：

  ① 计划说 `n2 depends_on n1`，而 n2 排在 n1 前面
     → 运行时**先跑了 n2**。依赖图只被校验过，从没被执行过。

  ② `len(self.steps_of_run)` 是"这条 Run 跑了多少步"，
     **不是**"这份计划消费到第几个节点"。重规划换出一份新计划之后，
     它从 `plan.nodes[已跑步数]` 开始取 —— 新计划的前 N 个节点
     **被静默跳过**，然后计划就用完了。

    探针实录（plan A 两节点 → 重规划 → plan B 三节点）：

        实际实例化顺序 : ['a0', 'a1', 'b2', 'ad-hoc-3']

    b0 与 b1 从来没被执行过。**系统在"换一条路"，换来的路的头两步被跳过了。**

--------------------------------------------------------------------------
⚠️ 补这条不变量时，1319 条既有测试**一条都不红**。

那说明它此前一直是**碰巧成立**的，不是被机制守住的：
既有测试的计划要么没有依赖（`ScriptedPlanner` 给 `n0, n1, …`），
要么依赖恰好与顺序一致（`test_agent_loop_full` 给 `n1, n2(dep n1)`）。
于是"按下标取"与"按依赖取"给出同一个答案 —— 差别被巧合抹平了。

与 M85 同款：**一条没人能违反的判据，和没有判据，对系统来说是一样的。**

--------------------------------------------------------------------------
本文件要守住什么

    I-16：节点进 Step 的依据是"**它准备好了**"，不是"**下标轮到它了**"。

五条一起才成立（缺任何一条，这条不变量都能被绕过）：

    1. 排好序的计划行为**不变** —— 控制组。少了它，第 2 条可能
       只是"所有计划都坏了"。
    2. 顺序颠倒的计划 → **按依赖**跑，不按位置跑。
    3. 游标属于**这份计划**：重规划之后新计划从**头**开始。
    4. 计划卡住（还有节点没做，但一个就绪的都没有）→ **不编造 ad-hoc 步**，
       走 REPLAN。
    5. "计划用完了"仍然允许 ad-hoc 步 —— 与第 4 条必须分得开。
       少了它，第 4 条可能只是"ad-hoc 步整个没了"，那会弄坏正常路径。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop, StepOutcome

# 这两个不是 TestCase 子类，放模块级不会污染收集。
# `MinimalLoopTest` 是 —— 它只在 `_Base._new()` 里 import（见那里的注释）。
from tests.unit.test_agent_loop import ScriptedDecisionEngine, ScriptedInterpreter


# ---------------------------------------------------------------- 测试替身
class LinearPlanner:
    """没有依赖的两个节点（既有测试的形状）。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="n1", name="one"),
            ),
        )


class OrderedDependentPlanner:
    """依赖与顺序**一致** —— 既有测试恰好都是这个形状。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n1", name="first"),
                PlanNode(node_id="n2", name="second", depends_on=("n1",)),
            ),
        )


class InvertedPlanner:
    """合法，但**顺序颠倒**：n2 声明依赖 n1，却排在 n1 前面。

    这份计划无环、无重复、依赖都存在 —— `Plan` 照收。
    它表达的意思是"n2 必须等 n1 先做完"。
    """

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n2", name="second", depends_on=("n1",)),
                PlanNode(node_id="n1", name="first"),
            ),
        )


class TwoPhasePlanner:
    """第一次给 plan A（2 节点）；之后给**形状不同**的 plan B（3 节点）。

    形状必须不同，否则 I-12 会判定"这不是重规划"。
    """

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            return Plan(
                run_id=state.run_id,
                nodes=(
                    PlanNode(node_id="a0", name="alpha-0"),
                    PlanNode(node_id="a1", name="alpha-1"),
                ),
            )
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="b0", name="beta-0"),
                PlanNode(node_id="b1", name="beta-1"),
                PlanNode(node_id="b2", name="beta-2"),
            ),
        )


class DependentOnAFailingNodePlanner:
    """m1 依赖 m0；m0 那一步会失败。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="m0", name="will-fail"),
                PlanNode(node_id="m1", name="needs-m0", depends_on=("m0",)),
            ),
        )


class SingleNodePlanner:
    """只有一个节点 —— 用来验"计划用完"那条路。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="only"),))


def _llm(run_id: str, n: int) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.LLM_CALL,
        payload={"prompt": f"prompt-{n}"},
    )


def _failing_tool(run_id: str) -> Action:
    """一个工具不存在的动作 —— 执行结果是 FAILED（既有测试同款）。"""
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "does_not_exist"},
    )


class _Base(unittest.TestCase):
    def _new(self, planner: object) -> tuple[AgentLoop, object]:
        # 刻意**在函数里** import：模块级 import 会让 unittest 把
        # `MinimalLoopTest` 也当成这个文件里的用例收集一遍 ——
        # 于是全套会多跑 4 条（同一个类、两个模块名），计数被凭空抬高。
        from tests.unit.test_agent_loop import MinimalLoopTest

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        return loop, loop.start("2+3=?")

    def _step_ids(self, loop: AgentLoop) -> list[str]:
        return [s.plan_node_id for s in loop.steps_of_run]


# ============================================================ 1. 控制组
class AnOrderedPlanIsUnaffectedTest(_Base):
    """控制组：排好序的计划行为**不变**。

    少了这一组，下面那些"按依赖跑"的用例可能只是"所有计划都坏了" ——
    它们会全绿，而它们什么都没守住。
    """

    def test_a_plan_without_dependencies_still_runs_in_order(self) -> None:
        loop, state = self._new(LinearPlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()
        loop.step()

        self.assertEqual(self._step_ids(loop), ["n0", "n1"])

    def test_a_dependency_that_matches_the_order_still_works(self) -> None:
        """依赖与顺序一致（既有测试的形状）—— 顺序照旧。"""
        loop, state = self._new(OrderedDependentPlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()
        loop.step()

        self.assertEqual(self._step_ids(loop), ["n1", "n2"])


# ============================================================ 2. 依赖可执行
class TheDependencyGraphIsExecutableTest(_Base):
    """核心：顺序颠倒的计划，按**依赖**跑，不按**位置**跑。"""

    def test_a_dependent_node_is_not_instantiated_before_its_dependency(self) -> None:
        """probe87 场景 1 的回归守卫。

        修复前：`['n2', 'n1']` —— n2 在自己的依赖 n1 之前被执行。
        """
        loop, state = self._new(InvertedPlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()

        self.assertEqual(
            self._step_ids(loop),
            ["n1"],
            "第一个 Step 必须是被依赖的那个节点（n1），不是排在前面的 n2",
        )

    def test_the_dependent_node_runs_after_its_dependency_completes(self) -> None:
        """而且 n2 不是被跳过 —— 它只是**排在后面**。"""
        loop, state = self._new(InvertedPlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()
        loop.step()

        self.assertEqual(self._step_ids(loop), ["n1", "n2"])
        # 名字也要对 —— 只比 node_id 的话，一次 id 交换也能骗过断言
        self.assertEqual(
            [s.name for s in loop.steps_of_run], ["first", "second"]
        )


# ============================================================ 3. 游标属于计划
class TheCursorBelongsToThePlanNotTheRunTest(_Base):
    """核心：游标属于**这份计划**，不属于这条 Run。"""

    def test_a_replan_starts_the_new_plan_from_its_first_node(self) -> None:
        """probe87 场景 2 的回归守卫。

        修复前：`['a0', 'a1', 'b2', 'ad-hoc-3']` —— 新计划从 b2 开始，
        b0 与 b1 从来没被执行过。
        """
        loop, state = self._new(TwoPhasePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [
                _llm(state.run_id, 1),
                _llm(state.run_id, 2),
                Action(run_id=state.run_id, action_type=ActionType.REPLAN),
                _llm(state.run_id, 3),
                _llm(state.run_id, 4),
            ]
        )

        for _ in range(5):
            loop.step()

        self.assertEqual(self._step_ids(loop), ["a0", "a1", "b0", "b1"])

    def test_the_new_plans_head_is_not_skipped(self) -> None:
        """把"没被跳过"单独钉一条 —— 上面那条是整条序列，坏了不好定位。"""
        loop, state = self._new(TwoPhasePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [
                _llm(state.run_id, 1),
                _llm(state.run_id, 2),
                Action(run_id=state.run_id, action_type=ActionType.REPLAN),
                _llm(state.run_id, 3),
            ]
        )

        for _ in range(4):
            loop.step()

        ids = self._step_ids(loop)
        self.assertIn("b0", ids, "重规划换出的新计划必须从它自己的第一个节点开始")
        self.assertNotIn("b2", ids, "b2 排在 b0/b1 之后，这时候还不该轮到它")


# ============================================================ 4. 卡住不编造
class AStuckPlanIsNotPaperedOverTest(_Base):
    """核心：计划卡住了 → **不编造**，走 REPLAN。

    判据是"依赖**完成**了"，不是"依赖**开始过**"。
    """

    def test_a_failed_dependency_does_not_satisfy_its_dependent(self) -> None:
        loop, state = self._new(DependentOnAFailingNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_failing_tool(state.run_id), _llm(state.run_id, 2)]
        )

        self.assertEqual(loop.step(), StepOutcome.FAILED)      # m0 那一步失败
        self.assertEqual(self._step_ids(loop), ["m0"])

        outcome = loop.step()

        self.assertEqual(
            outcome,
            StepOutcome.REPLANNED,
            "依赖失败之后，m1 不该被实例化 —— 这条计划走不通了",
        )
        self.assertEqual(self._step_ids(loop), ["m0"])

    def test_no_ad_hoc_step_is_invented_when_the_plan_is_stuck(self) -> None:
        """★ 这一条是"卡住"与"用完"的分界线。

        混起来的话，卡住的计划会静默退化成 ad-hoc 步：
        Runtime 自己编一个计划没批准过的步，照常往下走，
        而账本上读不出任何区别（M82 的"把存在当成被处理"同族）。
        """
        loop, state = self._new(DependentOnAFailingNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_failing_tool(state.run_id), _llm(state.run_id, 2)]
        )

        loop.step()
        loop.step()

        ids = self._step_ids(loop)
        self.assertFalse(
            [i for i in ids if i.startswith("ad-hoc-")],
            f"计划卡住时不许编造 ad-hoc 步，实际拿到 {ids}",
        )


# ============================================================ 5. 用完仍可 ad-hoc
class AnExhaustedPlanStillAllowsAdHocStepsTest(_Base):
    """控制组：**计划用完**仍然允许 ad-hoc 步。

    少了这一组，第 4 组可能只是"ad-hoc 步整个没了" ——
    那会用一个正确的判据弄坏一条正常的路径（动态决策图里 ad-hoc 是常态）。
    """

    def test_a_finished_plan_falls_back_to_ad_hoc_steps(self) -> None:
        loop, state = self._new(SingleNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()
        loop.step()

        self.assertEqual(self._step_ids(loop), ["n0", "ad-hoc-1"])

    def test_an_exhausted_plan_does_not_trigger_a_replan(self) -> None:
        """用完 ≠ 卡住：不该被判成"这条路走不通"。"""
        loop, state = self._new(SingleNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        loop.step()
        outcome = loop.step()

        self.assertNotEqual(
            outcome,
            StepOutcome.REPLANNED,
            "计划做完了是常态，不是'卡住'",
        )


if __name__ == "__main__":
    unittest.main()
