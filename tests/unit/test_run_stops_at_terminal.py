"""M87 / I-17：`run()` 的停止条件是「**这条 Run** 到了终态」，
不是「这一步的结果属于某张枚举表」。

--------------------------------------------------------------------------
洞的形状（probe87.py 场景 3 实测，**不需要任何变异**）

`run()` 原本的停止条件是一份 `StepOutcome` 的白名单：

    FINISHED / BUDGET_EXHAUSTED / WAITING_APPROVAL / WAITING_CHILD
    / DENY_LOOP / CANCELLED

**没有 FAILED。** 那是有道理的：作为"这一步的结果"，FAILED 通常不是终点 ——
失败是事实，Agent 要据此换计划（I-11 就是这么设计的）。

但"这一步怎么了"与"这条 Run 还在不在"是**两个事实**。白名单只管住了前者，
于是两者不重合的地方就开始空转：

    重规划换不出形状不同的计划 -> `_plan()` 返回 False
    -> `_declare_terminal(FAILED)` -> `step()` 从此每次都返回 FAILED
    -> `steps` 不再增长（FAILED 那条路不吃预算）
    -> `steps >= budget` 永远不成立 -> **`run()` 永远转下去**

没有报错，也没有尽头。而 L-7 自己写下的正是这句话：

    "一个永远被拒的 Run 会永远空转，且没有任何报错。"

L-7 把 `DENY_LOOP` 加进了那张表 —— 它看见了症状，却没看见**判据选错了对象**。

--------------------------------------------------------------------------
本文件要守住什么

    I-17：`run()` 在"这条 Run 已终态"时**必须**返回，无论这一步的结果叫什么。

三条一起才成立（缺任何一条，这条不变量都能被绕过）：

    1. 正常的 Run 照常返回（控制组）。
    2. 被判死的 Run，`run()` 必须停 —— 而且**不能**靠把 `FAILED` 塞进白名单
       来"修"：那样会把"某一步失败了"也当成终点，弄坏 I-11 的换计划路径。
    3. "某一步 FAILED"与"这条 Run FAILED"必须分得开（控制组）。
       少了它，第 2 条可能只是"FAILED 一律停"，而那是错的。

⚠️ 测"它会停下来"这件事，本身有个陷阱：**判据被破坏时，测试会挂住而不是失败**。
   挂住的测试比失败的测试坏得多 —— 它拖垮整套、还不告诉你是哪一条。
   所以这里用 `_drive_with_a_leash()`：把 `step()` 包一层，超过上限就抛
   `AssertionError`。**把"转不完"翻译成一条会红的断言。**
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop, StepOutcome

from tests.unit.test_agent_loop import ScriptedDecisionEngine, ScriptedInterpreter


class SameShapePlanner:
    """每次都返回**同一份形状**的计划 —— 重规划换不出新路（触发 I-12）。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="only"),))


class TwoNodePlanner:
    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="n1", name="one"),
            ),
        )


def _llm(run_id: str, n: int) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.LLM_CALL,
        payload={"prompt": f"prompt-{n}"},
    )


def _failing_tool(run_id: str) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "does_not_exist"},
    )


class _Base(unittest.TestCase):
    def _new(self, planner: object) -> tuple[AgentLoop, object]:
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

    def _drive_with_a_leash(self, loop: AgentLoop, *, max_steps: int = 50):
        """跑 `loop.run()`，但给 `step()` 上一根绳子。

        判据被破坏时 `run()` 会转不完 —— 那会让**测试挂住**，而不是变红。
        挂住比红更坏：它拖垮整套，还不告诉你是哪一条。
        所以这里把"转不完"翻译成一条会红的断言。
        """
        calls = {"n": 0}
        original = loop.step

        def leashed_step():
            calls["n"] += 1
            if calls["n"] > max_steps:
                raise AssertionError(
                    f"run() 转了 {max_steps} 步还没停 —— 它在一条已经终态的 Run 上空转"
                )
            return original()

        loop.step = leashed_step        # type: ignore[method-assign]
        loop.run()
        return calls["n"]


# ============================================================ 控制组
class ANormalRunStillReturnsTest(_Base):
    """控制组：正常的 Run 照常返回。

    少了这一组，下面那条"被判死的 Run 要停"可能只是"run() 整个坏掉了"。
    """

    def test_a_normal_run_reaches_completed_and_returns(self) -> None:
        loop, state = self._new(TwoNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(state.run_id, 1), _llm(state.run_id, 2)]
        )

        steps = self._drive_with_a_leash(loop)

        self.assertEqual(loop.agent_run.status, AgentRunStatus.COMPLETED)
        self.assertLess(steps, 50)

    def test_an_empty_script_finishes_immediately(self) -> None:
        loop, state = self._new(TwoNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine([])

        self._drive_with_a_leash(loop)

        self.assertEqual(loop.agent_run.status, AgentRunStatus.COMPLETED)


# ============================================================ 核心
class ARunThatIsAlreadyTerminalStopsTheDriverTest(_Base):
    """核心：Run 已终态 → `run()` 必须返回。"""

    def test_a_run_declared_failed_stops_the_driver(self) -> None:
        """probe87 场景 3 的回归守卫。

        破坏前：`run()` 在一条已经 FAILED 的 Run 上**永远转下去**
        （`step()` 一直返回 FAILED，`steps` 不再增长，预算耗尽那条路到不了）。
        """
        loop, state = self._new(SameShapePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [Action(run_id=state.run_id, action_type=ActionType.REPLAN)]
        )

        self._drive_with_a_leash(loop)

        self.assertEqual(
            loop.agent_run.status,
            AgentRunStatus.FAILED,
            "换不出形状不同的计划 -> Run 判 FAILED（I-12）",
        )

    def test_the_driver_stops_even_though_the_outcome_is_failed(self) -> None:
        """停止**不能**靠"把 FAILED 加进白名单"来"修"。

        这条钉的是"判据读的是什么"：读的是 Run 的终态，
        不是这一步的结果。判据读错对象，就会在两者不重合的地方空转。
        """
        loop, state = self._new(SameShapePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [Action(run_id=state.run_id, action_type=ActionType.REPLAN)]
        )
        loop.step()                      # 第 1 步：REPLAN，计划作废

        self.assertEqual(loop.step(), StepOutcome.FAILED)   # 第 2 步：判死
        self.assertTrue(loop.agent_run.is_terminal)

        # 再推几次，结果必须**稳定**在 FAILED（而不是继续变化或空转）
        self.assertEqual(loop.step(), StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)


# ============================================================ 控制组
class AFailedStepIsNotATerminalRunTest(_Base):
    """控制组：「某一步 FAILED」与「这条 Run FAILED」必须分得开。

    少了这一组，上面那条可能只是"FAILED 一律停" —— 而那是错的：
    失败是事实，Agent 要据此换计划（I-11）。把 FAILED 加进白名单，
    等于把"这一步没做成"读成"这条 Run 结束了"，那会弄坏换计划那条路。
    """

    def test_a_failed_step_leaves_the_run_running(self) -> None:
        loop, state = self._new(TwoNodePlanner())
        loop.decision_engine = ScriptedDecisionEngine(
            [_failing_tool(state.run_id)]
        )

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        self.assertFalse(
            loop.agent_run.is_terminal,
            "某一步失败 ≠ 这条 Run 结束 —— 它还要据此换计划（I-11）",
        )
        self.assertEqual(loop.agent_run.status, AgentRunStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
