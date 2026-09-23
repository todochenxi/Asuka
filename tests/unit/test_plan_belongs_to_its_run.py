"""M89 / I-19：一份计划必须**属于**它被执行的那条 Run。

--------------------------------------------------------------------------
洞的形状（probe89.py 实证，不是推演）

`Plan.run_id` 不是一个装饰字段：

* `Plan.__post_init__` 要求它**非空**（造不出一份"没主人的计划"）；
* 它被序列化进快照；
* 恢复路径（`snapshot.py:138`）写着

      run_id=plan_raw.get("run_id") or data.get("run_id", "")

  —— 缺失时**回填这条 Run 的 id**。也就是说代码**认为**两者应当相等。

可是规划路径（`_plan()`）**从不比对**。全仓 grep `plan.run_id` 与
`state.run_id` 的比对：**为空**。

于是一份声称属于 `run_someone_else` 的计划会被**照单全收** ——
成为这条 Run 的 `current_plan`，节点被实例化成 Step 照常往下走，
而**账本上没有任何一处说"这不是这条 Run 的计划"**（probe89.py 实测）：

    已被接受的计划.run_id: run_someone_else
    state.run_id:          run_640b5d55f8cd4ce6
    两者相等吗:            False
    实际实例化的 Step:     ['n0']

一个按目标文本做缓存的 Planner、一个"计划建一次就复用"的实现，
都会正好长成这样。而"这条 Run 走的是别人的计划"是审计账本
**最不该沉默**的那类事实 —— 与 M88 同族：系统替一份它没读懂的东西做了主。

--------------------------------------------------------------------------
⚠️ 补这条不变量时，1353 条既有测试**一条都不红**。

第四次同款（M85 1287 / M87 1319 / M88 1334 / M89 1353）。
成因也一样：既有测试的替身 Planner 全部写着 `run_id=state.run_id` ——
于是"比对 run_id"与"不比对"给出同一个答案。

**一条没人能违反的判据，和没有判据，对系统来说是一样的。**

--------------------------------------------------------------------------
本文件要守住什么

    I-19：运行时必须在**执行这份计划之前**确认它属于**这条 Run**；
          不属于 → 拒绝 + 点名两个 run_id，且不许执行、不许跳过、
          **不许"再换一份计划"糊过去**（那会把真因顶替成 I-12 的
          "same shape"，D-37）。

五组一起才成立（缺任何一组，这条不变量都能被绕过）：

    1. 计划属于自己这条 Run 时行为**不变** —— 控制组。少了它，
       下面的"拒绝"可能只是"所有计划都坏了"。
    2. 外来计划 → **FAILED + 点名理由**，且 `steps == 0`
       （拒绝发生在任何副作用之前）。
    3. 拒绝是**一次就判死**，不是"重规划换一份" —— Planner 只被调用一次，
       且理由里没有 I-12 的 "same shape"。
    4. **快照恢复**回来的外来计划同样被拦（`state_from_dict` 直接写
       `current_plan`，不走 reducer）。这条最容易漏：判据只挂在
       `_plan()` 后面的话，恢复回来的 Run 会**绕过**它（M85 的通则）。
    5. I-18 与 I-19 **同时触发**时，两条缺陷都在理由里 —— 不许一条
       盖住另一条（否则运维修完 kind 才发现计划还是别人的）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.snapshot import RunSnapshot, state_to_dict
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import (
    PLAN_BELONGS_TO_ANOTHER_RUN,
    PLAN_NODE_KIND_NOT_EXECUTABLE,
    PLAN_RESOURCE_UNAVAILABLE,
    PLAN_TOOL_NOT_FOUND,
    AgentLoop,
    PlanDefect,
    StepOutcome,
    plan_defects,
)
from packages.agent_runtime.reducer import RUN_FINISHED

from .helpers import new_run


# ---------------------------------------------------------------- 测试替身
class OwnPlanPlanner:
    """一份**属于自己这条 Run** 的计划 —— 正常路径（控制组）。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="n1", name="one"),
            ),
        )


class ForeignPlanPlanner:
    """一份声称属于**别的 Run** 的计划 —— 全部节点都是普通 `task`。

    ★ 节点刻意全是 `task`（运行时**支持**的 kind）：这样一旦被拒绝，
    唯一的死因就只能是 run_id —— 断言边界划在守点上（M85 的教训：
    多道校验叠加时，验到的是"某处会拦"而不是"这一处会拦"）。
    """

    def __init__(self, *, foreign_run_id: str | None = None) -> None:
        #: 计划**实际声称**属于哪条 Run —— 断言要用它，所以必须回写。
        self.foreign_run_id = foreign_run_id
        self.calls = 0

    def plan(self, state):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.foreign_run_id is None:
            self.foreign_run_id = new_run()
        return Plan(
            run_id=self.foreign_run_id,
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="n1", name="one"),
            ),
        )


class ForeignAndUnexecutablePlanner:
    """两个缺陷**同时**成立：计划是别人的，且有个节点运行时跑不了。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=new_run(),
            nodes=(
                PlanNode(node_id="n0", name="zero"),
                PlanNode(node_id="gate", name="needs-a-gpu", resource_labels=("gpu",)),
            ),
        )


def _llm(run_id: str, n: int) -> Action:
    return Action(
        run_id=run_id,
        action_type=ActionType.LLM_CALL,
        payload={"prompt": f"prompt-{n}"},
    )


def _terminal_reason(loop: AgentLoop) -> str:
    """从**账本**里读 `run.finished` 的理由（B-12 / D-37：运维拿到的是账本）。"""
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
        # M99：给 worker 一份**自述的**资源能力，计划期的 Resource 判据才有答案。
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

    def _snapshot(self, *, plan_run_id: str, snapshot_run_id: str) -> RunSnapshot:
        """一张快照：`run_id` 是 `snapshot_run_id`，而里面的计划声称属于
        `plan_run_id` —— 两者可以不同，这正是恢复路径上最危险的那种输入。"""
        plan = Plan(
            run_id=plan_run_id,
            nodes=(PlanNode(node_id="restored", name="from-a-snapshot"),),
        )
        state = State(
            run_id=snapshot_run_id,
            goal=Goal(
                run_id=snapshot_run_id,
                objective="answer",
                success_criteria=("an answer is produced",),
                budget=Budget(max_steps=5),
            ),
            current_plan=plan,
        )
        return RunSnapshot(
            run_id=snapshot_run_id,
            agent_id="agent-t",
            status="RUNNING",
            state=state_to_dict(state),
            progress={},
        )


# ============================================================ 1. 控制组
class APlanThatBelongsToItsRunStillRunsTest(_Base):
    """控制组：计划属于自己这条 Run → 行为**不变**。

    少了这一组，下面的"拒绝"可能只是"所有计划都坏了" —— 它们会全绿，
    而它们什么都没守住。
    """

    def test_a_plan_that_names_its_own_run_still_runs_every_node(self) -> None:
        loop, state = self._new(OwnPlanPlanner())
        self._script(loop, state.run_id)

        loop.step()
        loop.step()

        self.assertEqual([s.plan_node_id for s in loop.steps_of_run], ["n0", "n1"])
        self.assertEqual(loop.agent_run.status, AgentRunStatus.RUNNING)

    def test_a_matching_plan_has_no_defects(self) -> None:
        """纯函数层：归属对得上 → 缺陷列表**空**。"""
        run_id = new_run()
        plan = Plan(run_id=run_id, nodes=(PlanNode(node_id="n0", name="x"),))

        self.assertEqual(plan_defects(plan, run_id=run_id), [])

    def test_the_run_id_field_is_not_a_decorative_default(self) -> None:
        """`Plan.run_id` 真的被读 —— 换个 run_id 就出缺陷。

        这条是"控制组"的另一半：如果 `plan_defects()` 无论传什么都返回空，
        上面那条也会绿，而它什么都没证明。
        """
        plan = Plan(run_id=new_run(), nodes=(PlanNode(node_id="n0", name="x"),))

        self.assertNotEqual(plan_defects(plan, run_id=new_run()), [])


# ============================================================ 2. 外来计划
class APlanFromAnotherRunIsRefusedTest(_Base):
    """核心：不属于这条 Run 的计划 → 判死 + 点名两个 run_id + **零副作用**。"""

    def test_a_plan_that_belongs_to_another_run_is_refused(self) -> None:
        planner = ForeignPlanPlanner()
        loop, state = self._new(planner)
        self._script(loop, state.run_id)

        outcome = loop.step()

        self.assertEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)

    def test_the_refusal_happens_before_any_side_effect(self) -> None:
        """★ "宁可拒绝"的意思就是这一条：**在产生任何副作用之前**拒绝。"""
        loop, state = self._new(ForeignPlanPlanner())
        self._script(loop, state.run_id)

        loop.step()

        self.assertEqual(loop.steps, 0)
        self.assertEqual(
            [s.plan_node_id for s in loop.steps_of_run],
            [],
            "既没有执行它，也没有为它开一个 Step",
        )

    def test_the_refusal_names_the_code_and_both_run_ids(self) -> None:
        """理由要点齐：机器可读的**码** + 两个 run_id 的**点名**（PR-19）。"""
        planner = ForeignPlanPlanner()
        loop, state = self._new(planner)
        self._script(loop, state.run_id)

        loop.step()
        reason = _terminal_reason(loop)

        self.assertIn(PLAN_BELONGS_TO_ANOTHER_RUN, reason)   # 机器可读的类别
        self.assertIn(planner.foreign_run_id, reason)        # 计划声称属于谁
        self.assertIn(state.run_id, reason)                  # 实际这条 Run 是谁
        self.assertIn("I-19", reason)                        # 判据的编号

    def test_the_foreign_plan_is_the_only_defect(self) -> None:
        """★ 断言隔离：这份计划的节点**全是**运行时支持的 `task`。

        所以理由里**不许**出现 `PLAN_NODE_KIND_NOT_EXECUTABLE` ——
        否则这条测试验的是"某处会拦"，而不是"这一处会拦"（M85 栽过）。
        """
        loop, state = self._new(ForeignPlanPlanner())
        self._script(loop, state.run_id)

        loop.step()
        reason = _terminal_reason(loop)

        self.assertIn(PLAN_BELONGS_TO_ANOTHER_RUN, reason)
        self.assertNotIn(PLAN_NODE_KIND_NOT_EXECUTABLE, reason)

    def test_the_run_is_not_replanned_around_the_foreign_plan(self) -> None:
        """★ 不许"再换一份计划"糊过去。

        计划本身没错，错的是**这份计划不是这条 Run 的**。让 Planner
        "再换一份"等于告诉它"你的计划有问题"，那是一句假话；而且最终
        FAILED 的理由会被 I-12 那句 "same shape" 冲淡 ——
        **真正的死因被顶替了**（D-37）。
        """
        planner = ForeignPlanPlanner()
        loop, state = self._new(planner)
        self._script(loop, state.run_id)

        loop.step()

        self.assertEqual(planner.calls, 1, "外来计划不该触发第二次规划")
        self.assertNotIn("same shape", _terminal_reason(loop))


# ============================================================ 3. 纯函数层
class PlanDefectsIsTheOneGateTest(unittest.TestCase):
    """`plan_defects()` 是这道门的**唯一实现** —— 直接测它，不隔着 Loop。"""

    def test_a_foreign_plan_yields_exactly_one_defect_with_the_code(self) -> None:
        plan = Plan(run_id=new_run(), nodes=(PlanNode(node_id="n0", name="x"),))
        defects = plan_defects(plan, run_id=new_run())

        self.assertEqual(len(defects), 1)
        self.assertIsInstance(defects[0], PlanDefect)
        self.assertEqual(defects[0].code, PLAN_BELONGS_TO_ANOTHER_RUN)

    def test_the_defect_carries_a_machine_readable_code_and_a_named_detail(self) -> None:
        """刻意分成 `code` + `detail` 两半：调用方按 code 分支，人读 detail。"""
        foreign = new_run()
        mine = new_run()
        plan = Plan(run_id=foreign, nodes=(PlanNode(node_id="n0", name="x"),))

        defect = plan_defects(plan, run_id=mine)[0]

        self.assertEqual(defect.code, PLAN_BELONGS_TO_ANOTHER_RUN)
        self.assertIn(foreign, defect.detail)
        self.assertIn(mine, defect.detail)

    def test_both_defects_are_reported_together(self) -> None:
        """★ I-18 与 I-19 同时成立时，**两条都要报**。

        报一条就返回的话，运维修完 kind 才发现"计划还是别人的" ——
        两次失败，两次都只说了一半。
        """
        plan = Plan(
            run_id=new_run(),
            nodes=(
                PlanNode(node_id="ok", name="ordinary"),
                PlanNode(
                    node_id="gate",
                    name="needs-a-missing-tool",
                    kind="tool",
                    tool="tool-this-runtime-lacks",
                ),
            ),
        )
        codes = [
            d.code
            for d in plan_defects(
                plan, run_id=new_run(), known_tools=frozenset({"some-tool"})
            )
        ]

        self.assertIn(PLAN_BELONGS_TO_ANOTHER_RUN, codes)
        self.assertIn(PLAN_TOOL_NOT_FOUND, codes)

    def test_a_matching_plan_with_an_unexecutable_node_reports_only_that(self) -> None:
        """反向隔离：归属对得上时，理由里**不许**出现归属那条码。

        M99：五个 kind 全部可执行，"执行不了"改由 `PLAN_TOOL_NOT_FOUND` 承载。
        """
        run_id = new_run()
        plan = Plan(
            run_id=run_id,
            nodes=(
                PlanNode(
                    node_id="gate",
                    name="needs-a-missing-tool",
                    kind="tool",
                    tool="tool-this-runtime-lacks",
                ),
            ),
        )
        codes = [
            d.code
            for d in plan_defects(plan, run_id=run_id, known_tools=frozenset({"some-tool"}))
        ]

        self.assertEqual(codes, [PLAN_TOOL_NOT_FOUND])


# ============================================================ 4. 恢复路径
class ARestoredForeignPlanIsRefusedTooTest(_Base):
    """核心：**快照恢复**回来的外来计划同样被拦（M85：判据住在所有路径的汇合处）。

    计划有两条来路：`_plan()` 落的，与**快照恢复**带回来的
    （`state_from_dict` 直接 `current_plan=plan`，不走 reducer）。
    判据只挂在 `_plan()` 后面的话，恢复回来那条会**绕过**它。
    """

    def test_a_restored_plan_that_belongs_to_this_run_still_runs(self) -> None:
        """控制组：恢复回来、且归属对得上 → 照常跑。"""
        loop, _ = self._new(OwnPlanPlanner())
        snapshot_run = new_run()
        loop.restore(self._snapshot(plan_run_id=snapshot_run, snapshot_run_id=snapshot_run))

        from tests.unit.test_agent_loop import ScriptedDecisionEngine

        loop.decision_engine = ScriptedDecisionEngine([_llm(snapshot_run, 1)])

        outcome = loop.step()

        self.assertNotEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.RUNNING)

    def test_a_restored_plan_from_another_run_is_refused(self) -> None:
        """★ 这条最容易漏：`_plan()` 根本没被调用（计划是恢复带回来的），
        这道门仍然要拦得住。"""
        foreign = new_run()
        snapshot_run = new_run()
        loop, _ = self._new(OwnPlanPlanner())
        loop.restore(self._snapshot(plan_run_id=foreign, snapshot_run_id=snapshot_run))

        outcome = loop.step()

        self.assertEqual(outcome, StepOutcome.FAILED)
        self.assertEqual(loop.agent_run.status, AgentRunStatus.FAILED)
        self.assertEqual(loop.steps, 0)

        reason = _terminal_reason(loop)
        self.assertIn(PLAN_BELONGS_TO_ANOTHER_RUN, reason)
        self.assertIn(foreign, reason)
        self.assertIn(snapshot_run, reason)

    def test_a_restored_plan_with_a_missing_run_id_is_backfilled_not_refused(self) -> None:
        """★ 反向边界：快照里的计划**没写** `run_id` 时，恢复路径**回填**
        这条 Run 的 id（`snapshot.py:138`）—— 那是**合法**的，
        不是"外来计划"，不许被这道门误伤。
        """
        snapshot_run = new_run()
        raw = {
            "run_id": snapshot_run,
            "goal": {
                "run_id": snapshot_run,
                "objective": "answer",
                "success_criteria": ["an answer is produced"],
                "budget": {"max_steps": 5},
            },
            # 计划**故意不带** run_id —— 老快照的形状
            "current_plan": {
                "plan_id": "p-1",
                "nodes": [{"node_id": "restored", "name": "from-a-snapshot"}],
            },
        }
        snapshot = RunSnapshot(
            run_id=snapshot_run,
            agent_id="agent-t",
            status="RUNNING",
            state=raw,
            progress={},
        )

        loop, _ = self._new(OwnPlanPlanner())
        loop.restore(snapshot)

        self.assertEqual(loop.state.current_plan.run_id, snapshot_run)
        self.assertEqual(plan_defects(loop.state.current_plan, run_id=snapshot_run), [])


# ============================================================ 5. 两条缺陷同时在账本
class TheTwoDefectsDoNotHideEachOtherTest(_Base):
    """核心：两条缺陷同时成立时，**账本上的理由里两条都在**。"""

    def test_the_ledger_reason_carries_both_codes(self) -> None:
        loop, state = self._new(ForeignAndUnexecutablePlanner())
        self._script(loop, state.run_id)

        loop.step()
        reason = _terminal_reason(loop)

        self.assertIn(PLAN_BELONGS_TO_ANOTHER_RUN, reason)
        self.assertIn(PLAN_RESOURCE_UNAVAILABLE, reason)
        self.assertIn("gate", reason)          # 哪个节点跑不了
        self.assertEqual(loop.steps, 0)


if __name__ == "__main__":
    unittest.main()
