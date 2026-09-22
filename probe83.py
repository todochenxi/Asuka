"""M83 探针：父 Run 自己把子 Run 取消掉，然后宣布完成 —— 这算说谎吗？

M82 治了 `unknown`。M82 的文档里对 `cancelled` 写了一句**判断**：

    S-15 说取消是父侧主动的选择，父 Run 自己知道，
    不构成"被隐瞒的失败"。

那句话**没有实证**。这一轮把它撞一遍。

要分清两件事：

    case A  父 Run 因为等不下去，主动取消子 Run（父侧的决定）
    case B  子 Run **被别人**取消了，父 Run 只是收到通知

A 里父 Agent 是决策者；B 里它是受害者 —— 但它听到的话一样吗？
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from packages.agent_domain.execution.aggregate import ExecutionStatus
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.delegation import InProcessChildRunSpawner
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from tests.unit.test_child_run import (
    ChildRunTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
)
from tests.unit.test_replan import VaryingPlanner

DELEGATION = Action(
    run_id="run_1",
    action_type=ActionType.AGENT_DELEGATION,
    payload={"agent_id": "researcher", "instruction": "go find out"},
)


def build() -> AgentLoop:
    base = ChildRunTestBase()
    base.setUp()
    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(),
        planner=VaryingPlanner(),
        decision_engine=ScriptedDecisionEngine([DELEGATION]),
        config=AgentLoopConfig(max_steps=6),
        spawner=InProcessChildRunSpawner(factory=base._child_stack_factory()),
    )
    loop.start("delegate it")
    assert loop.step() is StepOutcome.WAITING_CHILD
    return loop


def drive_to_end(loop: AgentLoop, *, limit: int = 8) -> None:
    for _ in range(limit):
        if loop.agent_run.is_terminal:
            return
        loop.step()


def report(tag: str, loop: AgentLoop, execution_id: str) -> None:
    print(f"\n=== {tag} ===")
    print(f"  kernel.status_of(委派 Execution) = {loop.kernel.status_of(execution_id)}")
    print("  State 上的 observation:")
    for obs in loop.state.observations:
        print(f"      {obs.kind!r}")
    print(f"  I-11 判据 = {loop._failures_since_last_plan()}")

    drive_to_end(loop)
    print(f"  最终状态 = {loop.agent_run.status.value}")
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            print(f"  账本 = {entry.payload}")
            break


def main() -> None:
    # ── case A：父 Run 主动取消（`child_cancelled` 由父侧逻辑调用）──
    a = build()
    handle_a = a.pending_child
    assert handle_a is not None
    a.child_cancelled(handle_a.child_run_id, reason="parent gave up waiting")
    report("A 父侧主动取消子 Run", a, handle_a.parent_execution_id)

    # ── case B：子 Run 被系统取消，父 Run 只是收到通知 ──
    b = build()
    handle_b = b.pending_child
    assert handle_b is not None
    b.child_cancelled(handle_b.child_run_id, reason="operator cancelled child run")
    report("B 子 Run 被别人取消，父 Run 收到通知", b, handle_b.parent_execution_id)

    print("\n--- 父 State 里那条子 Run 记录的 outcome 字段 ---")
    for tag, loop in (("A", a), ("B", b)):
        for obs in loop.state.observations:
            if obs.kind == "child_run.finished":
                print(f"  {tag}: outcome={obs.content.get('outcome')!r}  "
                      f"error={obs.content.get('error')!r}")


if __name__ == "__main__":
    main()
