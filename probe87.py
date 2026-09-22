"""探针 87：Plan 的依赖图，是**可执行的约束**还是**注释**？

本探针查两件事，它们是**同一个根因**的两张脸：

    根因：运行时按**位置**消费计划，不按**意义**消费。

  (1) `PlanNode.depends_on` 从不被读 —— 依赖图只被**校验**，从没被**执行**。
  (2) `index = len(self.steps_of_run)` 把"已经跑了多少步"当成
      "这份计划消费到第几个节点了" —— 于是**重规划之后，
      新计划的前 N 个节点被静默跳过**（N = 已经跑过的步数）。

背景
----
`depends_on` 被领域层用一次**完整拓扑排序**守着：

    Plan.__post_init__  ->  拒绝自依赖 / 拒绝未知依赖 / assert_acyclic()

它还被序列化进快照（`snapshot.py:144`），并且 `plan.py:55` 的 docstring 写着：

    "Planner 的 Plan Validator 会检查 DAG 环；这里做领域级兜底。"

—— 而**全仓没有 Plan Validator**（grep 只命中 `Execution.validate()`）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop, StepOutcome
from tests.unit.test_agent_loop import (
    MinimalLoopTest,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)


def _action(run_id: str, kind: ActionType, **payload) -> Action:  # noqa: ANN003
    return Action(run_id=run_id, action_type=kind, payload=payload)


def _llm(run_id: str, n: int) -> Action:
    return _action(run_id, ActionType.LLM_CALL, prompt=f"prompt-{n}")


def _new_loop(planner):  # noqa: ANN001, ANN201
    base = MinimalLoopTest("test_full_loop_reaches_goal")
    base.setUp()
    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(),
        planner=planner,
        decision_engine=ScriptedDecisionEngine([]),
    )
    return loop, loop.start("2+3=?")


# ===================================================================== 场景 1
class InvertedPlanner:
    """合法但**顺序颠倒**的计划：n2 声明依赖 n1，却排在 n1 前面。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="n2", name="second", depends_on=("n1",)),
                PlanNode(node_id="n1", name="first"),
            ),
        )


def scenario_one() -> None:
    print("=" * 74)
    print("场景 1：计划说『n2 必须等 n1』，运行时先跑谁？")
    print("=" * 74)

    loop, state = _new_loop(InvertedPlanner())

    plan = Plan(
        run_id=state.run_id,
        nodes=(
            PlanNode(node_id="n2", name="second", depends_on=("n1",)),
            PlanNode(node_id="n1", name="first"),
        ),
    )
    print("这份计划合法吗？")
    print("   构造通过      :", [n.node_id for n in plan.nodes])
    print("   root_nodes()  :", [n.node_id for n in plan.root_nodes()])
    print("   n2.depends_on :", plan.node("n2").depends_on, " <- 计划说：n2 等 n1")
    print()

    loop.decision_engine = ScriptedDecisionEngine(
        [_llm(state.run_id, 1), _llm(state.run_id, 2)]
    )
    for i in range(2):
        print(f"   step {i}: outcome={loop.step()}")

    ids = [s.plan_node_id for s in loop.steps_of_run]
    print()
    print("   实际实例化顺序 :", ids)
    if ids[:2] == ["n2", "n1"]:
        print("   ★ n2 在自己的依赖 n1 **之前**被执行了 —— depends_on 没被读。")
    elif ids[:2] == ["n1", "n2"]:
        print("   没有洞：按依赖图挑了 n1 先跑。")
    else:
        print(f"   意外结果：{ids}")
    print()


# ===================================================================== 场景 2
class TwoPhasePlanner:
    """第一次给 plan A（2 节点）；之后给**形状不同**的 plan B（3 节点）。

    形状必须不同，否则 I-12 会判定"这不是重规划"。
    """

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, state):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            return Plan(
                run_id=state.run_id,
                nodes=(
                    PlanNode(node_id="a0", name="alpha-0"),
                    PlanNode(node_id="a1", name="alpha-1"),
                ),
            )
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(node_id="b0", name="beta-0"),
                PlanNode(node_id="b1", name="beta-1"),
                PlanNode(node_id="b2", name="beta-2"),
            ),
        )


def scenario_two() -> None:
    print("=" * 74)
    print("场景 2：跑两步 → 重规划 → 新计划的头两个节点还在吗？")
    print("=" * 74)

    loop, state = _new_loop(TwoPhasePlanner())
    loop.decision_engine = ScriptedDecisionEngine(
        [
            _llm(state.run_id, 1),                        # → plan A, Step a0
            _llm(state.run_id, 2),                        # → Step a1
            _action(state.run_id, ActionType.REPLAN),     # → 计划作废
            _llm(state.run_id, 3),                        # → plan B, Step ?
            _llm(state.run_id, 4),                        # → Step ?
        ]
    )

    for i in range(5):
        outcome = loop.step()
        print(f"   step {i}: outcome={outcome}")

    ids = [s.plan_node_id for s in loop.steps_of_run]
    print()
    print("   plan A 的节点 : ['a0', 'a1']")
    print("   plan B 的节点 : ['b0', 'b1', 'b2']   <- 重规划换出来的新路")
    print("   实际实例化顺序 :", ids)
    print()
    if "b2" in ids and "b0" not in ids and "b1" not in ids:
        print("   ★ 新计划从 **b2** 开始 —— b0 与 b1 被静默跳过了。")
        print("     重规划换了一条路，但这条路的头两步从来没被执行。")
    elif "b0" in ids:
        print("   没有洞：新计划从 b0 开始。")
    else:
        print(f"   意外结果：{ids}")
    print("=" * 74)


class SameShapePlanner:
    """每次都返回**同一份形状**的计划 —— 重规划换不出新路。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="only"),))


def scenario_three() -> None:
    print("=" * 74)
    print("场景 3：重规划换不出新计划 -> Run 被判 FAILED 之后，还停得下来吗？")
    print("=" * 74)

    loop, state = _new_loop(SameShapePlanner())
    loop.decision_engine = ScriptedDecisionEngine(
        [_action(state.run_id, ActionType.REPLAN)]
    )

    outcomes = []
    for _ in range(8):
        outcomes.append(loop.step())

    print("   连续 8 次 step() 的结果：")
    for i, o in enumerate(outcomes):
        print(f"     {i}: {o}")
    print()
    print("   agent_run.status =", loop.agent_run.status)
    print("   steps =", loop.steps, " budget =", state.goal.budget.max_steps)

    terminal = {
        StepOutcome.FINISHED,
        StepOutcome.BUDGET_EXHAUSTED,
        StepOutcome.WAITING_APPROVAL,
        StepOutcome.WAITING_CHILD,
        StepOutcome.DENY_LOOP,
        StepOutcome.CANCELLED,
    }
    if not any(o in terminal for o in outcomes):
        print()
        print("   洞的形状：Run 早就被声明成终态（FAILED），而 `step()` 每次都返回")
        print("   FAILED、`steps` 也不再增长 —— 只看这张枚举表的话，`run()` 会永远转下去。")
        print("   没有报错，也没有尽头。（L-7 的原话：'一个永远被拒的 Run 会永远空转，")
        print("   且没有任何报错' —— FAILED 就是漏掉的那一个。）")

    # ── 修复之后：`run()` 看的是"这条 Run 还在不在"，不是"这一步叫什么" ──
    import time

    loop2, state2 = _new_loop(SameShapePlanner())
    loop2.decision_engine = ScriptedDecisionEngine(
        [_action(state2.run_id, ActionType.REPLAN)]
    )
    t0 = time.monotonic()
    loop2.run()
    elapsed = time.monotonic() - t0

    print()
    print(f"   修复后 `run()` 的返回：{loop2.agent_run.status}，耗时 {elapsed:.4f}s")
    if elapsed < 5.0:
        print("   ✓ 停下来了 —— 停止条件是「这条 Run 到了终态」，不是「这一步的结果")
        print("     属于某张枚举表」。两张表说的是两件事，不能互相代替。")
    else:
        print("   ★ 还是没停下来。")
    print("=" * 74)


if __name__ == "__main__":
    scenario_one()
    scenario_two()
    scenario_three()
