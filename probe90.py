"""探针 90：`ActionType` 里有一个**声明了却没人执行、也没人诚实拒绝**的成员。

背景
----
`packages/agent_domain/intelligence/action.py` 声明了 9 个 `ActionType`：

    LLM_CALL / TOOL_CALL / SKILL_CALL / AGENT_DELEGATION /
    HUMAN_APPROVAL / ASK_USER / WAIT / REPLAN / FINISH

`packages/agent_runtime/task_factory.py` 的 `ACTION_TO_TASK` 把其中 6 个映射成
真实 Task，另外 3 个映射成 `None`，注释各写了一句归宿：

    FINISH: None,   # 终态，不需要执行
    WAIT:   None,   # 等待由 Wake-up Controller 管，不是一个 Task
    REPLAN: None,   # 触发重新规划，由 Loop 自己处理

本探针把 9 个**逐个走一遍**，看运行时实际怎么对待它们 —— 而不是看注释怎么说。
"""
from __future__ import annotations

import sys
from datetime import timedelta

sys.path.insert(0, ".")

from packages.agent_domain.business.run import TERMINAL_RUN_STATUSES
from packages.agent_domain.execution.execution import SuspensionReason
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import AgentLoop
from packages.agent_runtime.task_factory import ACTION_TO_TASK
from tests.unit.test_agent_loop import (
    MinimalLoopTest,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)


class OneNodePlanner:
    """一份最简单的计划，让 step() 有节点可取。"""

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n0", name="zero"),),
        )


def _new_loop(actions):  # noqa: ANN001, ANN201
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
        [a for a in actions(state.run_id)]
    )
    return loop, state


# ===================================================================== 场景 1
def scenario_one() -> None:
    print("=" * 78)
    print("场景 1：9 个 ActionType 逐个走一遍 —— 运行时**实际**怎么对待它们")
    print("=" * 78)
    print()
    print(f"   {'ActionType':18} {'映射(TaskType/Executor)':26} {'step() 结果':24} 账本")
    print("   " + "-" * 74)

    crashed: list[str] = []      # 抛异常
    silent: list[str] = []       # 没有账本记录
    for kind in ActionType:
        mapping = ACTION_TO_TASK.get(kind)
        label = f"{mapping[0].value}/{mapping[1].value}" if mapping else "None"

        loop, state = _new_loop(
            lambda rid, k=kind: [Action(run_id=rid, action_type=k, payload={}, timeout=timedelta(seconds=30))]
        )
        try:
            outcome = loop.step()
            result = outcome.value
        except Exception as exc:                       # noqa: BLE001
            result = f"✗ {type(exc).__name__}"
            crashed.append(kind.value)

        kinds = [e.kind for e in loop.trace.entries]
        ledger = "空" if not kinds else ",".join(kinds)
        if not kinds:
            silent.append(kind.value)
        if any(k == "run.finished" for k in kinds):
            ledger += " ★终态"
        print(f"   {kind.value:18} {label:26} {result:24} {ledger}")

    print()
    # ⚠️ 结论必须从上面那些**实测值**推出来，不许写死 ——
    # 写死的话，修好之后探针会继续替旧世界说话（§0.12）。
    if crashed:
        print(f"   ⇒ 抛未捕获异常的动作：{crashed}")
    if silent:
        print(f"   ⇒ 账本**一条都没有**的动作：{silent}")
    if not crashed and not silent:
        print("   ⇒ 9 个动作**每一个都有归宿**：产生 Task / 挂起 / 由 Loop 自己处理，")
        print("     且都在账本上留下了记录。")
    print()


# ===================================================================== 场景 2
def scenario_two() -> None:
    print("=" * 78)
    print("场景 2：`wait` 到底发生了什么")
    print("=" * 78)

    loop, state = _new_loop(
        lambda rid: [Action(run_id=rid, action_type=ActionType.WAIT, payload={}, timeout=timedelta(seconds=30))]
    )
    print(f"   这条 Run 的 id  : {state.run_id}")
    raised = None
    try:
        outcome = loop.step()
        print(f"   step() 返回     : {outcome.value}")
    except Exception as exc:                           # noqa: BLE001
        raised = exc
        print(f"   step() 抛异常   : {type(exc).__name__}")
        print(f"   消息            : {exc}")

    kinds = [e.kind for e in loop.trace.entries]
    status = loop.agent_run.status
    is_terminal = status in TERMINAL_RUN_STATUSES
    print()
    print(f"   trace 条目      : {kinds}")
    print(f"   有 run.finished 吗: {'run.finished' in kinds}")
    print(f"   Run 状态        : {status.value}（终态={is_terminal}）")
    print(f"   State 的 obs    : {[o.kind for o in loop.state.observations]}")
    print()

    # ⚠️ 分支读**实测**，不读"应该会怎样"（§0.12）。
    if raised is not None:
        print("   ✗ 三个问题叠在一起：")
        print("     ① 异常没被接住（`run()` 里没有 try/except）")
        print("     ② 账本**一条都没有** —— 运维查不到'这条 Run 怎么了'")
        print(f"     ③ Run 停在 `{status.value}`，既没有终态，也没有原因")
    else:
        print("   ✔ 修好之后：**判死 + 落账本 + 点名理由**，一个都不少。")
        print("     理由里点齐了：哪个动作类型 / 声明了什么 / 支持什么 /")
        print("     为什么不能凑合 / **正确的替代路径**。")
        print()
        print("     run.finished 的理由：")
        reason = ""
        for e in loop.trace.entries:
            if e.kind == "run.finished":
                reason = str(e.payload.get("reason", ""))
        print(f"       {reason}")
    print()


# ===================================================================== 场景 3
def scenario_three() -> None:
    print("=" * 78)
    print("场景 3：`task_factory` 那句注释指向的归宿，接得上吗？")
    print("=" * 78)
    print()
    print("   注释写着：`WAIT: None,  # 等待由 Wake-up Controller 管，不是一个 Task`")
    print()
    print("   仓里确实有一个 wakeup_controller（`apps/wakeup_controller/`），")
    print("   它的输入是 `Suspension` —— 一条**挂起的 Execution**。")
    print()
    print("   而 `wait` 动作：")
    print("     * 不产生 Task          → `ACTION_TO_TASK[WAIT] is None`")
    print("     * 不产生 Execution     → 没有 Execution 就没有 Suspension")
    print("     * 在 Harness 之前就抛了 → 连挂起的机会都没有")
    print()
    # ⚠️ 不写死结论 —— 真的扫一遍仓库，看每个 SuspensionReason 有没有 producer。
    # `execution.py` 自己的 docstring 写下了这个病：
    #   "M25 之前，`CHILD_AGENT` 被冻结在这里，但**全仓库没有任何一处设置过它**。"
    #
    # producer 的判据是 AST 级的：**它被传给了 `suspend(...)` / `Suspension(...)`**。
    # 不能用文本 grep —— 注释、docstring、错误消息里的提及都不是 producer
    # （本探针第一版就是这么错的：扫到了自己刚写下的那句注释）。
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent
    producers: dict[str, list[str]] = {r.name: [] for r in SuspensionReason}
    for path in root.rglob("*.py"):
        if ".workbuddy-ai" in path.parts or ".git" in path.parts:
            continue
        if path.name in {"execution.py", "probe90.py"}:
            continue                      # 定义处与探针自己不算
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = ast.unparse(node.func).rsplit(".", 1)[-1]
            if fname not in {"suspend", "Suspension"}:
                continue                  # 只认"造出一条 Suspension"的调用
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "SuspensionReason"
                ):
                    producers[sub.attr].append(f"{rel}:{sub.lineno}")

    print("   全仓扫描（AST）：谁把 SuspensionReason **真的传给了** `suspend(...)`？")
    print()
    prod_only: dict[str, list[str]] = {}
    for reason in SuspensionReason:
        sites = producers[reason.name]
        # 生产代码 / 测试分开列 —— 混在一起会把结论糊掉：
        # "只有测试造过它"与"生产代码造过它"是两件完全不同的事。
        prod = sorted(s for s in sites if not s.startswith("tests/"))
        tests = sorted(s for s in sites if s.startswith("tests/"))
        prod_only[reason.name] = prod
        if prod:
            mark = "✔ 生产代码有 producer"
        elif tests:
            mark = "⚠️ **只有测试造过它，生产代码没有**"
        else:
            mark = "✗ **全仓没有任何 producer**"
        print(f"     {reason.value:16} {mark}")
        for s in prod[:3]:
            print(f"          [生产] {s}")
        for s in tests[:2]:
            print(f"          [测试] {s}")
    print()
    orphans = [r.value for r in SuspensionReason if not prod_only[r.name]]
    if orphans:
        print(f"   ⇒ **生产代码里没有任何 producer 的等待原因**：{orphans}")
        print("     它们被声明了、有（部分）消费者，但**没有一处会造出它们** ——")
        print("     与 `CHILD_AGENT` 在 M25 之前的状态**一模一样**。")
        print("     （`execution.py` 的 docstring 自己写下了这个病的判据。）")
        print()
    print("   ⇒ **那句注释描述的是一个不存在的连接**：")
    print("     Wake-up Controller 唤醒的是 Suspension，")
    print("     而 `wait` 动作从来没有变成过 Suspension ——")
    print("     而且它要等的那件事本身也没有 producer。")
    print("     ⇒ `wait` 执行不了，不是'少写了一个分支'，")
    print("       而是**它要等的那件事，运行时还没有产生它的能力**。")
    print()


# ===================================================================== 场景 4
def scenario_four() -> None:
    print("=" * 78)
    print("场景 4：整条 Run 会怎样（`run()` 有没有接住）")
    print("=" * 78)

    loop, _ = _new_loop(
        lambda rid: [
            Action(run_id=rid, action_type=ActionType.LLM_CALL, payload={"prompt": "p"}, timeout=timedelta(seconds=30)),
            Action(run_id=rid, action_type=ActionType.WAIT, payload={}, timeout=timedelta(seconds=30)),
        ]
    )
    raised = None
    try:
        loop.run()
        print("   run() 正常返回")
    except Exception as exc:                           # noqa: BLE001
        raised = exc
        print(f"   run() 抛异常    : {type(exc).__name__}: {exc}")
    print()
    kinds = [e.kind for e in loop.trace.entries]
    print(f"   Run 状态        : {loop.agent_run.status.value}")
    print(f"   已跑步数        : {loop.steps}")
    print(f"   trace 条目      : {kinds}")
    print()

    # ⚠️ 同样读实测。
    if raised is not None:
        print("   ✗ 一条**已经跑了几步**的 Run 会整条崩掉，")
        print("     而账本上没有一条说'它为什么停'。")
        print("     前几步真的发生过（花钱、留痕），但账本读起来像什么都没发生。")
    else:
        print("   ✔ 修好之后：`run()` 正常返回，Run 走到终态，")
        print("     账本上有 `run.finished` —— 前几步的记录还在，")
        print("     而'为什么停'也写清楚了。")
    print("=" * 78)


if __name__ == "__main__":
    scenario_one()
    scenario_two()
    scenario_three()
    scenario_four()
