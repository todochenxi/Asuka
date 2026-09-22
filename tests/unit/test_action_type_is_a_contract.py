"""M90 / I-20：一个声明了却**没有归宿**的动作，不该让 Run 崩在一个没有账本记录的异常上。

--------------------------------------------------------------------------
洞的形状（probe90.py 实证，不是推演）

`action.py` 声明了 9 个 `ActionType`；`task_factory.ACTION_TO_TASK` 把其中
3 个映射成 `None`，各配一句注释说归宿：

    FINISH: None,   # 终态，不需要执行
    WAIT:   None,   # 等待由 Wake-up Controller 管，不是一个 Task
    REPLAN: None,   # 触发重新规划，由 Loop 自己处理

把 9 个**逐个走一遍**（probe90.py 场景 1），实测：

    llm_call / tool_call / skill_call / agent_delegation  -> 有 Task，有账本
    human_approval / ask_user                             -> 挂起等审批，有账本
    replan / finish                                       -> Loop 自己处理，有终态
    wait                                                  -> ✗ 未捕获的 InvariantViolation
                                                              账本**一条都没有**

`wait` 一路掉到 `_execute()` → `task_factory.from_action()` → 抛异常，
而 `run()` 里**没有 try/except** ⇒ 异常直接冲出整条 Run：

    step() 抛异常   : InvariantViolation
    trace 条目      : []                    ← 一条都没有
    有 run.finished 吗: False
    Run 状态        : created               ← 既不是终态，也没有原因

一条**已经跑了几步、花过钱**的 Run 会整条崩掉，而账本读起来像什么都没发生。

--------------------------------------------------------------------------
根因：那句注释指向的是一条**从未被接通**的线

`WAIT: None` 的注释说"等待由 Wake-up Controller 管"。仓里确实有一个
`wakeup_controller`，但它的输入是 `Suspension`（一条挂起的 Execution），
而 `wait` 动作**不产生 Task → 不产生 Execution → 不可能有 Suspension**。

更要紧的是那条路本来就没接通（probe90.py 场景 3，AST 扫描"谁把它传给了
`suspend(...)`"）：

    SuspensionReason.HUMAN_APPROVAL  ✔ loop.py:1878 真的设过
    SuspensionReason.CHILD_AGENT     ✔ loop.py:2050 真的设过
    SuspensionReason.CHILD_SKILL     ✔ loop.py:2052 真的设过
    SuspensionReason.TIMER           ⚠️ **只有测试造过它，生产代码没有**
    SuspensionReason.EXTERNAL_EVENT  ✗ **全仓没有任何 producer**

而 `execution.py` 里 `SuspensionReason` 的 docstring **自己写下了这个病的判据**：

    "M25 之前，`CHILD_AGENT` 被冻结在这里，但**全仓库没有任何一处设置过它** ——
     和 A-3（幂等键接到 Redis）是同一种病：概念冻结了，实现从没跟上。"

⇒ `wait` 执行不了，不是"少写了一个分支"，而是**它要等的那件事，运行时还没有
产生它的能力**。

--------------------------------------------------------------------------
⚠️ 补这条不变量时，1353 条既有测试**一条不红**。

第五次同款（M85 1287 / M87 1319 / M88 1334 / M89 1353 / M90 1353）。
成因：既有测试的替身 DecisionEngine 从没选过 `WAIT`。

**一条没人能违反的判据，和没有判据，对系统来说是一样的。**

--------------------------------------------------------------------------
本文件要守住什么

    I-20：运行时必须**自述**它能执行的 `ActionType` 集合；
          集合外的动作 → **在产生任何副作用之前**判死 + 点名理由，
          且不许执行它、不许跳过它、不许让它崩在一个没有账本记录的异常上。

五组一起才成立（缺任何一组，这条不变量都能被绕过）：

    1. 可执行的动作行为**不变** —— 控制组。少了它，下面的"拒绝"可能只是
       "所有动作都坏了"。
    2. 那个集合是**推导出来的**，不是手写的。手写的白名单会在 `ActionType`
       新增成员时**静默漏掉**它 —— 而那正是 M88 那一族洞的成因。
    3. ⭐ **自述的能力必须是真的**：每一个自称可执行的动作，`step()` 都不许抛异常。
       少了它，"支持集合"就只是一句愿望（与 M88 的 `kind` 完全同款）。
    4. 集合外的动作 → **FAILED + 零副作用 + 账本上有理由**。
       少了"账本上有理由"，就退化成本轮要治的那个洞本身。
    5. 一条**已经跑了几步**的 Run 不会整条崩掉 —— 前几步的记录还在，
       而且"为什么停"写得出来。
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.run import TERMINAL_RUN_STATUSES
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from packages.agent_runtime.reducer import RUN_FINISHED
from packages.agent_runtime.task_factory import (
    ACTION_TO_TASK,
    EXECUTABLE_ACTION_TYPES,
    LOOP_HANDLED_ACTION_TYPES,
    TASK_PRODUCING_ACTION_TYPES,
    UNEXECUTABLE_ACTION_TYPES,
    TaskFactory,
)

#: I-8 要求 HUMAN_APPROVAL 必须带 timeout，否则构造就抛。
#: 其他类型带上也无害 —— 统一给足，免得"少给一个字段"变成另一个失败原因
#: （M85 的断言隔离：除了被测那一项，其他字段一律给足）。
_TTL = timedelta(seconds=30)


class OneNodePlanner:
    """一份最简单的计划，让 `step()` 有节点可取。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="zero"),))


def _action(run_id: str, kind: ActionType) -> Action:
    return Action(run_id=run_id, action_type=kind, payload={}, timeout=_TTL)


def _terminal_reason(loop: AgentLoop) -> str:
    """从**账本**里读 `run.finished` 的理由（B-12 / D-37：运维拿到的是账本）。"""
    for entry in reversed(loop.trace.entries):
        if entry.kind == RUN_FINISHED:
            return str(entry.payload.get("reason", ""))
    return ""


class _Base(unittest.TestCase):
    def _new(self, *actions: ActionType) -> tuple[AgentLoop, object]:
        # 刻意**在函数里** import：模块级 import 会让 unittest 把
        # `MinimalLoopTest` 也当成这个文件里的用例收集一遍。
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=OneNodePlanner(),
            decision_engine=ScriptedDecisionEngine([]),
        )
        state = loop.start("2+3=?")
        loop.decision_engine = ScriptedDecisionEngine(
            [_action(state.run_id, k) for k in actions]
        )
        return loop, state


# ============================================================ 1. 控制组
class AnExecutableActionIsUnaffectedTest(_Base):
    """控制组：能执行的动作行为**不变**。

    少了这一组，下面的"拒绝"可能只是"所有动作都坏了" —— 它们会全绿，
    而它们什么都没守住。
    """

    def test_an_llm_call_still_produces_a_task_and_a_ledger_entry(self) -> None:
        loop, _ = self._new(ActionType.LLM_CALL)

        loop.step()

        self.assertIn("task.submitted", [e.kind for e in loop.trace.entries])
        self.assertEqual(len(loop.steps_of_run), 1)

    def test_finish_still_finishes(self) -> None:
        loop, _ = self._new(ActionType.FINISH)

        self.assertEqual(loop.step(), StepOutcome.FINISHED)
        self.assertIn(RUN_FINISHED, [e.kind for e in loop.trace.entries])

    def test_replan_still_replans(self) -> None:
        loop, _ = self._new(ActionType.REPLAN)

        self.assertEqual(loop.step(), StepOutcome.REPLANNED)

    def test_a_human_approval_still_suspends(self) -> None:
        loop, _ = self._new(ActionType.HUMAN_APPROVAL)

        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)
        self.assertIn("approval.requested", [e.kind for e in loop.trace.entries])

    def test_ask_user_still_suspends(self) -> None:
        loop, _ = self._new(ActionType.ASK_USER)

        self.assertEqual(loop.step(), StepOutcome.WAITING_APPROVAL)


# ============================================================ 2. 集合是推导的
class TheExecutableSetIsDerivedTest(unittest.TestCase):
    """核心：那个集合**从 `ACTION_TO_TASK` 推导**，不手写。

    手写的白名单会在 `ActionType` 新增成员时**静默漏掉**它 ——
    而那正是 M88 那一族洞的成因（"声明了却没人管"）。
    """

    def test_the_three_sets_partition_every_action_type(self) -> None:
        """三个集合互斥、且并集恰好是全集 —— 没有成员落在缝里。"""
        union = (
            TASK_PRODUCING_ACTION_TYPES
            | LOOP_HANDLED_ACTION_TYPES
            | UNEXECUTABLE_ACTION_TYPES
        )
        self.assertEqual(union, frozenset(ActionType))
        self.assertEqual(
            len(TASK_PRODUCING_ACTION_TYPES)
            + len(LOOP_HANDLED_ACTION_TYPES)
            + len(UNEXECUTABLE_ACTION_TYPES),
            len(ActionType),
            "有成员同时属于两个集合 —— 那说明推导写错了",
        )

    def test_the_task_producing_set_is_exactly_the_non_none_mappings(self) -> None:
        expected = frozenset(k for k, v in ACTION_TO_TASK.items() if v is not None)
        self.assertEqual(TASK_PRODUCING_ACTION_TYPES, expected)

    def test_the_executable_set_is_the_union_of_the_first_two(self) -> None:
        self.assertEqual(
            EXECUTABLE_ACTION_TYPES,
            TASK_PRODUCING_ACTION_TYPES | LOOP_HANDLED_ACTION_TYPES,
        )

    def test_the_loop_handled_set_is_exactly_finish_and_replan(self) -> None:
        """这两个是"不产生 Task，但 Loop 自己处理"—— 改它要连着改 `_step()`。"""
        self.assertEqual(
            LOOP_HANDLED_ACTION_TYPES, frozenset({ActionType.FINISH, ActionType.REPLAN})
        )

    def test_the_table_covers_every_action_type(self) -> None:
        """`ACTION_TO_TASK` 是"Action → Task 的唯一通道"，它必须**覆盖全集**。

        新加一个 `ActionType` 却忘了决定它的 Task 映射 → 这条红。
        （即便忘了，运行时也会把它当"执行不了"拒绝掉 —— 安全；
          但"忘了决定"本身该是一次**显式的决定**，所以这里也拦一道。）
        """
        self.assertEqual(frozenset(ACTION_TO_TASK), frozenset(ActionType))

    def test_the_task_producing_set_is_inside_the_executable_set(self) -> None:
        """有 Task 映射的必须都能执行 —— 表里有的不能执行不了。"""
        self.assertTrue(TASK_PRODUCING_ACTION_TYPES <= EXECUTABLE_ACTION_TYPES)

    def test_the_unexecutable_set_is_not_empty(self) -> None:
        """这一轮的存在意义就是它 —— 空了说明有人补上了实现（那时该改这条）。"""
        self.assertTrue(UNEXECUTABLE_ACTION_TYPES)


# ============================================================ 3. 自述必须是真的
class TheDeclaredCapabilityIsTrueTest(_Base):
    """核心：**每一个自称可执行的动作，`step()` 都不许抛异常**。

    少了这一组，"支持集合"就只是一句愿望 —— 与 M88 的 `kind`
    （"声明了五种、实际只支持一种"）完全同款。
    """

    def test_no_executable_action_type_raises(self) -> None:
        for kind in sorted(EXECUTABLE_ACTION_TYPES, key=lambda k: k.value):
            with self.subTest(action_type=kind.value):
                loop, _ = self._new(kind)
                try:
                    loop.step()
                except Exception as exc:                   # noqa: BLE001
                    self.fail(
                        f"{kind.value} 自称可执行，却抛了 "
                        f"{type(exc).__name__}: {exc}"
                    )

    def test_every_action_type_is_either_executable_or_refused_cleanly(self) -> None:
        """★ 穷尽性：9 个动作类型，**没有一个**会掉进"抛异常"那个缝里。

        这条不依赖上面那份集合 —— 它直接对 `ActionType` 全集跑一遍。
        """
        for kind in sorted(ActionType, key=lambda k: k.value):
            with self.subTest(action_type=kind.value):
                loop, _ = self._new(kind)
                try:
                    outcome = loop.step()
                except Exception as exc:                   # noqa: BLE001
                    self.fail(
                        f"{kind.value} 既没被执行、也没被诚实拒绝，"
                        f"而是抛了 {type(exc).__name__}: {exc}"
                    )
                self.assertIsInstance(outcome, StepOutcome)


# ============================================================ 4. 拒绝
class AnUnexecutableActionIsRefusedTest(_Base):
    """核心：集合外的动作 → 判死 + 点名理由 + **零副作用** + **账本有记录**。"""

    def test_every_unexecutable_action_is_refused(self) -> None:
        for kind in sorted(UNEXECUTABLE_ACTION_TYPES, key=lambda k: k.value):
            with self.subTest(action_type=kind.value):
                loop, _ = self._new(kind)

                outcome = loop.step()

                self.assertEqual(outcome, StepOutcome.FAILED)
                self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)
                # ★ 拒绝发生在**任何副作用之前**
                self.assertEqual(loop.steps, 0)
                self.assertEqual(loop.steps_of_run, [])

    def test_the_refusal_lands_in_the_ledger(self) -> None:
        """★ 本轮要治的正是这个：此前 trace **一条都没有**。"""
        loop, _ = self._new(*sorted(UNEXECUTABLE_ACTION_TYPES, key=lambda k: k.value))

        loop.step()

        kinds = [e.kind for e in loop.trace.entries]
        self.assertIn(
            RUN_FINISHED,
            kinds,
            f"拒绝必须落账本 —— 此前这里是一条都没有（实测拿到的是 {kinds}）",
        )

    def test_the_refusal_names_the_action_type_the_supported_set_and_the_fix(self) -> None:
        """理由要点齐五样（PR-19：说中真发生了什么，并指出往哪走）。"""
        loop, _ = self._new(*sorted(UNEXECUTABLE_ACTION_TYPES, key=lambda k: k.value))

        loop.step()
        reason = _terminal_reason(loop)

        self.assertIn("wait", reason)              # 哪个动作类型
        self.assertIn("llm_call", reason)          # 运行时支持什么
        self.assertIn("I-20", reason)              # 判据的编号
        # 为什么不能凑合 + 正确的替代路径
        self.assertIn("Wake-up Controller", reason)
        self.assertIn("approval gate", reason)

    def test_the_run_reaches_a_terminal_status(self) -> None:
        """不许停在非终态 —— B-12 说终态必须带原因，而此前**连终态都没有**。"""
        loop, _ = self._new(*sorted(UNEXECUTABLE_ACTION_TYPES, key=lambda k: k.value))

        loop.step()

        self.assertIn(loop.agent_run.status, TERMINAL_RUN_STATUSES)


# ============================================================ 5. 整条 Run
class TheWholeRunSurvivesTest(_Base):
    """核心：一条**已经跑了几步**的 Run 不会整条崩掉。"""

    def test_run_returns_instead_of_raising(self) -> None:
        loop, _ = self._new(ActionType.LLM_CALL, ActionType.LLM_CALL, ActionType.WAIT)

        loop.run()          # 此前这里会抛 InvariantViolation

        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)
        self.assertIn(loop.agent_run.status, TERMINAL_RUN_STATUSES)

    def test_the_earlier_steps_are_still_in_the_ledger(self) -> None:
        """★ 前几步**真的发生过**（花钱、留痕）—— 账本必须留着它们。"""
        loop, _ = self._new(ActionType.LLM_CALL, ActionType.LLM_CALL, ActionType.WAIT)

        loop.run()
        kinds = [e.kind for e in loop.trace.entries]

        self.assertGreaterEqual(kinds.count("task.submitted"), 1)
        self.assertIn(RUN_FINISHED, kinds)

    def test_the_reason_is_the_real_cause_not_a_side_effect(self) -> None:
        """D-37：终态理由必须是**真正的死因**。"""
        loop, _ = self._new(ActionType.LLM_CALL, ActionType.WAIT)

        loop.run()
        reason = _terminal_reason(loop)

        self.assertIn("I-20", reason)
        self.assertIn("wait", reason)


# ============================================================ 6. 两种"没有 Task"
class TheTwoKindsOfNoTaskAreDistinctTest(unittest.TestCase):
    """`from_action()` 要分清**调用方的 bug** 与**能力的缺口**（PR-19）。"""

    def _call(self, kind: ActionType) -> str:
        action = Action(run_id="run-x", action_type=kind, payload={}, timeout=_TTL)
        with self.assertRaises(InvariantViolation) as ctx:
            TaskFactory().from_action(action)
        return str(ctx.exception)

    def test_a_loop_handled_type_is_reported_as_a_caller_bug(self) -> None:
        for kind in sorted(LOOP_HANDLED_ACTION_TYPES, key=lambda k: k.value):
            with self.subTest(action_type=kind.value):
                message = self._call(kind)
                self.assertIn("caller bug", message)
                self.assertIn("I-4", message)

    def test_an_unexecutable_type_is_reported_as_a_capability_gap(self) -> None:
        for kind in sorted(UNEXECUTABLE_ACTION_TYPES, key=lambda k: k.value):
            with self.subTest(action_type=kind.value):
                message = self._call(kind)
                self.assertIn("I-20", message)
                self.assertIn("nothing executes it", message)

    def test_the_two_messages_are_not_the_same_sentence(self) -> None:
        """少了这条，两种"没有 Task"会退化成同一句话 —— 读的人分不出该改哪边。"""
        bug = self._call(ActionType.FINISH)
        gap = self._call(next(iter(UNEXECUTABLE_ACTION_TYPES)))
        self.assertNotEqual(bug, gap)


if __name__ == "__main__":
    unittest.main()
