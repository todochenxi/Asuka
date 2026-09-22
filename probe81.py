"""M81 探针：子 Run 失败了，父 Run 还能宣布"完成"吗？

I-11（M77）说的是：带着**没被处理过的失败**，不许宣布完成。
它的判据是 `_failures_since_last_plan()` —— 数自上次规划以来的
`EXECUTION_FAILED` observation。

而子 Run 失败走的是另一条路：

    ChildRunWaker → loop.child_failed(...)
      → _close_child_gate(outcome="failed")   ← kernel.fail()
      → _apply(Observation(kind=CHILD_RUN_FINISHED, ...))

那条 observation 的 kind 是 **CHILD_RUN_FINISHED**，不是 EXECUTION_FAILED。
于是问题来了：**I-11 数得到它吗？**

如果数不到，那么"一个委派失败了的父 Run"可以照常 FINISH → **completed**
—— 那正是 M77 治掉的那句谎言，只是从委派这扇门又进来了。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig, StepOutcome
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)

from tests.unit.test_child_run import (
    ChildRunTestBase,
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)


class AlwaysFinish:
    """用完脚本就 FINISH（与 ScriptedDecisionEngine 的默认行为一致）。"""

    def decide(self, state: State) -> Decision:
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.FINISH),
            rationale="nothing left to do",
        )


def main() -> None:
    from packages.agent_runtime.delegation import InProcessChildRunSpawner

    base = ChildRunTestBase()
    base.setUp()
    factory = base._child_stack_factory()

    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(),
        planner=ScriptedPlanner(),
        decision_engine=ScriptedDecisionEngine(
            [
                Action(
                    run_id="run_1",
                    action_type=ActionType.AGENT_DELEGATION,
                    payload={"agent_id": "researcher", "instruction": "go find out"},
                )
            ]
        ),
        config=AgentLoopConfig(max_steps=6),
        spawner=InProcessChildRunSpawner(factory=factory),
    )

    loop.start("delegate it")
    outcome = loop.step()
    print(f"step 1  → {outcome}")

    handle = loop.pending_child
    assert handle is not None, "没有在等子 Run —— 探针没走到派生分支"
    print(f"         父 Run 在等 {handle.child_run_id}")

    failed = loop.child_failed(handle.child_run_id, reason="child budget out")
    print(f"step 2  → 子 Run 失败交回：{failed}")

    print("\n--- 父 Run 的 State 上留下了什么 ---")
    for obs in loop.state.observations:
        print(f"  kind={obs.kind!r}")
    print(f"  I-11 判据 _failures_since_last_plan() = {loop._failures_since_last_plan()}")

    print("\n--- 继续推进（引擎用尽脚本后一律 FINISH）---")
    for i in range(3, 8):
        if loop.agent_run.is_terminal:
            break
        outcome = loop.step()
        print(f"step {i}  → {outcome}   status={loop.agent_run.status.value}")
        if outcome is StepOutcome.REPLANNED:
            print("          （I-11 拦下了 FINISH）")

    print(f"\n最终状态：{loop.agent_run.status.value}")
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            print(f"账本 run.finished：{entry.payload}")
            break

    verdict = "❌ 说谎：委派失败了，父 Run 却宣布完成" if loop.agent_run.status.value == "completed" else "✅ 没说谎"
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
