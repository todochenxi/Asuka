"""M80 · 子 Run 的死因必须跟着结果一起交给父 Run（D-37）。

--------------------------------------------------------------------------
探针实录（本轮起因）

B-12 让子 Run 的终态带上了死因，于是去问了一句"父 Run 听到的是不是它"：

    === A 子 Run 预算耗尽 ===
      子 Run 真正的死因   step budget exhausted (2/2)
      事件 payload        {'status': 'failed', 'steps': 2, 'summary': ...}
      父 Run 会听到的     child run failed: execution exec_xxx attempt#1
                          COMPLETED (completed)

    === B 子 Run 换不出新计划 ===
      子 Run 真正的死因   replan produced a plan with the same shape ...
      父 Run 会听到的     child run failed: plan invalidated; replanning

--------------------------------------------------------------------------
为什么说它是"说谎"而不是"少了个字段"

A 那一条最刺眼：**不是说漏了，是说反了**。父 Run 被告知"一个已 COMPLETED
的执行导致了失败"。而 `summary` 恰恰是"最后一条 observation"——
它是**过程**，不是**结论**，两者不能互相顶替。

B 那一条是同一种病的轻症：父 Run 听到的是"正在重规划"，
听起来像还没结束，而真实结局恰恰是"重规划这条路也走不通"。

--------------------------------------------------------------------------
它比空洞 238 强在哪：有真实消费者

父 State 里那条 `child_run.finished` 的 `content["error"]` 是这句
`_reason()` 的产物，也是父 Agent 决定"下一步怎么走"时**唯一**能看到的东西。
而 I-12 要求重规划**换一条路** —— 理由错了就换不对路。

--------------------------------------------------------------------------
D-37

    子 Run 交给父 Run 的失败原因，必须是子 Run 真正的死因。

死因缺失时（落库早于 D-37 的那批 `result`）必须说"没记录到"，
**不许拿 summary 顶替** —— 那是用一句过程描述冒充结论，
而它恰恰可能是反的（A 那条）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunIdentity,
    ChildRunKind,
    ChildRunRegistry,
)
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)
from .test_terminal_reason import (
    AlwaysReplan,
    AlwaysToolCall,
    StubbornPlanner,
)

CHILD_RUN_ID = "child_1"


def _handle() -> ChildRunHandle:
    return ChildRunHandle(
        child_run_id=CHILD_RUN_ID,
        kind=ChildRunKind.AGENT,
        parent_run_id="run_parent",
        target="researcher",
        action=Action(run_id="run_parent", action_type=ActionType.AGENT_DELEGATION),
        parent_task_id="task_1",
        parent_execution_id="exec_1",
    )


def _child_loop(
    base: LoopTestBase, *, planner: object, max_steps: int, engine: object
) -> tuple[AgentLoop, ChildRunRegistry]:
    """一条**被登记过的**子 Run —— 否则 `_emit_child_run_outcome` 什么都不发。"""
    registry = ChildRunRegistry()
    bound = registry.bind(_handle())
    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(max_steps=max_steps),
        planner=planner,  # type: ignore[arg-type]
        decision_engine=ScriptedDecisionEngine([]),
        config=AgentLoopConfig(max_steps=max_steps),
    )
    loop.child_identity = ChildRunIdentity(bound, registry)
    loop.start("do research")
    loop.decision_engine = engine  # type: ignore[assignment]
    return loop, registry


def _to_the_end(loop: AgentLoop, *, limit: int = 20) -> None:
    for _ in range(limit):
        loop.step()
        if loop.agent_run.is_terminal:
            return
    raise AssertionError("child run did not reach a terminal state")


def _event_result(loop: AgentLoop) -> dict:
    """父 Run 那边真正会收到的那个 payload 里的 `result`。"""
    for event in loop.kernel.outbox.pending(limit=50):
        if "child_run" in str(getattr(event, "event_type", "")):
            return dict(event.payload.get("result") or {})
    raise AssertionError("no child_run event was emitted")


def _heard_by_parent(registry: ChildRunRegistry) -> str:
    """父 State 里 `content["error"]` 会是什么 —— 只有这一条线索。"""
    handle = registry.for_child(CHILD_RUN_ID)
    assert handle is not None
    waker = ChildRunWaker(registry=registry, recovery=None, saga=None, driver=None)
    return waker._reason(handle)


class TestTheCauseTravelsWithTheResult(LoopTestBase):
    def test_budget_exhaustion_reaches_the_parent(self) -> None:
        loop, registry = _child_loop(
            self, planner=ScriptedPlanner(), max_steps=2, engine=AlwaysToolCall()
        )
        _to_the_end(loop)

        self.assertIn("budget", str(_event_result(loop).get("reason", "")))
        self.assertIn("budget", _heard_by_parent(registry))

    def test_the_replan_death_reaches_the_parent(self) -> None:
        loop, registry = _child_loop(
            self, planner=StubbornPlanner(), max_steps=6, engine=AlwaysReplan()
        )
        _to_the_end(loop)

        self.assertIn("replan", str(_event_result(loop).get("reason", "")))
        self.assertIn("replan", _heard_by_parent(registry))

    def test_the_parent_hears_the_cause_not_the_last_words(self) -> None:
        """探针里那个"COMPLETED 导致失败"反过来写。

        ⚠️ 这条必须同时断言 `summary` 里**确实**写着 COMPLETED：
        否则"父 Run 听到的不是执行摘要"会在两者恰好一样时假绿，
        而它之所以值得断言，恰恰因为它们是反的。
        """
        loop, registry = _child_loop(
            self, planner=ScriptedPlanner(), max_steps=2, engine=AlwaysToolCall()
        )
        _to_the_end(loop)

        result = _event_result(loop)
        summary = str(result.get("summary", ""))
        cause = str(result.get("reason", ""))

        self.assertTrue(summary, "控制组：这条子 Run 确实留下了最后一句 observation")
        self.assertIn("COMPLETED", summary)
        self.assertNotEqual(cause, summary)

        heard = _heard_by_parent(registry)
        self.assertIn(cause, heard)
        self.assertNotIn("COMPLETED", heard)

    def test_cancellation_carries_its_cause_too(self) -> None:
        """B-8：取消的归因同样要走到父 Run，不能只留在子 Run 自己的账本上。"""
        loop, registry = _child_loop(
            self, planner=ScriptedPlanner(), max_steps=6, engine=AlwaysToolCall()
        )
        loop.cancel(reason="parent gave up waiting", by="alice")

        self.assertIn("alice", str(_event_result(loop).get("reason", "")))

        heard = _heard_by_parent(registry)
        self.assertTrue(heard.startswith("child run cancelled:"))
        self.assertIn("parent gave up waiting", heard)

    def test_a_new_emitter_cannot_omit_the_cause(self) -> None:
        """`reason` 是必填关键字参数：不给死因就构造不出这次调用。"""
        loop, _registry = _child_loop(
            self, planner=ScriptedPlanner(), max_steps=6, engine=AlwaysToolCall()
        )
        with self.assertRaises(TypeError):
            loop._emit_child_run_outcome(loop.agent_run.status)  # type: ignore[call-arg]


class TestTheWakerDoesNotInventACause(LoopTestBase):
    """落库早于 D-37 的那批 `result` 没有 `reason` 字段。

    这时把 `summary` 当死因说出去就是**编造** —— 宁可少说，也要说清
    那句话是什么性质的。
    """

    def _legacy(self, status: str, result: dict) -> str:
        registry = ChildRunRegistry()
        registry.bind(_handle())
        registry.mark_finished(CHILD_RUN_ID, status, result)
        return _heard_by_parent(registry)

    def test_a_legacy_failure_says_the_cause_was_not_recorded(self) -> None:
        heard = self._legacy(
            "failed",
            {"summary": "execution exec_1 attempt#1 COMPLETED (completed)"},
        )
        self.assertIn("cause not recorded", heard)
        # summary 还在（它不是垃圾），但被标明是"最后一条 observation"。
        self.assertIn("last observation", heard)

    def test_a_legacy_failure_does_not_pass_the_summary_off_as_the_cause(self) -> None:
        """`child run failed: <summary>` 那个旧格式必须消失 —— 它会说反。"""
        heard = self._legacy("failed", {"summary": "everything was COMPLETED"})
        self.assertNotIn("failed: everything was COMPLETED", heard)

    def test_a_legacy_failure_with_nothing_at_all_is_still_honest(self) -> None:
        heard = self._legacy("failed", {})
        self.assertIn("cause not recorded", heard)
        self.assertNotIn("last observation", heard)

    def test_the_control_group_still_sees_a_real_cause(self) -> None:
        """控制组：证明上面三条不是因为"这一路根本走不通"。"""
        heard = self._legacy(
            "failed", {"reason": "step budget exhausted (2/2)", "summary": "boom"}
        )
        self.assertEqual(heard, "child run failed: step budget exhausted (2/2)")


if __name__ == "__main__":
    unittest.main()
