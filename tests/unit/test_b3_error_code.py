"""M51 / 错误码语义：B-3「终态不可变」该报 409，不是 422。

--------------------------------------------------------------------------
洞的形状（并发探测时撞到的）

同一条 Run，两次"终态之后再推进"：

    单线程：Run 已 completed → 再 drive   → 409 RUN_TERMINAL
    并发  ：两个线程同时 drive，一个先跑完，
            另一个推到一半才发现已终态   → 422 INVARIANT_VIOLATION

**同一个事实**（这条 Run 已经终态了，推不动了），给了**两个错误码**。

调用方要判断"是不是终态"，就得同时匹配 409 和"422 且消息里有 B-3" ——
而后者是靠**错误消息里的字样**判断的，消息一改就断。

--------------------------------------------------------------------------
为什么 422 是错的

`packages/agent_api/errors.py` 的映射表把两者分得很清楚：

    409  IllegalTransition / TerminalStateError
         "资源存在、状态明确，只是这个转换不允许"
    422  InvariantViolation
         "请求本身不合法" —— 改请求就有用

B-3 是**前者**：Run 确实存在、状态确实明确了（completed），
只是"已经 completed 就不能变成 suspended"这个转换不被允许。
把它报成 422，等于告诉调用方"你的请求写得不对，改改再来" ——
而它改一万次结果都一样：那条 Run 已经终态了。

--------------------------------------------------------------------------
修法只改异常**类型**，不改消息

消息里那句 `B-3: run ... is already ...; cannot become ...` 是有用的
（它说清了"现在是什么、想变成什么"），保留原样。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business.run import AgentRun, AgentRunStatus
from packages.agent_domain.errors import IllegalTransition
from packages.agent_domain.intelligence.goal import Budget, Goal


def _goal(run_id: str = "run_b") -> Goal:
    return Goal(
        run_id=run_id,
        objective="answer it",
        success_criteria=("answer is produced",),
        budget=Budget(max_steps=6),
    )


def make_run(**kw) -> AgentRun:
    kw.setdefault("agent_id", "agent-1")
    kw.setdefault("goal", _goal(kw.get("run_id", "run_b")))
    return AgentRun(**kw)


class TestB3IsAConflictNotABadRequest(unittest.TestCase):
    """终态不可变 = 状态转换不允许（409），不是请求不合法（422）。"""

    def _terminal_run(self) -> AgentRun:
        """一条已经 COMPLETED 的 Run（终态只能由 Runtime 宣布，故走 sync）。"""
        run = make_run()
        run.sync([], runtime_terminal=AgentRunStatus.COMPLETED)
        return run

    def test_b3_raises_illegal_transition(self):
        """B-3 必须是 `IllegalTransition` —— 它才能被映射成 409。"""
        run = self._terminal_run()
        with self.assertRaises(IllegalTransition):
            run.sync([], runtime_terminal=AgentRunStatus.FAILED)

    def test_b3_message_still_says_what_happened(self):
        """改的是异常类型，消息要说清"现在是什么、想变成什么"（PR-19）。"""
        run = self._terminal_run()
        with self.assertRaises(IllegalTransition) as ctx:
            run.sync([], runtime_terminal=AgentRunStatus.FAILED)
        message = str(ctx.exception)
        self.assertIn("B-3", message)
        self.assertIn("completed", message)

    def test_terminal_to_same_status_is_not_a_violation(self):
        """终态 → **同一个**终态不是违规（幂等的重投影必须允许）。

        这条是 B-3 的边界：`new_status != self.status` 才算违规。
        若改类型时顺手放宽成"终态一概不许重投影"，
        每次重投影都会抛 —— 那比报错码更糟。
        """
        run = self._terminal_run()
        run.sync([], runtime_terminal=AgentRunStatus.COMPLETED)
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
