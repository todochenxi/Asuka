"""探针 88：`PlanNode.kind` 是**约束**，还是**写在注释里的愿望**？

    根因：`kind` 声明了一种区分（五种工作类型），
          但**没有任何一层**为这个声明负责 —— 它不校验、不路由。

  (1) 领域层不校验 —— `kind="banana"` / `""` / `"TASK"` 全部静默通过。
  (2) 运行时不分派 —— `kind="human"`（需要人签字）的节点被当普通 task 跑完，
      Run 报告 COMPLETED，**没有任何人签过字**。
  (3) 运行时不分派 —— `kind="agent"`（需要委派）的节点在本地跑完，
      **没有子 Run 发生过**。
  (4) `expected_output` 能序列化、能还原，但**没有任何一处读它做判断**。

背景
----
`plan.py` 那一行是全部真相：

    kind: str = "task"                      # task / tool / agent / human / decision

* 类型是 `str`，五个值只活在**行尾注释**里；
* `PlanNode.__post_init__` 只查 `node_id` / `name` 非空与自依赖，
  **完全不看 `kind`**；
* 运行时 `_ensure_step()` 只读 `node.node_id` 与 `node.name`，
  `node.kind` 只被 `_plan_shape()`（I-12 的形状签名）顺带带上。

对比：同一个项目里 `ObservationSource` / `ChildRunKind` / `RiskLevel` /
`ActionType` 全是 `str, Enum` + `__post_init__` 校验。
`PlanNode.kind` 是**唯一的例外**。

架构文档 §32 则写着：

    Goal -> Planner -> Plan -> Plan Validator -> Action Selector -> Task
    Validator 检查：DAG Cycle / Tool Exists / Permission /
                    Dependency / Resource / Budget / Risk

M87 已实证「全仓没有 Plan Validator」（`grep -rn "Plan Validator"` 只命中
这句文档与 `plan.py` 的 docstring）。M87 修掉了七项检查里的「Dependency」。
M88 修的是另一个问题：**`kind` 这个声明，谁来负责？**

修复（I-18）
-----------
* 领域层：`kind` 变成 `PlanNodeKind`（`str, Enum`），`__post_init__` 拒绝未知值；
* 运行时：`SUPPORTED_PLAN_NODE_KINDS` 声明它**真的能执行**的集合（现在只有
  `task`），计划里出现集合外的 kind → **在产生任何副作用之前**判死，理由点名；
* `expected_output`：**登记不治**（它是给人看的注释，拿它做判据需要另一个裁判）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode, PlanNodeKind
from packages.agent_runtime.loop import (
    SUPPORTED_PLAN_NODE_KINDS,
    AgentLoop,
    StepOutcome,
)
from tests.unit.test_agent_loop import (
    MinimalLoopTest,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)


def _action(run_id: str, kind: ActionType, **payload) -> Action:  # noqa: ANN003
    return Action(run_id=run_id, action_type=kind, payload=payload)


def _llm(run_id: str, n: int) -> Action:
    return _action(run_id, ActionType.LLM_CALL, prompt=f"prompt-{n}")


class OneNodePlanner:
    """一份只有一个节点的计划，节点的 `kind` 由构造参数指定。"""

    def __init__(self, *, node_kind: str, node_id: str = "n1") -> None:
        self.node_kind = node_kind
        self.node_id = node_id

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(
                PlanNode(
                    node_id=self.node_id,
                    name=f"node-of-kind-{self.node_kind}",
                    kind=self.node_kind,
                ),
            ),
        )


def _new_loop(planner):  # noqa: ANN001, ANN201
    """造一条 Run。

    ⚠️ 决策脚本必须在**第一次 step() 之前**装好：`ScriptedDecisionEngine([])`
    会让第一次 step() 直接 FINISHED，Run 就此终态，后面再 step() 会撞 B-3。
    """
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


def _finished_reason(loop) -> str:  # noqa: ANN001
    """从账本里读 `run.finished` 的 payload —— 运维读的是这个，不是内存。"""
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            return str(entry.payload.get("reason", "<没有记录到原因>"))
    return "<账本里没有 run.finished>"


# ===================================================================== 场景 1
def scenario_one() -> None:
    print("=" * 74)
    print("场景 1：`kind` 写错了，谁会吭声？")
    print("=" * 74)

    declared = [k.value for k in PlanNodeKind]
    print(f"   声明的五个值（现在是 `PlanNodeKind`）: {declared}")
    print()

    print("   领域层对以下取值分别怎么反应：")
    for value in (*declared, "banana", "", "TASK", None, 42):
        try:
            plan = Plan(
                run_id="run-1",
                nodes=(PlanNode(node_id="n1", name="x", kind=value),),
            )
            got = plan.node("n1").kind
            print(f"     kind={value!r:12} -> 构造通过，kind={got!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"     kind={value!r:12} -> 拒绝：{type(exc).__name__}")

    print()
    print("   ✓ 五个声明值全部通过；`'banana'` / `''` / `'TASK'` / `None` / `42`")
    print("     全部被拒 —— 空串与 None **同罪**（M85 的边界）。")
    print("   ⇒ 「只有这五种」现在写在**类型**里，不是写在注释里。")
    print()


# ===================================================================== 场景 2
def scenario_two() -> None:
    print("=" * 74)
    print("场景 2：节点声明 `kind='human'`（这一步要人签字）—— 谁签的？")
    print("=" * 74)

    loop, state = _new_loop(OneNodePlanner(node_kind="human"))
    loop.decision_engine = ScriptedDecisionEngine([_llm(state.run_id, 1)])

    first = loop.step()
    node = loop.state.current_plan.node("n1")
    print(f"   计划声明 : node_id={node.node_id!r}  kind={node.kind.value!r}")
    print("   计划的意思：这一步必须由**人**放行（HITL 门）。")
    print()

    outcomes = [first]
    for _ in range(6):
        if loop.agent_run.is_terminal:
            break
        outcomes.append(loop.step())

    approvals = [o for o in loop.state.observations if o.kind == "approval.requested"]

    print("   step() 序列 :", [o.value for o in outcomes])
    print("   Run 终态     :", loop.agent_run.status.value)
    print("   步数         :", loop.steps, "  <- ★ 0：拒绝发生在**任何副作用之前**")
    print(f"   approval.requested 条数 : {len(approvals)}")
    print()
    print("   账本里 run.finished 的理由：")
    print("     " + _finished_reason(loop))
    print()
    if loop.agent_run.status.value == "failed" and loop.steps == 0:
        print("   ✓ 在跑第一步之前就拒绝了 —— 不许执行它（那是编造『我做到了』），")
        print("     也不许跳过它（那是编造『它做过了』）。")
        print("     理由点名了**哪个节点**、**声明了什么**、**运行时支持什么**。")
    else:
        print("   ★ 计划说『这一步要人签字』，运行时把它当普通 task 跑完了。")
    print()


# ===================================================================== 场景 3
def scenario_three() -> None:
    print("=" * 74)
    print("场景 3：节点声明 `kind='agent'`（这一步要委派给子 Run）—— 子 Run 呢？")
    print("=" * 74)

    loop, state = _new_loop(OneNodePlanner(node_kind="agent"))
    loop.decision_engine = ScriptedDecisionEngine([_llm(state.run_id, 1)])

    first = loop.step()
    node = loop.state.current_plan.node("n1")
    print(f"   计划声明 : node_id={node.node_id!r}  kind={node.kind.value!r}")
    print("   计划的意思：这一步应该**派生一个子 Run** 去做。")
    print()

    for _ in range(6):
        if loop.agent_run.is_terminal:
            break
        loop.step()

    registry = getattr(loop, "child_runs", None)
    children = []
    if registry is not None and hasattr(registry, "for_parent"):
        children = list(registry.for_parent(state.run_id))

    print("   step() 首步        :", first.value)
    print("   Run 终态            :", loop.agent_run.status.value)
    print(f"   子 Run 条数          : {len(children)}")
    print()
    print("   账本里 run.finished 的理由：")
    print("     " + _finished_reason(loop))
    print()
    if loop.agent_run.status.value == "failed":
        print("   ✓ 拒绝了 —— 一个**声明要委派**的节点，在本地跑完并报 COMPLETED，")
        print("     等于宣布『子 Run 做完了』，而一个子 Run 都没有发生过。")
    else:
        print("   ★ 委派从未发生，Run 却报 COMPLETED。")
    print()


# ===================================================================== 场景 4
def scenario_four() -> None:
    print("=" * 74)
    print("场景 4：运行时支持的 kind 集合，以及『能力』与『声明』的差")
    print("=" * 74)

    supported = sorted(k.value for k in SUPPORTED_PLAN_NODE_KINDS)
    declared = sorted(k.value for k in PlanNodeKind)
    print(f"   `PlanNodeKind` 声明的       : {declared}")
    print(f"   `SUPPORTED_PLAN_NODE_KINDS` : {supported}")
    print()
    print("   差集 =", sorted(set(declared) - set(supported)))
    print("   ⇒ 这四个 kind 声明得出、但运行时**做不到**。")
    print("     现在它们会被**点名拒绝**，而不是静默当 task 跑掉。")
    print("     M12 落地真正的按 kind 分派时，集合扩大，拒绝自动消失。")
    print()


# ===================================================================== 场景 5
def scenario_five() -> None:
    print("=" * 74)
    print("场景 5：`expected_output` 有人读吗？")
    print("=" * 74)

    plan = Plan(
        run_id="run-1",
        nodes=(PlanNode(node_id="n1", name="compute", expected_output="一个整数"),),
    )
    print("   计划声明 : expected_output='一个整数'")
    print("   构造通过 :", plan.node("n1").expected_output)

    from packages.agent_domain.business.snapshot import plain

    raw = plain(plan)
    print("   序列化后 :", raw["nodes"][0])
    print()
    print("   它能往返（`_plain` 是通用序列化器，`snapshot.py` 显式还原它）——")
    print("   但**没有任何一处读它做判断**。")
    print("   ⇒ 处置：**登记不治**（空洞 244）。它是自由文本（'一个整数'），")
    print("     拿它做判据需要另一个裁判模型 —— 那是 Evaluation 的活。")
    print("     把定位写清楚，比假装它有约束力要诚实。")
    print("=" * 74)


if __name__ == "__main__":
    scenario_one()
    scenario_two()
    scenario_three()
    scenario_four()
    scenario_five()
