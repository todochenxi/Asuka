"""M79 · 终态必须带上原因（B-12）。

--------------------------------------------------------------------------
探针实录（本轮起因）

M78 冻完之后，去问了一句"一个 FAILED 的 Run，账本说得出它是怎么死的吗":

    === A budget exhausted ===
      status : failed
      history: ['failed', 'failed', 'budget_exhausted']
      run.finished | {'status': 'failed'}

    === B replan produced nothing new ===
      status : failed
      history: ['replanned', 'failed']
      run.finished | {'status': 'failed'}

    两份 payload 一样吗： True

--------------------------------------------------------------------------
难堪的地方在于：这是**自己打自己脸**

`_declare_terminal` 的 docstring 原话是：

    Trace 里的这条 `run.finished` 是"谁宣布了这个 Run 结束"的**唯一证据**

而那条唯一证据里恰恰没有最关键的那个字 —— **为什么**。

把 FAILED 收成一个出口是对的（S-7：补偿挂在这里才不会漏）。
代价是**四种不同的死法从这里出去之后长得一模一样**：

    预算耗尽 / 换不出新计划 / 连续被拒 / ……

运维手上有的只是账本。`loop.history` 与 `last_outcome` 只在内存里，
快照不带它们（`RunSnapshot` 里没有 history 字段），进程一死就蒸发。
于是"这个 Run 为什么失败"在**可审计的记录上无从查证**。

这与 F-1 治过的是同一类病 —— 只不过 F-1 治的是"三种停在页面上是同一副
样子"（挂起 / 完成 / 预算耗尽），这次是**同一个 FAILED 内部的三种成因**。

--------------------------------------------------------------------------
B-12

    终态声明必须带上原因，原因落在 `run.finished` 的 payload 上。

实现方式的关键不是"加个字段"，而是 **`reason` 是必填关键字参数**：
新增一个终态出口时，不写原因就构造不出这次调用。
默认值的诱惑在于它会让"忘了说为什么"看起来像"没什么可说的"。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_harness.cost import Budget as CostBudget
from packages.agent_harness.harness import Harness
from packages.agent_runtime.loop import (
    AgentLoop,
    AgentLoopConfig,
    StepOutcome,
)
from packages.agent_runtime.trace import FINISHED

from .test_agent_loop import (
    LoopTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


class StubbornPlanner:
    """无论被问多少次都给同一条路 —— I-12 要守的那种"想不出别的办法"。"""

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state: State) -> Plan:
        self.calls += 1
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n1", name="ask-llm"),),
        )


class AlwaysReplan:
    def decide(self, state: State) -> Decision:
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.REPLAN),
            rationale="never satisfied",
        )


class AlwaysToolCall:
    """永远要调工具，永远不 FINISH —— 迟早撞预算。"""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, state: State) -> Decision:
        self.calls += 1
        return Decision(
            run_id=state.run_id,
            selected_action=Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "calculator", "args": {"expr": "1+1"}},
            ),
            rationale="keep going",
        )


def _loop(
    base: LoopTestBase,
    *,
    max_steps: int = 6,
    harness: Harness | None = None,
    **config: object,
) -> AgentLoop:
    return AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(max_steps=max_steps),
        planner=ScriptedPlanner(),
        decision_engine=ScriptedDecisionEngine([]),
        config=AgentLoopConfig(max_steps=max_steps, **config),  # type: ignore[arg-type]
        harness=harness,
    )


def _run_to_the_end(loop: AgentLoop, *, limit: int = 20) -> None:
    for _ in range(limit):
        loop.step()
        if loop.agent_run.is_terminal:
            return
    raise AssertionError("run did not reach a terminal state within the limit")


def _finished(loop: AgentLoop) -> dict:
    """账本上那条 `run.finished` 的 payload —— 运维唯一能看到的东西。"""
    for entry in reversed(loop.trace.entries):
        if entry.kind == FINISHED:
            return dict(entry.payload)
    raise AssertionError("no run.finished entry in the ledger")


class TestATerminalDeclarationSaysWhy(LoopTestBase):
    def test_budget_exhaustion_names_the_budget(self) -> None:
        loop = _loop(self, max_steps=2)
        loop.start("2+3=?")
        loop.decision_engine = AlwaysToolCall()
        _run_to_the_end(loop)

        self.assertEqual(loop.agent_run.status.value, "failed")
        payload = _finished(loop)
        self.assertEqual(payload.get("status"), "failed")
        self.assertIn("budget", str(payload.get("reason", "")).lower())

    def test_a_replan_that_changes_nothing_names_replan(self) -> None:
        loop = _loop(self, max_steps=6)
        loop.planner = StubbornPlanner()
        loop.start("boom")
        loop.decision_engine = AlwaysReplan()
        _run_to_the_end(loop)

        self.assertEqual(loop.agent_run.status.value, "failed")
        reason = str(_finished(loop).get("reason", "")).lower()
        self.assertIn("replan", reason)

    def test_the_deny_loop_names_the_denials(self) -> None:
        """L-7 那条死路也必须说清是"被拒太多次"，而不是泛泛的 failed。"""
        loop = _loop(
            self,
            max_steps=6,
            # 预算一开始就是负的 → 每个动作都被 Harness 拒（真实机制，不是打桩）
            harness=Harness.default(budget=CostBudget(max_cost=-1.0)),
            max_consecutive_denials=2,
            # ↑ `harness` 是 AgentLoop 的字段，`max_consecutive_denials` 在 config 上
        )
        loop.start("2+3=?")
        loop.decision_engine = AlwaysToolCall()
        _run_to_the_end(loop)

        self.assertEqual(loop.agent_run.status.value, "failed")
        reason = str(_finished(loop).get("reason", "")).lower()
        self.assertIn("denied", reason)

    def test_cancellation_names_who_and_why(self) -> None:
        """B-8：取消的归因不能只在 `run.cancelled` 那条上 —— 终态那条也要有。"""
        loop = _loop(self)
        loop.start("2+3=?")
        loop.cancel(reason="user changed their mind", by="alice")

        reason = str(_finished(loop).get("reason", ""))
        self.assertIn("alice", reason)
        self.assertIn("user changed their mind", reason)

    def test_completion_is_not_silent_either(self) -> None:
        loop = _loop(self)
        loop.start("2+3=?")
        self.assertEqual(loop.step(), StepOutcome.FINISHED)

        self.assertEqual(_finished(loop).get("status"), "completed")
        self.assertTrue(str(_finished(loop).get("reason", "")).strip())


class TestTheLedgerCanTellTwoDeathsApart(LoopTestBase):
    """探针那个 `True` 反过来写 —— 两条不同的死法必须留下两条不同的记录。"""

    def test_budget_and_replan_failure_are_distinguishable(self) -> None:
        a = _loop(self, max_steps=2)
        a.start("2+3=?")
        a.decision_engine = AlwaysToolCall()
        _run_to_the_end(a)

        b = _loop(self, max_steps=6)
        b.planner = StubbornPlanner()
        b.start("boom")
        b.decision_engine = AlwaysReplan()
        _run_to_the_end(b)

        self.assertEqual(a.agent_run.status.value, "failed")
        self.assertEqual(b.agent_run.status.value, "failed")
        self.assertNotEqual(
            _finished(a),
            _finished(b),
            "two runs that died for different reasons must not leave the same "
            "entry in the ledger — otherwise 'why did this run fail' is not "
            "answerable from the audit record",
        )

    def test_a_new_exit_cannot_omit_the_reason(self) -> None:
        """`reason` 是必填的：新增一个出口时，不写原因就构造不出这次调用。"""
        loop = _loop(self)
        loop.start("2+3=?")
        with self.assertRaises(TypeError):
            loop._declare_terminal(loop.agent_run.status)  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
