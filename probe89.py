"""探针 89：§32 的 `Plan Validator` —— 一个不存在的阶段，和它该拦的那些计划。

背景
----
基线 §32 画了这条流水线：

    Goal -> Planner -> Plan -> **Plan Validator** -> Action Selector -> Task

并列出 Validator 要检查的七项：

    DAG Cycle / Tool Exists / Permission / Dependency / Resource / Budget / Risk

M87 已实证「全仓没有 Plan Validator」。M88 的 §125.11 把它登记成空洞 248。
本探针把七项**逐项**查一遍（谁做了、在哪一层做、什么时候做），
并单独撞一条**从来没被检查过**的关系：

    `Plan.run_id`  vs  `State.run_id`

—— 计划声称它属于哪条 Run，与它实际被应用到哪条 Run。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from packages.agent_domain.business import AgentRunStatus
from packages.agent_domain.business.snapshot import state_to_dict
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from tests.unit.test_agent_loop import (
    MinimalLoopTest,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)


def _llm(run_id: str, n: int) -> Action:
    return Action(
        run_id=run_id, action_type=ActionType.LLM_CALL, payload={"prompt": f"p{n}"}
    )


def _new_loop(planner, script=None):  # noqa: ANN001, ANN201
    base = MinimalLoopTest("test_full_loop_reaches_goal")
    base.setUp()
    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(),
        planner=planner,
        decision_engine=ScriptedDecisionEngine(list(script or [])),
    )
    return loop, loop.start("2+3=?")


def _terminal_reason(loop) -> str:  # noqa: ANN001
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            return str(entry.payload.get("reason", ""))
    return "<账本里没有 run.finished>"


# ===================================================================== 场景 1
class ForeignPlanPlanner:
    """返回一份**属于另一条 Run** 的计划。

    这不是编出来的场景：一个按目标文本做缓存的 Planner（或一个把计划
    建一次就复用的实现）会正好长成这样。而 `Plan.run_id` 是被
    `Plan.__post_init__` 要求非空的 —— 它是一个**有意义**的字段。
    """

    def __init__(self, *, foreign_run_id: str) -> None:
        self.foreign_run_id = foreign_run_id

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=self.foreign_run_id,          # ← 不是这条 Run
            nodes=(
                PlanNode(node_id="n0", name="from-another-run"),
                PlanNode(node_id="n1", name="also-from-another-run"),
            ),
        )


def scenario_one() -> None:
    print("=" * 74)
    print("场景 1：Planner 交回一份**属于别的 Run** 的计划 —— 谁吭声？")
    print("=" * 74)

    foreign = "run_someone_else"
    loop, state = _new_loop(ForeignPlanPlanner(foreign_run_id=foreign))
    loop.decision_engine = ScriptedDecisionEngine([_llm(state.run_id, 1)])

    print(f"   这条 Run 的 id            : {state.run_id}")
    print(f"   Planner 交回的计划声称属于 : {foreign}")
    print()

    outcome = loop.step()

    plan = loop.state.current_plan
    print(f"   step() 结果              : {outcome.value}")
    print(f"   已被接受的计划.run_id      : {plan.run_id if plan else None}")
    print(f"   state.run_id             : {loop.state.run_id}")
    print(f"   两者相等吗                : {plan is not None and plan.run_id == loop.state.run_id}")
    print()
    ran = [s.plan_node_id for s in loop.steps_of_run]
    print("   实际实例化的 Step        :", ran)
    print()

    # ⚠️ 分支条件要读**它有没有真的跑起来**，不是读"计划是不是别人的"。
    # 只读后者的话，M89 修好之后这段仍然会打印"照单全收" ——
    # 探针自己开始说一句不真的话（与它要揭露的那类病同款）。
    foreign_plan = plan is not None and plan.run_id != loop.state.run_id
    if foreign_plan and ran:
        print("   ★ 洞：计划声称它属于**另一条 Run**，运行时照单全收：")
        print("     它成了这条 Run 的 `current_plan`，节点被实例化成 Step，照常往下走。")
        print("     账本上没有任何一处说'这份计划不是这条 Run 的'。")
        print()
        print("   为什么这是个洞：")
        print("     * `Plan.run_id` 不是装饰 —— `__post_init__` 要求它非空，")
        print("       快照也序列化它，而恢复路径写着：")
        print('         run_id=plan_raw.get("run_id") or data.get("run_id", "")')
        print("       —— 缺失时**回填这条 Run 的 id**。也就是说代码**认为**两者应当相等。")
        print("     * 规划路径（`_plan()`）**从不比对**。")
    elif foreign_plan:
        print("   ✔ 洞已被 M89 堵上：计划仍被记进 `current_plan`（诚实留痕），")
        print("     但**一个 Step 都没有实例化**，Run 判死并点名了两个 run_id：")
        print()
        print("      ", _terminal_reason(loop))
    else:
        print("   没有洞：不属于这条 Run 的计划被拦住了。")
    print()


# ===================================================================== 场景 2
def scenario_two() -> None:
    print("=" * 74)
    print("场景 2：账本上看得出来吗？")
    print("=" * 74)

    loop, state = _new_loop(ForeignPlanPlanner(foreign_run_id="run_someone_else"))
    loop.decision_engine = ScriptedDecisionEngine([_llm(state.run_id, 1)])
    loop.step()

    print("   run_id =", state.run_id)
    print("   trace 全部条目：")
    for e in loop.trace.entries:
        print(f"     {e.kind:24} payload.run_id={e.payload.get('run_id')!r}")
    print()
    reason = _terminal_reason(loop)
    print("   run.finished 的理由：")
    print("     ", reason)
    print()

    # 同样地：分支要读**账本里到底写了什么**，不是读"应该会怎样"。
    if "run_someone_else" in reason and state.run_id in reason:
        print("   ✔ M89 之后：账本**自己说出**了这条 Run 走的是别人的计划 ——")
        print("     两个 run_id 都在理由里（机器可读的码 + 点名），运维照着就能定位。")
    else:
        print("   账本读起来完全正常 —— 没有任何字段说'计划的 run_id 与 Run 不一致'。")
        print("   运维拿到账本，看不出这条 Run 走的是**别人的计划**。")
    print()


# ===================================================================== 场景 3
def scenario_three() -> None:
    print("=" * 74)
    print("场景 3：§32 列的七项检查，逐项查一遍")
    print("=" * 74)

    rows = [
        ("DAG Cycle", "✅ 有人做", "`Plan.assert_acyclic()`（领域层，构造时）"),
        ("Dependency", "✅ 有人做", "`_next_plan_node()`（M87 / I-16，运行时取节点时）"),
        ("Tool Exists", "❌ 无机制", "`PlanNode` **没有** `tool` 字段 —— 这项连表达都表达不了"),
        ("Permission", "⚠️ 换了时机", "Harness `before_action()`（**每个动作执行前**，不是计划期）"),
        ("Risk", "⚠️ 换了时机", "Harness `RiskLevel` 策略（同上，逐步）"),
        ("Budget", "⚠️ 换了时机", "Loop 的 `steps >= budget`（**执行时**拦，不是计划期）"),
        ("Resource", "❌ 无机制", "全仓没有任何 Resource 概念"),
    ]
    for name, status, where in rows:
        print(f"   {name:12} {status:10} {where}")
    print()
    print("   统计：2 项真的做了；3 项**换了时机**（执行期逐步，不是计划期一次性）；")
    print("         2 项**连表达都表达不了**。")
    print()
    print("   ⇒ 差的不是'七项里少做了几项'，是**一道门的位置**：")
    print("     §32 说计划**在执行之前**被审过一遍；实际是**每一步执行时**被审。")
    print("     这是两种不同的保证 —— 后者允许'先跑了三个节点才发现这条路走不通'。")
    print()


# ===================================================================== 场景 4
def scenario_four() -> None:
    print("=" * 74)
    print("场景 4：`plan.constraints` 有人执行吗？")
    print("=" * 74)

    plan = Plan(
        run_id="run-1",
        nodes=(PlanNode(node_id="n0", name="x"),),
        constraints=("不得调用外部网络", "预算不超过 1 美元"),
    )
    print("   计划声明 constraints =", plan.constraints)
    print("   构造通过 ✓（`constraints` 是自由字符串，没有任何校验）")
    print()
    print("   运行时读它吗？—— `_plan_shape()` 读，但只是为了判断'两份计划是不是同一条路'（I-12）。")
    print("   **没有任何一处拿它当约束执行。**")
    print()
    print("   ⇒ 与 `kind`（M88）、`expected_output`（空洞 244）同族：")
    print("     声明了、持久化了、**从不被执行**。")
    print("     处置：**登记不治** —— 它是自由文本，'不得调用外部网络'要变成判据，")
    print("     需要一套策略语言（那是 Policy / Harness 的活，不是 Plan 的）。")
    print("=" * 74)


if __name__ == "__main__":
    scenario_one()
    scenario_two()
    scenario_three()
    scenario_four()
