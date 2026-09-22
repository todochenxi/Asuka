"""M82 探针：委派**没有回音**的父 Run，能不能宣布完成？

M81 只治了 `failed`。按 §0.4 回头查另外两扇门：

    failed     子 Run 真的失败了        → I-13 已治（必须进 State）
    cancelled  子 Run 被取消            → S-15：取消不是失败
    unknown    等到上限也没有任何结果    → D-19：不知道做没做成

`child_wait_expired` 的 docstring 明写：它也会"关闸门
（Kernel 那条 Execution 判死）"，然后**不替父 Run 写终态**，
把下一步留给 `step()`。

于是同一个问题再问一遍：**那条被判死的 Execution 有没有进 State？**
如果没进，父 Run 就能带着"这一步什么都没拿到"宣布 goal reached。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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


def main() -> None:
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
    print(f"step 1  → {loop.step()}")
    handle = loop.pending_child
    assert handle is not None

    outcome = loop.child_wait_expired(
        handle.child_run_id, reason="child run produced no result before deadline"
    )
    print(f"step 2  → 等到上限、不再等：{outcome}")

    print("\n--- 父 Run 那条委派 Execution 的真实状态 ---")
    from packages.agent_domain.execution.aggregate import ExecutionStatus

    status = loop.kernel.status_of(handle.parent_execution_id)
    print(f"  kernel.status_of = {status}")
    print(f"  是终态吗        = {status is ExecutionStatus.FAILED}")

    print("\n--- State 上留下了什么 ---")
    for obs in loop.state.observations:
        print(f"  kind={obs.kind!r}")
    print(f"  I-11 判据 _failures_since_last_plan() = {loop._failures_since_last_plan()}")

    print("\n--- 继续推进（引擎用尽脚本后一律 FINISH）---")
    for i in range(3, 8):
        if loop.agent_run.is_terminal:
            break
        o = loop.step()
        print(f"step {i}  → {o}   status={loop.agent_run.status.value}")

    print(f"\n最终状态：{loop.agent_run.status.value}")
    for entry in reversed(loop.trace.entries):
        if entry.kind == "run.finished":
            print(f"账本 run.finished：{entry.payload}")
            break

    if loop.agent_run.status.value == "completed":
        print("\n⚠️  委派没有任何回音，父 Run 却宣布了完成")
    else:
        print("\n✅ 没说谎")


if __name__ == "__main__":
    main()
