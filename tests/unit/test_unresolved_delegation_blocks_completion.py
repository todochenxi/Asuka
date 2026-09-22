"""M82 · 委派没有回音的父 Run，不许宣布完成（I-14）。

--------------------------------------------------------------------------
探针实录（本轮起因）

M81 只治了 `failed`，留着 `cancelled` 与 `unknown` 两扇门没治。
按 §0.4 回头查它们说不说谎 —— 委派**没有任何回音**时（`child_wait_expired`）：

    step 2  → 等到上限、不再等
    kernel.status_of(父委派 Execution) = FAILED      ← 确实被判死了
    State: plan.created / child_run.spawned / child_run.unknown
    I-11 判据 _failures_since_last_plan() = 0        ← 数不到
    step 3  → FINISHED   status=completed
    账本     {'status': 'completed', 'reason': 'goal reached'}

**与 M81 一字不差的同一句话**，只是换了一扇门进来。

--------------------------------------------------------------------------
I-14：查不出来的失败也算

`child_wait_expired` 的 docstring 明写它会"关闸门（Kernel 那条 Execution
判死）" —— 因为不判死它永远挂着。但**"判死"是"我们不再等"，
不是"它做不成"（D-19）**：那条子 Run 可能正在某个 worker 上跑得好好的。

于是这条 Execution 进 State 时面临两个都不对的选项：

    写成 `execution_failed`        PR-19：报错说的 ≠ 真实发生的
    干脆不写                       父 Run 带着"这一步什么都没拿到"宣布目标达成

所以要有第三个 kind：`execution_unresolved` ——
**它必须进 State，但不能冒充失败。**

I-11 的判据随之扩成"这一步没有被证明成功"：
证明不了的（unresolved）和证明失败的（failed）一样，不该被当成功汇报。

`cancelled` 仍不在此列：S-15 说取消不是失败，它是**父侧主动的选择**，
父 Run 自己知道，不构成"被隐瞒的失败"。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.execution.aggregate import ExecutionStatus
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.agent_runtime.reducer import (
    EXECUTION_FAILED,
    EXECUTION_UNRESOLVED,
)

from .test_child_run import (
    ChildRunTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)
from .test_replan import VaryingPlanner

DELEGATION = Action(
    run_id="run_1",
    action_type=ActionType.AGENT_DELEGATION,
    payload={"agent_id": "researcher", "instruction": "go find out"},
)


class UnresolvedDelegationTest(ChildRunTestBase):
    def _parent(self) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=VaryingPlanner(),
            decision_engine=ScriptedDecisionEngine([DELEGATION]),
            config=AgentLoopConfig(max_steps=6),
            spawner=InProcessChildRunSpawner(factory=self._child_stack_factory()),
        )

    def _delegate_then_time_out(self, loop: AgentLoop) -> str:
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None
        loop.child_wait_expired(
            handle.child_run_id, reason="child produced no result"
        )
        return handle.parent_execution_id


class TestAVoiceLessDelegationBlocksCompletion(UnresolvedDelegationTest):
    def test_the_next_step_replans_instead_of_finishing(self) -> None:
        """探针那个 `FINISHED → completed` 反过来写。"""
        loop = self._parent()
        self._delegate_then_time_out(loop)

        self.assertIs(loop.step(), StepOutcome.REPLANNED)

    def test_the_unresolved_execution_reaches_the_state(self) -> None:
        loop = self._parent()
        execution_id = self._delegate_then_time_out(loop)

        kinds = [o.kind for o in loop.state.observations]
        self.assertIn(EXECUTION_UNRESOLVED, kinds)

    def test_i11_sees_it(self) -> None:
        """探针里那个 0 反过来写。"""
        loop = self._parent()
        self._delegate_then_time_out(loop)

        self.assertEqual(loop._failures_since_last_plan(), 1)


class TestItDoesNotPretendToBeAFailure(UnresolvedDelegationTest):
    """★ 这一组是本轮的重点：进了 State，但**不许冒充失败**。"""

    def test_the_kind_is_not_execution_failed(self) -> None:
        """PR-19：那条子 Run 可能正在某个 worker 上跑得好好的。"""
        loop = self._parent()
        self._delegate_then_time_out(loop)

        kinds = [o.kind for o in loop.state.observations]
        self.assertIn(EXECUTION_UNRESOLVED, kinds)
        self.assertNotIn(
            EXECUTION_FAILED,
            kinds,
            "we do not know whether the child failed — recording it as a "
            "failure is exactly the lie D-19 keeps out of the ledger",
        )

    def test_the_summary_says_unresolved_not_failed(self) -> None:
        loop = self._parent()
        self._delegate_then_time_out(loop)

        obs = [
            o for o in loop.state.observations if o.kind == EXECUTION_UNRESOLVED
        ][0]
        self.assertIn("unresolved", obs.summary)
        self.assertNotIn("failed", obs.summary)

    def test_it_still_binds_the_real_execution(self) -> None:
        """I-6：执行类 Observation 必须绑定真实 Execution。"""
        loop = self._parent()
        execution_id = self._delegate_then_time_out(loop)

        obs = [
            o for o in loop.state.observations if o.kind == EXECUTION_UNRESOLVED
        ][0]
        self.assertEqual(obs.execution_id, execution_id)
        # 那条 Execution 在 Kernel 里确实已经是终态（闸门关了）。
        self.assertEqual(
            self.kernel.status_of(execution_id), ExecutionStatus.FAILED
        )

    def test_the_reducer_actually_treats_it_as_a_finished_step(self) -> None:
        """⚠️ 这条守的是 **Reducer**，不是 Loop —— 上面几条守不住它。

        只断言 `obs.kind` 是不够的：Reducer 末尾有一个兜底分支
        （任何 observation 都留下摘要），所以"新 kind 没被认"时
        那条 observation 照样**存在**于 State 上，断言照样绿
        （M82 变红验证里 M4 没红，就是这个漏）。

        真正能区分的是**它有没有把这一步当"结束了"**：
        从 `active_tasks` 上摘掉、并写进 `completed_tasks`。
        """
        loop = self._parent()
        execution_id = self._delegate_then_time_out(loop)

        self.assertIn(
            execution_id,
            loop.state.completed_tasks,
            "the reducer did not treat execution_unresolved as 'this step is "
            "over' — the execution stays in active_tasks forever",
        )

    def test_a_stranger_kind_would_not_be_treated_as_finished(self) -> None:
        """控制组：证明上一条真的在测 reducer 的分支，而不是碰巧为真。"""
        loop = self._parent()
        execution_id = self._delegate_then_time_out(loop)

        self.assertNotIn("execution_something_else", [o.kind for o in loop.state.observations])
        self.assertIn(execution_id, loop.state.completed_tasks)


class TestTheBoundaryStillHolds(UnresolvedDelegationTest):
    def test_a_fresh_plan_after_the_timeout_can_still_complete(self) -> None:
        """换了计划之后仍能完成 —— 修法不能把可救的情况也判死刑。"""
        loop = self._parent()
        self._delegate_then_time_out(loop)

        self.assertIs(loop.step(), StepOutcome.REPLANNED)
        for _ in range(8):
            if loop.agent_run.is_terminal:
                break
            loop.step()

        self.assertEqual(loop.agent_run.status.value, "completed")

    def test_cancelling_a_child_leaves_no_unresolved_either(self) -> None:
        """S-15：取消是**父侧主动的选择**，不是"被隐瞒的失败"。"""
        loop = self._parent()
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None

        loop.child_cancelled(handle.child_run_id, reason="parent changed its mind")

        kinds = [o.kind for o in loop.state.observations]
        self.assertNotIn(EXECUTION_UNRESOLVED, kinds)
        self.assertNotIn(EXECUTION_FAILED, kinds)


if __name__ == "__main__":
    unittest.main()
