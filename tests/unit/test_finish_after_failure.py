"""M77 · 带着没被处理的失败，不许宣布完成。

--------------------------------------------------------------------------
探针实录（本轮的起因）

    loop = …; state = loop.start("boom")
    engine = ScriptedDecisionEngine([bad_tool_action])   # 用完就 FINISH

    step 1 → StepOutcome.FAILED     runtime_status RUNNING   agent_run running
    step 2 → StepOutcome.FINISHED   runtime_status FINISHED  agent_run **completed**

而失败的证据就在 State 里：

    state.variables["result:exec_…"] =
        {'kind': 'execution_failed', 'summary': '… attempt#1 FAILED (failed)'}

**一个关键步骤失败了的 Run，被宣布 COMPLETED。**

--------------------------------------------------------------------------
为什么这是"系统说谎"，不只是"少了个功能"

引擎看不见失败（它按脚本走），而**宣布终态的是 Runtime**（B-7）。
于是"这条 Run 成功了"这句话是 Runtime 说的，
而 Runtime 手上明明握着 `execution_failed` 这条事实。

按"宁可拒绝，不许编造"判：它不该说"完成了"，
它至少应该说"我不知道这一步算不算数"或者"这条计划行不通"。

--------------------------------------------------------------------------
为什么判据钉在 Loop，不钉在引擎

钉在引擎的话，**换一个笨引擎谎言就回来了**。
而 B-7 说终态只能由 Runtime 声明 —— 谁宣布，谁负责核实。
所以这条判据在 `_advance` 的 FINISH 分支上。

--------------------------------------------------------------------------
新增不变量 I-11

    Run 在完成之前，必须没有"自上次规划以来未被处理的失败"。
    有 → REPLAN（不是 FINISH）；重规划吃预算（I-10），
    救不回来的最终会走到 FAILED —— 既不谎报成功，也不空转。

它也给 M76 修好的那条 REPLAN 边带来了**第一个真正的触发条件**：
在此之前没有任何引擎会产出 REPLAN，那条边只是"通了"而已。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)
from .test_replan import VaryingPlanner


def _bad_tool(state: State) -> Action:
    """一个必然失败的动作：工具不存在。"""
    return Action(
        run_id=state.run_id,
        action_type=ActionType.TOOL_CALL,
        payload={"tool": "does_not_exist"},
    )


def _finish(state: State) -> Action:
    return Action(run_id=state.run_id, action_type=ActionType.FINISH)


class FailThenFinishForever:
    """一个"失败一次就想收工"的引擎：坏动作 → FINISH → 坏动作 → FINISH …

    它代表最该被拦住的那类行为：失败了，但立刻宣称目标达成。
    """

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, state: State) -> Decision:
        self.calls += 1
        wanted = _bad_tool(state) if self.calls % 2 == 1 else _finish(state)
        return Decision(
            run_id=state.run_id,
            selected_action=wanted,
            rationale="scripted",
        )


class TestFailureBlocksTheCompletion(LoopTestBase):
    def _loop_with_bad_tool(self, max_steps: int = 6) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=max_steps),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
            config=AgentLoopConfig(max_steps=max_steps),
        )

    def test_a_failed_step_then_finish_does_not_complete(self):
        """探针里那条：I-11 之前，第二步就宣布 completed。"""
        loop = self._loop_with_bad_tool()
        state = loop.start("boom")
        loop.decision_engine = ScriptedDecisionEngine([_bad_tool(state)])

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        second = loop.step()
        self.assertEqual(
            second,
            StepOutcome.REPLANNED,
            "a run whose only real step failed must not be announced as finished",
        )
        self.assertFalse(loop.agent_run.is_terminal)
        self.assertNotEqual(loop.agent_run.status.value, "completed")

    def test_the_failure_is_still_visible_in_the_state(self):
        """控制组：失败确实进了 State —— 判据有没有东西可看。"""
        loop = self._loop_with_bad_tool()
        state = loop.start("boom")
        loop.decision_engine = ScriptedDecisionEngine([_bad_tool(state)])
        loop.step()
        kinds = [o.kind for o in state.observations]
        self.assertIn("execution_failed", kinds)

    def test_after_a_real_replan_a_clean_attempt_can_still_finish(self):
        """I-11 不是"失败过就永远不能完成"——**但前提是那份计划真的换了**。

        按"上次规划"划界：换了一份计划之后，旧失败不再挡路。
        否则一个"第一步失败、换计划后成功"的 Run 会被判死刑。

        ⚠️ 这里**必须**用 VaryingPlanner：用 ScriptedPlanner（每轮换汤不换药）
        的话 I-12 会先把 Run 判死 —— 这正是本轮要补的那条（见 test_replan_must_change）。
        """
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=VaryingPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("boom")
        loop.decision_engine = ScriptedDecisionEngine([_bad_tool(state)])

        self.assertEqual(loop.step(), StepOutcome.FAILED)
        self.assertEqual(loop.step(), StepOutcome.REPLANNED)
        # 真的换了一份计划，且新计划下没有失败 → 允许收尾
        self.assertEqual(loop.step(), StepOutcome.FINISHED)
        self.assertEqual(loop.agent_run.status.value, "completed")


class TestItDoesNotSpinForever(LoopTestBase):
    """I-10 与 I-11 的交界：一直救不回来的 Run 必须走到 FAILED。"""

    def test_a_run_that_keeps_failing_ends_failed_not_completed(self):
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(max_steps=4),
            planner=ScriptedPlanner(),
            decision_engine=FailThenFinishForever(),
            config=AgentLoopConfig(max_steps=4),
        )
        state = loop.start("boom")

        calls = 0
        while not loop.agent_run.is_terminal and calls < 50:
            loop.step()
            calls += 1

        self.assertTrue(loop.agent_run.is_terminal, f"ran {calls} rounds without ending")
        self.assertEqual(
            loop.agent_run.status.value,
            "failed",
            "a run that never recovers must be FAILED — the one thing it must "
            "never be is 'completed'",
        )
        # 而且失败的事实没有被吞掉
        kinds = [o.kind for o in state.observations]
        self.assertIn("execution_failed", kinds)


class TestTheBlockIsNotUnconditional(LoopTestBase):
    """控制组：没有失败时，FINISH 照旧 —— 上一条不是因为"永远挡着"才过的。"""

    def test_a_clean_run_still_finishes(self):
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([]),
        )
        loop.start("2+3=?")
        self.assertEqual(loop.step(), StepOutcome.FINISHED)
        self.assertEqual(loop.agent_run.status.value, "completed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
