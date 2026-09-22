"""M83 · 取消不是"什么都不知道"（I-15）。

--------------------------------------------------------------------------
起因：M82 留下的一句**没有实证的判断**

M82 把 `unknown` 那扇门治了，但文档里对 `cancelled` 只写了一句判断：

> S-15 说取消是父侧主动的选择，父 Run 自己知道，
> 不构成"被隐瞒的失败"。

**判断不能当结论用。** 这一轮把父子两侧关于"取消"的信息都撞了一遍
（`probe83.py`），结论是：那句话成立，而且它有明确的结构性理由。

--------------------------------------------------------------------------
A 与 B：机制上无法区分，因此也不该区分

    case A  父 Run 等不下去，自己取消子 Run
    case B  子 Run 被别人取消，父 Run 只是收到通知

探针实测：两者在父子两侧留下的记录**完全一样** ——
因为**能走到这里的只有 A**：`child_cancelled()` 的调用者是父侧
（`ChildRunWaker` 收到 `child_run.cancelled` 事件后交回）。
"子 Run 被别人取消、父 Run 事后才知道"是**还没有实现的能力**，
不是一个正在说谎的既有路径。

所以这里不该发明一个"父侧主动 vs 被动"的区分 —— 那会是一个
描述不了任何真实路径的字段。

--------------------------------------------------------------------------
I-15 要守的其实是这一条：**取消不是"我们不知道"**（D-19）

三个终态在父侧留下的**信息量**是不同的：

| 终态 | State 上的痕迹 | 父 Agent 知道的 |
|---|---|---|
| `failed` | `child_run.finished` + `execution_failed`（I-13） | 它失败了 |
| `cancelled` | `child_run.finished`（outcome/error 都在） | **为什么被取消**（谁、什么理由） |
| `unknown` | `child_run.finished` + `execution_unresolved`（I-14） | **什么都不知道** |

`cancelled` 与 `unknown` 都不产生 `execution_failed` —— 这是对的。
但如果一条 `cancelled` 路径在父侧**什么都不留**，父 Run 就既不知道
自己被取消了、也不知道为什么，那才是真的说谎。

所以 I-15 判的是：**取消必须在父侧留下可读的原因**，
且它不能被记成"不知道"（那是 D-19 的形状）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunKind,
    ChildRunRegistry,
    InProcessChildRunSpawner,
)
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


class CancellationIsNotIgnoranceTest(ChildRunTestBase):
    USER_REASON = "operator cancelled the child run"

    def _cancelled_parent(self, reason: str | None = None) -> AgentLoop:
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=VaryingPlanner(),
            decision_engine=ScriptedDecisionEngine([DELEGATION]),
            config=AgentLoopConfig(max_steps=6),
            spawner=InProcessChildRunSpawner(factory=self._child_stack_factory()),
        )
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None
        loop.child_cancelled(
            handle.child_run_id, reason=reason or self.USER_REASON
        )
        return loop

    # ── 留下来的原因 ─────────────────────────────────────────────

    def test_the_parent_can_read_why_it_was_cancelled(self) -> None:
        """I-15 的落点：取消必须在父侧留下**可读的原因**。"""
        loop = self._cancelled_parent()

        entries = [
            o for o in loop.state.observations if o.kind == "child_run.finished"
        ]
        self.assertEqual(len(entries), 1)
        content = entries[0].content
        self.assertEqual(content["outcome"], "cancelled")
        self.assertEqual(content["error"], self.USER_REASON)

    def test_the_summary_says_cancelled(self) -> None:
        loop = self._cancelled_parent()

        entry = [
            o for o in loop.state.observations if o.kind == "child_run.finished"
        ][0]
        self.assertIn("cancelled", entry.summary)

    # ── 它不是"什么都不知道" ─────────────────────────────────────

    def test_it_is_not_recorded_as_ignorance(self) -> None:
        """D-19 的形状是 `execution_unresolved` —— 取消不该长得像它。"""
        loop = self._cancelled_parent()

        kinds = [o.kind for o in loop.state.observations]
        self.assertNotIn(
            EXECUTION_UNRESOLVED,
            kinds,
            "a cancellation is not 'we do not know' — the parent chose it and "
            "knows the reason; recording it as unresolved would erase that",
        )

    def test_it_is_not_recorded_as_a_failure_either(self) -> None:
        """S-15：取消不是失败。"""
        loop = self._cancelled_parent()

        self.assertNotIn(
            EXECUTION_FAILED, [o.kind for o in loop.state.observations]
        )


class TestTheThreeOutcomesStayDistinguishable(CancellationIsNotIgnoranceTest):
    """三个终态在父侧的信息量不同 —— 这一组把它们并排比出来。"""

    def _trace_kinds(self, loop: AgentLoop) -> set[str]:
        return {o.kind for o in loop.state.observations}

    def test_cancelled_vs_unresolved_vs_failed(self) -> None:
        cancelled = self._cancelled_parent()
        kinds = self._trace_kinds(cancelled)

        self.assertIn("child_run.finished", kinds)
        self.assertNotIn(EXECUTION_UNRESOLVED, kinds)
        self.assertNotIn(EXECUTION_FAILED, kinds)

    def test_the_control_group_a_timeout_looks_different(self) -> None:
        """控制组：同一个夹具下走 `unknown`，痕迹必须**明显不同** ——
        否则上一条不是"取消特殊"，而是"这套断言根本区分不了东西"。"""
        loop = AgentLoop(
            kernel=self.kernel,
            worker=self.worker,
            interpreter=ScriptedInterpreter(),
            planner=VaryingPlanner(),
            decision_engine=ScriptedDecisionEngine([DELEGATION]),
            config=AgentLoopConfig(max_steps=6),
            spawner=InProcessChildRunSpawner(factory=self._child_stack_factory()),
        )
        loop.start("delegate it")
        self.assertIs(loop.step(), StepOutcome.WAITING_CHILD)
        handle = loop.pending_child
        assert handle is not None
        loop.child_wait_expired(handle.child_run_id, reason="no result")

        kinds = self._trace_kinds(loop)
        self.assertIn(EXECUTION_UNRESOLVED, kinds)
        self.assertNotIn(EXECUTION_FAILED, kinds)


class TestWhatTheParentHears(ChildRunTestBase):
    """`_reason()` 是父 Agent 读到的**那句话**（D-37），它也要分得开。"""

    def _waker_reason(self, handle: ChildRunHandle) -> str:
        registry = ChildRunRegistry()
        registry.bind(handle)
        registry.mark_finished(
            handle.child_run_id, handle.status, dict(handle.result or {})
        )
        waker = ChildRunWaker(
            registry=registry, recovery=None, saga=None, driver=None
        )
        return waker._reason(handle)

    def _handle(self, status: str, result: dict) -> ChildRunHandle:
        return ChildRunHandle(
            child_run_id="child_1",
            kind=ChildRunKind.AGENT,
            parent_run_id="run_parent",
            target="researcher",
            action=Action(
                run_id="run_parent", action_type=ActionType.AGENT_DELEGATION
            ),
            parent_task_id="task_1",
            parent_execution_id="exec_1",
            status=status,
            result=result,
        )

    def test_a_cancelled_child_says_cancelled_not_failed(self) -> None:
        heard = self._waker_reason(
            self._handle("cancelled", {"reason": "operator stopped it"})
        )
        self.assertTrue(heard.startswith("child run cancelled:"))
        self.assertIn("operator stopped it", heard)

    def test_a_cancelled_child_without_a_cause_does_not_get_called_a_failure(self) -> None:
        """控制组：没有原因也要说 cancelled，不能说 failed。"""
        heard = self._waker_reason(self._handle("cancelled", {}))
        self.assertIn("cancelled", heard)
        self.assertNotIn("failed", heard)

    def test_a_failed_child_still_says_failed(self) -> None:
        """控制组：证明上面两条不是因为 `_reason` 一律输出 cancelled。"""
        heard = self._waker_reason(
            self._handle("failed", {"reason": "step budget exhausted (2/2)"})
        )
        self.assertTrue(heard.startswith("child run failed:"))


if __name__ == "__main__":
    unittest.main()
