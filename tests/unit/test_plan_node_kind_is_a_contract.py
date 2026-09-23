"""M88 / I-18：`PlanNode.kind` 必须是一份**契约**，不是写在注释里的愿望。

--------------------------------------------------------------------------
洞的形状（probe88.py 实证，不是推演）

`plan.py` 那一行是全部真相：

    kind: str = "task"                      # task / tool / agent / human / decision

* 类型是 `str`，五个值只活在**行尾注释**里；
* `PlanNode.__post_init__` 只查 `node_id` / `name` 非空与自依赖，
  **完全不看 `kind`**；
* 运行时 `_ensure_step()` 只读 `node.node_id` 与 `node.name` ——
  `node.kind` 只被 `_plan_shape()`（I-12 的形状签名）顺带带上。

对比：同一个项目里 `ObservationSource` / `ChildRunKind` / `RiskLevel` /
`ActionType` 全是 `str, Enum` + `__post_init__` 校验 —— 这是**唯一的例外**。

探针实测（修复前）：

    kind='banana'  ->  构造通过        ← 拼错没人管
    kind=''        ->  构造通过        ← 空串也没人管
    kind='TASK'    ->  构造通过        ← 大小写不符也没人管

    kind='human'   ->  step() 序列 ['executed', 'finished']
                       Run 终态 completed
                       approval.requested 条数 0     ← **没有任何人签过字**

    kind='agent'   ->  Run 终态 completed
                       子 Run 条数 0                 ← **委派从未发生**

一个**声明要人签字**的节点被当普通 task 跑完、Run 报 COMPLETED ——
这不是"少了个功能"，是**系统主动说了假话**（与 `SkillExecutor` 那条
`SKILL_NOT_WORKER_EXECUTABLE` 同一族病，那份 docstring 自己写着
"正确的行为不是假装把技能跑一遍，而是把话说清楚"）。

--------------------------------------------------------------------------
⚠️ 补这条不变量时，1334 条既有测试**一条都不红**。

第三次同款（M85 1287 / M87 1319 / M88 1334），成因也一样：
既有测试的 `PlanNode` **全部**用的是默认 `kind="task"`
（全仓 grep `PlanNode(` 只有 `snapshot.py` 一个生产构造点，而测试里
一个带 `kind=` 的都没有）。于是"按 kind 校验"与"不校验"给出同一个答案。

**一条没人能违反的判据，和没有判据，对系统来说是一样的。**

--------------------------------------------------------------------------
本文件要守住什么

    I-18：运行时必须声明它**能执行**的 `PlanNode.kind` 集合；
          计划里出现集合外的 kind，**在产生任何副作用之前**拒绝，
          且不许执行它、不许跳过它、不许当普通 task 跑。

六条一起才成立（缺任何一条，这条不变量都能被绕过）：

    1. 全是 `task` 的计划行为**不变** —— 控制组。少了它，第 3 条可能
       只是"所有计划都坏了"。
    2. `kind` 是一个**闭集**：五个声明值收下（字符串也收），其余拒绝。
       少了它，`kind='banana'` 照样能溜进运行时。
    3. 集合外的 kind → **FAILED + 点名理由**，且 `steps == 0`
       （拒绝发生在任何副作用之前）。
    4. 查的是**整份计划**，不是"下一个要跑的节点" —— 一份
       `[好节点, kind='human']` 的计划，第一个节点也不许跑。
       少了它，第 3 条会漏掉"先跑了几个再失败"那条路。
    5. **快照恢复**回来的计划同样被查（`state_from_dict` 直接写
       `current_plan`，不走 reducer）。少了它，判据只挂在 `_plan()` 后面，
       一条恢复回来的 Run 会**绕过**它 —— 判据要住在所有路径的汇合处（M85）。
    6. 拒绝理由点名**哪个节点**、**声明了什么**、**支持什么**。
       少了它，运维只知道"失败了"，不知道"该去扩什么能力"。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.snapshot import RunSnapshot, state_to_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import (
    PLAN_NODE_ACTION_TYPES,
    SUPPORTED_PLAN_NODE_KINDS,
    AgentLoop,
    StepOutcome,
)
from packages.agent_runtime.reducer import RUN_FINISHED

from .helpers import new_run


def _tool_for(kind: PlanNodeKind) -> str:
    """M98：`kind='tool'` 的节点必须点名工具，其余 kind 留空。"""
    return "calculator" if kind is PlanNodeKind.TOOL else ""


# ---------------------------------------------------------------- 测试替身
class TaskOnlyPlanner:
    """一份**全是** `task` 的计划（默认 kind）—— 正常路径。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="n1", name="one"),
            ),
        )


class OneKindPlanner:
    """一个节点，`kind` 由构造参数指定。"""

    def __init__(self, *, kind: str, node_id: str = "n1") -> None:
        self.kind = kind
        self.node_id = node_id

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(
                    node_id=self.node_id,
                    name=f"node-of-kind-{self.kind}",
                    kind=self.kind,
                ),
            ),
        )


class ExecutableThenUnexecutablePlanner:
    """第一个节点**能跑**，第二个节点**这份运行时执行不了**。

    用来验"查的是整份计划"：如果只查"下一个要跑的节点"，
    第一个节点会先被执行（**副作用已经发生了**），然后才失败。

    ⚠️ M99 之后**五个 kind 全部可执行**，所以"执行不了"不再来自 kind，
    而来自**这个节点要的资源这台 worker 供不上**（M99 的
    `PLAN_RESOURCE_UNAVAILABLE`）—— 同一精神：一份整体跑不完的计划，
    第一个节点也不许跑。
    """

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="ok", name="ordinary-task"),
                PlanNode(
                    node_id="gate",
                    name="needs-a-gpu",
                    resource_labels=("gpu",),
                ),
            ),
        )


def _llm(run_id: str, n: int) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.LLM_CALL,
        payload={"prompt": f"prompt-{n}"},
    )


def _terminal_reason(loop: AgentLoop) -> str:
    """从**账本**里读 `run.finished` 的理由。

    刻意读账本而不是 `loop.last_outcome`：后者只在内存里，
    快照不带它，进程一死就蒸发 —— 运维拿到的是账本（M79 / B-12）。
    """
    for entry in reversed(loop.trace.entries):
        if entry.kind == RUN_FINISHED:
            return str(entry.payload.get("reason", ""))
    return ""


class _Base(unittest.TestCase):
    def _new(self, planner: object) -> tuple[AgentLoop, object]:
        # 刻意**在函数里** import：模块级 import 会让 unittest 把
        # `MinimalLoopTest` 也当成这个文件里的用例收集一遍。
        from tests.unit.test_agent_loop import (
            MinimalLoopTest,
            ScriptedDecisionEngine,
            ScriptedInterpreter,
        )

        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        # M99：给 worker 一份**自述的**资源能力，这样计划期的 Resource 判据
        # 才有一个"这个运行时能提供什么"的答案（`_known_resource_labels`）。
        # 只提供 `cpu`，于是计划里的 `gpu` 标签会被判成供不上。
        from packages.execution_kernel.scheduler import WorkerCapability

        base.worker.capability = WorkerCapability(
            executors=frozenset(base.worker.executors),
            labels=frozenset({"cpu"}),
        )
        loop = AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=planner,
            decision_engine=ScriptedDecisionEngine([]),
        )
        return loop, loop.start("2+3=?")

    def _script(self, loop: AgentLoop, run_id: str, n: int = 3) -> None:
        from tests.unit.test_agent_loop import ScriptedDecisionEngine

        loop.decision_engine = ScriptedDecisionEngine(
            [_llm(run_id, i + 1) for i in range(n)]
        )


# ============================================================ 1. 控制组
class APlanOfOrdinaryTasksIsUnaffectedTest(_Base):
    """控制组：全是 `task` 的计划行为**不变**。

    少了这一组，下面那些"拒绝"的用例可能只是"所有计划都坏了" ——
    它们会全绿，而它们什么都没守住。
    """

    def test_a_plan_of_ordinary_tasks_still_runs_every_node(self) -> None:
        loop, state = self._new(TaskOnlyPlanner())
        self._script(loop, state.run_id)

        loop.step()
        loop.step()

        self.assertEqual([s.plan_node_id for s in loop.steps_of_run], ["n0", "n1"])
        self.assertEqual(loop.agent_run.status, AgentRunStatus.RUNNING)

    def test_an_explicit_string_task_kind_is_still_accepted(self) -> None:
        """`kind="task"` 写全了也照收 —— 收下的是**值**，不是枚举对象。

        这条同时守住"历史调用点与快照里的字符串不会被新判据打红"。
        """
        node = PlanNode(node_id="n0", name="x", kind="task")

        self.assertIs(node.kind, PlanNodeKind.TASK)
        self.assertEqual(node.kind, "task")          # str, Enum 的好处

    def test_a_plan_with_no_nodes_still_finishes(self) -> None:
        """空计划不该被这道判据误伤。"""
        class EmptyPlanner:
            def plan(self, state):  # noqa: ANN001, ANN201
                return Plan(run_id=state.run_id, nodes=())

        loop, _ = self._new(EmptyPlanner())

        self.assertEqual(loop.step(), StepOutcome.FINISHED)


# ============================================================ 2. 闭集
class TheDeclaredSetIsAClosedSetTest(unittest.TestCase):
    """核心：`kind` 是一个闭集 —— 五个声明值收下，其余拒绝。"""

    def test_every_declared_value_is_accepted(self) -> None:
        for kind in PlanNodeKind:
            node = PlanNode(node_id="n0", name="x", kind=kind, tool=_tool_for(kind))
            self.assertIs(node.kind, kind)

    def test_every_declared_value_is_accepted_as_a_plain_string(self) -> None:
        """快照与历史调用点给的是字符串 —— 必须收。"""
        for kind in PlanNodeKind:
            node = PlanNode(
                node_id="n0", name="x", kind=kind.value, tool=_tool_for(kind)
            )
            self.assertIs(node.kind, kind)

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation) as ctx:
            PlanNode(node_id="n0", name="x", kind="banana")

        # 理由要点名**哪个节点**与**允许哪些值**（M85：拒绝理由必须点名）
        message = str(ctx.exception)
        self.assertIn("n0", message)
        self.assertIn("banana", message)
        self.assertIn("task", message)

    def test_an_empty_kind_is_refused_like_none(self) -> None:
        """空串与 `None` **同罪** —— 这是 M85 栽过的那条边界。"""
        for bad in ("", None):
            with self.subTest(kind=bad):
                with self.assertRaises(InvariantViolation):
                    PlanNode(node_id="n0", name="x", kind=bad)

    def test_a_case_mismatch_is_refused(self) -> None:
        """`"TASK"` 不是 `"task"` —— 不做大小写兜底。"""
        with self.assertRaises(InvariantViolation):
            PlanNode(node_id="n0", name="x", kind="TASK")

    def test_a_non_string_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation):
            PlanNode(node_id="n0", name="x", kind=42)

    def test_an_unknown_kind_does_not_fall_back_to_task(self) -> None:
        """★ 这条是本轮的核心：**不做"未知值兜底成 task"**。

        那正是 M88 要消灭的行为 —— 一个 Planner 把 `kind` 拼错
        （`"tsak"` 而不是 `"task"`），运行时却当 task 跑了。
        那不是宽容，是**系统替一份它没读懂的计划做了主**。

        所以：拼错必须**抛**，而不是悄悄给一个 `PlanNodeKind.TASK`。
        """
        with self.assertRaises(InvariantViolation) as ctx:
            PlanNode(node_id="n0", name="x", kind="tsak")

        # 而且是**构造就抛**，不是"造出来之后再被谁发现"
        self.assertIn("tsak", str(ctx.exception))


# ============================================================ 3. 运行时能力自述
class TheRuntimeDeclaresWhatItCanExecuteTest(_Base):
    """M99 之后，**全部五个** kind 都有真实分派路径 —— 集合与声明对齐。"""

    def test_the_runtime_declares_what_it_can_execute(self) -> None:
        """运行时必须**自述**能力 —— 否则"它做不到什么"是个秘密。"""
        self.assertEqual(SUPPORTED_PLAN_NODE_KINDS, frozenset(PlanNodeKind))
        self.assertEqual(set(PLAN_NODE_ACTION_TYPES), set(PlanNodeKind))

    def test_every_declared_kind_has_a_dispatch_path(self) -> None:
        """M99 的判据：`SUPPORTED_PLAN_NODE_KINDS` 里的每个 kind，
        都必须在 `PLAN_NODE_ACTION_TYPES` 里有非空的 Action 集合 ——
        否则它只是换了个地方说谎（声明支持、实际走到拒）。"""
        for kind in SUPPORTED_PLAN_NODE_KINDS:
            with self.subTest(kind=kind.value):
                self.assertTrue(
                    PLAN_NODE_ACTION_TYPES[kind],
                    f"{kind.value} 声明为可执行，却没有对应的 Action 集合",
                )

    def test_agent_dispatches_to_delegation(self) -> None:
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.AGENT],
            frozenset({ActionType.AGENT_DELEGATION}),
        )

    def test_decision_is_narrower_than_task(self) -> None:
        """`decision` 只允许纯决策动作（REPLAN）——放宽到 task 就失去区分力。"""
        self.assertEqual(
            PLAN_NODE_ACTION_TYPES[PlanNodeKind.DECISION],
            frozenset({ActionType.REPLAN}),
        )
        self.assertNotIn(
            ActionType.TOOL_CALL, PLAN_NODE_ACTION_TYPES[PlanNodeKind.DECISION]
        )


class AnUnknownKindIsRefusedAtConstructionTest(unittest.TestCase):
    """闭集在**构造期**兜底：未知 kind 连造都造不出来。

    M88 之前 `kind` 只被行尾注释约束（`kind='banana'` 也通过）；
    M99 之后五个声明的 kind 全部可执行，于是"运行时执行不了某个 kind"
    这条路径**自然消失** —— 剩下的唯一缺口是拼错了 kind 名，而那在
    `PlanNode.__post_init__` 就被拒绝（不需要等到计划门）。
    """

    def test_an_unknown_kind_never_becomes_a_node(self) -> None:
        with self.assertRaises(InvariantViolation) as ctx:
            PlanNode(node_id="gate", name="x", kind="banana")
        self.assertIn("banana", str(ctx.exception))
        self.assertIn("gate", str(ctx.exception))

    def test_a_misspelled_kind_does_not_fall_back_to_task(self) -> None:
        """M88 要消灭的正是"未知值兜底成 task" —— 拼错必须抛。"""
        with self.assertRaises(InvariantViolation):
            PlanNode(node_id="n0", name="x", kind="tsak")


# ============================================================ 4. 查整份计划
class TheWholePlanIsCheckedTest(_Base):
    """核心：查的是**整份计划**，不是"下一个要跑的节点"。"""

    def test_an_executable_first_node_is_not_run_when_a_later_node_is_not(self) -> None:
        """★ 一份 `[好节点, 执行不了的节点]` 的计划，**第一个节点也不许跑**。

        只查"下一个要跑的节点"的话，第一个节点会先被执行 ——
        而这份计划从一开始就不可能被完整执行，**副作用已经白发生了**。
        "宁可拒绝"的意思正是：在产生任何副作用之前拒绝。
        """
        loop, state = self._new(ExecutableThenUnexecutablePlanner())
        self._script(loop, state.run_id)

        outcome = loop.step()

        self.assertEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.steps, 0)
        self.assertEqual(
            [s.plan_node_id for s in loop.steps_of_run],
            [],
            "第一个节点是能跑的，但这份计划整体跑不完 —— 所以它也不许跑",
        )

    def test_the_reason_names_the_offending_node_not_the_first_one(self) -> None:
        """理由要点名**真正出问题的那个节点**（D-37：必须是真正的死因）。"""
        loop, state = self._new(ExecutableThenUnexecutablePlanner())
        self._script(loop, state.run_id)

        loop.step()
        reason = _terminal_reason(loop)

        self.assertIn("gate", reason)
        self.assertNotIn("ok=", reason)       # 第一个节点不该出现在理由里


# ============================================================ 5. 恢复路径
class ARestoredPlanIsCheckedTooTest(_Base):
    """核心：**快照恢复**回来的计划同样被查（M85：判据要住在所有路径的汇合处）。

    计划有两条来路：`_plan()` 落的，与**快照恢复**带回来的
    （`state_from_dict` 直接 `current_plan=plan`，不走 reducer）。
    判据只挂在 `_plan()` 后面的话，恢复回来那条会**绕过**它 ——
    然后照旧把执行不了的节点当 task 跑掉。

    M99 之后用 `PLAN_TOOL_NOT_FOUND` 作为"执行不了"的载体（五个 kind 全可执行）。
    """

    def _snapshot_with_plan(
        self, *, kind: str = "task", tool: str = "", resource_labels: tuple[str, ...] = ()
    ) -> RunSnapshot:
        run_id = new_run()
        plan = Plan(
            run_id=run_id,
            nodes=(
                PlanNode(
                    node_id="restored",
                    name="from-a-snapshot",
                    kind=kind,
                    tool=tool,
                    resource_labels=resource_labels,
                ),
            ),
        )
        state = State(
            run_id=run_id,
            goal=Goal(
                run_id=run_id,
                objective="answer",
                success_criteria=("an answer is produced",),
                budget=Budget(max_steps=5),
            ),
            current_plan=plan,
        )
        return RunSnapshot(
            run_id=run_id,
            agent_id="agent-t",
            status="RUNNING",
            state=state_to_dict(state),
            progress={},
        )

    def test_an_executable_restored_plan_still_runs(self) -> None:
        """控制组：恢复回来的是 `task` 计划 → 照常跑。"""
        loop, _ = self._new(OneKindPlanner(kind="task", node_id="restored"))
        loop.restore(self._snapshot_with_plan(kind="task"))

        from tests.unit.test_agent_loop import ScriptedDecisionEngine

        loop.decision_engine = ScriptedDecisionEngine([_llm(loop.state.run_id, 1)])

        outcome = loop.step()

        self.assertNotEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.RUNNING)

    def test_a_restored_plan_that_cannot_run_is_refused(self) -> None:
        loop, _ = self._new(OneKindPlanner(kind="task"))
        loop.restore(self._snapshot_with_plan(resource_labels=("gpu",)))

        outcome = loop.step()

        self.assertEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)
        self.assertEqual(loop.steps, 0)
        self.assertIn("restored", _terminal_reason(loop))


if __name__ == "__main__":
    unittest.main()
