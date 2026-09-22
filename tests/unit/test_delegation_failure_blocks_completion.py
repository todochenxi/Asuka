"""M81 · 委派失败了的父 Run，不许宣布完成（I-13）。

--------------------------------------------------------------------------
探针实录（本轮起因）

M80 冻完之后按 §0.4 那条回头问了一句：
**I-11 刚落地，它自己引入的机制能不能绕过它？** 探针实测（`probe81.py`）：

    step 1  → WAITING_CHILD
    step 2  → 子 Run 失败交回（父 Run 那条委派 Execution 真的进了 FAILED）
    父 State: plan.created / child_run.spawned / child_run.finished
    I-11 判据 _failures_since_last_plan() = 0        ← 数不到
    step 3  → FINISHED   status=completed
    账本     {'status': 'completed', 'reason': 'goal reached'}

**一个委派失败了的父 Run 被宣布 completed，账本上还写着 goal reached。**

--------------------------------------------------------------------------
形状：同一个事实，两条路给出两个答案

那条委派 Execution 确实被 `_close_child_gate` 判成了 FAILED。
正常执行失败时，Loop 会为它写一条 `execution.failed` observation；
而委派这条路不经 Worker，没人写 —— State 上只有 `child_run.finished`，
那条说的是"子 Run 完事了"，**不是**"这条 execution 失败了"，是两件事。

I-11 从 State 读，于是读不到。这是 M77 治掉的那句谎言
（"带着没被处理的失败不许宣布完成"），只是从委派这扇门又进来了。

--------------------------------------------------------------------------
I-13

    委派失败就是一次执行失败，必须进 State（`execution.failed`，
    且绑定那条真实的委派 Execution —— I-6）。

只治 `failed`：
- `cancelled` 不是失败（S-15）；
- `unknown` 是"不知道做没做成"（D-19），算成失败会说过头。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome

from .test_child_run import (
    ChildRunTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)
from .test_replan import VaryingPlanner
from .test_terminal_reason import StubbornPlanner

DELEGATION = Action(
    run_id="run_1",
    action_type=ActionType.AGENT_DELEGATION,
    payload={"agent_id": "researcher", "instruction": "go find out"},
)


class DelegationFailureTest(ChildRunTestBase):
    def _parent(self, *, planner: object) -> AgentLoop:
        return AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=planner,  # type: ignore[arg-type]
            decision_engine=ScriptedDecisionEngine([DELEGATION]),
            config=AgentLoopConfig(max_steps=6),
            spawner=InProcessChildRunSpawner(factory=self._child_stack_factory()),
        )

    def _delegate_then_fail(self, loop: AgentLoop) -> str:
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None
        loop.child_failed(handle.child_run_id, reason="child budget out")
        return handle.parent_execution_id

    def _step_until_terminal(self, loop: AgentLoop, *, limit: int = 8) -> None:
        for _ in range(limit):
            if loop.agent_run.is_terminal:
                return
            loop.step()
        raise AssertionError("run did not reach a terminal state")


class TestAFailedDelegationBlocksCompletion(DelegationFailureTest):
    def test_the_next_step_replans_instead_of_finishing(self) -> None:
        """探针那个 `FINISHED → completed` 反过来写。

        ⚠️ 为什么断言的是"下一步 REPLANNED"而不是"最终不是 completed"：
        I-11 按**上次规划**划界 —— 换一份形状不同的计划之后，判据归零，
        那时完成是**设计允许的**（见 `TestTheBoundaryStillHolds`）。
        于是 I-13 真正的可观测后果是：**不能从委派失败直接走到 FINISH**。
        """
        loop = self._parent(planner=VaryingPlanner())
        self._delegate_then_fail(loop)

        self.assertIs(loop.step(), StepOutcome.REPLANNED)

    def test_a_successful_delegation_does_not_replan(self) -> None:
        """控制组：上一条的 REPLANNED 是**失败**引起的，不是委派机制本身。"""
        loop = self._parent(planner=VaryingPlanner())
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None
        loop.child_completed(handle.child_run_id, {"answer": "42"})

        self.assertIsNot(loop.step(), StepOutcome.REPLANNED)

    def test_with_a_stubborn_planner_the_run_fails_instead(self) -> None:
        """探针原样：换不出新计划 → 走到 FAILED，而不是 completed。"""
        loop = self._parent(planner=StubbornPlanner())
        self._delegate_then_fail(loop)
        self._step_until_terminal(loop)

        self.assertNotEqual(loop.agent_run.status.value, "completed")

    def test_the_failure_reaches_the_state(self) -> None:
        """I-13 的落点：那条委派 Execution 的失败必须真的进 State。"""
        loop = self._parent(planner=VaryingPlanner())
        execution_id = self._delegate_then_fail(loop)

        failed = [
            o for o in loop.state.observations if o.kind == "execution_failed"
        ]
        self.assertTrue(failed, "委派失败没有在 State 上留下 execution.failed")
        # I-6：执行类的 Observation 必须绑定真实 Execution —— 不能是凭空造的。
        self.assertEqual(failed[0].execution_id, execution_id)

    def test_i11_sees_it(self) -> None:
        loop = self._parent(planner=VaryingPlanner())
        self._delegate_then_fail(loop)
        # 探针里那个 0 反过来写。
        self.assertEqual(loop._failures_since_last_plan(), 1)

    def test_the_next_step_replans_instead_of_finishing(self) -> None:
        loop = self._parent(planner=VaryingPlanner())
        self._delegate_then_fail(loop)

        self.assertIs(loop.step(), StepOutcome.REPLANNED)


class TestTheBoundaryStillHolds(DelegationFailureTest):
    """I-11 按"上次规划"划界 —— 修法不能把可救的失败也判死刑。"""

    def test_a_fresh_plan_after_the_failure_can_still_complete(self) -> None:
        """换了计划之后必须还能完成，否则 I-13 就变成"委派失败过就永不能完成"。

        ⚠️ 新计划是在 REPLANNED 的**下一步**才落到 State 上的
        （实测：`plan.invalidated` 与新的 `plan.created` 分两步），
        所以判据归零要等那一步走完，不能在 REPLANNED 当场就断言。
        """
        loop = self._parent(planner=VaryingPlanner())
        self._delegate_then_fail(loop)

        self.assertIs(loop.step(), StepOutcome.REPLANNED)
        self._step_until_terminal(loop)

        self.assertEqual(loop.agent_run.status.value, "completed")
        self.assertEqual(loop._failures_since_last_plan(), 0)


class TestOnlyFailureCounts(DelegationFailureTest):
    """只治 `failed`：取消与"不知道"都不该被算成执行失败。"""

    def test_a_cancelled_child_is_not_an_execution_failure(self) -> None:
        """S-15：取消不是失败。"""
        loop = self._parent(planner=VaryingPlanner())
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None

        loop.child_cancelled(handle.child_run_id, reason="parent gave up")

        kinds = [o.kind for o in loop.state.observations]
        self.assertNotIn("execution_failed", kinds)

    def test_a_successful_child_leaves_no_failure_either(self) -> None:
        """控制组：委派成功时，父 Run 照常完成 —— 上面几条不是因为这一路走不通。"""
        loop = self._parent(planner=VaryingPlanner())
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None

        loop.child_completed(handle.child_run_id, {"answer": "42"})
        self._step_until_terminal(loop)

        self.assertEqual(loop.agent_run.status.value, "completed")
        self.assertNotIn(
            "execution_failed", [o.kind for o in loop.state.observations]
        )


if __name__ == "__main__":
    unittest.main()
