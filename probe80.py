"""M80 探针：一条死掉的子 Run，父 Run 听到的理由是对的还是编的？

链路：
    子 Run 终态
      → `_declare_terminal(status, reason=...)`           ← B-12 已经把原因写上
      → `_emit_child_run_outcome(status)`                 ← 这一步带不带原因？
      → Outbox 事件 payload["result"]
      → 父 Run 被唤醒 → `ChildRunWaker._reason(handle)`
      → 父 State 里那条 observation 的 content["error"]

最后那句是父 Agent 决定"下一步怎么办"时唯一能看到的东西。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import (
    ChildRunHandle,
    ChildRunIdentity,
    ChildRunKind,
    ChildRunRegistry,
)
from packages.agent_runtime.loop import AgentLoop, AgentLoopConfig
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryAttemptRepository,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    ManualClock,
)

from tests.unit.test_agent_loop import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
)
from tests.unit.test_terminal_reason import AlwaysToolCall, StubbornPlanner


def _handle(child_run_id: str = "child_1") -> ChildRunHandle:
    return ChildRunHandle(
        child_run_id=child_run_id,
        kind=ChildRunKind.AGENT,
        parent_run_id="run_parent",
        target="researcher",
        action=Action(run_id="run_parent", action_type=ActionType.AGENT_DELEGATION),
        parent_task_id="task_1",
        parent_execution_id="exec_1",
    )


class AlwaysReplan:
    def decide(self, state):
        from packages.agent_domain.intelligence.decision import Decision

        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=ActionType.REPLAN),
            rationale="never satisfied",
        )


def _kernel_and_worker():
    from datetime import timedelta

    from packages.agent_runtime.assembly import assemble_runtime_stack
    from packages.agent_runtime.interpreter import Interpreter
    from packages.agent_runtime.planner import Planner
    from packages.execution_kernel import Scheduler, Worker, WorkerConfig

    clock = ManualClock()
    kernel = ExecutionKernel(
        repository=InMemoryExecutionRepository(),
        attempts=InMemoryAttemptRepository(),
        outbox=InMemoryOutbox(),
        clock=clock,
    )
    from tests.unit.test_agent_loop import _tool_runtime  # noqa
    return kernel, clock


def build_child(*, planner, max_steps: int, engine) -> tuple[AgentLoop, ChildRunRegistry]:
    from datetime import timedelta

    from packages.agent_domain.intelligence.state import State
    from packages.execution_kernel import Scheduler, Worker, WorkerConfig

    from tests.unit.test_agent_loop import LoopTestBase

    base = LoopTestBase()
    base.setUp()
    registry = ChildRunRegistry()
    bound = registry.bind(_handle())

    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(max_steps=max_steps),
        planner=planner,
        decision_engine=ScriptedDecisionEngine([]),
        config=AgentLoopConfig(max_steps=max_steps),
    )
    loop.child_identity = ChildRunIdentity(bound, registry)
    loop.start("do research")
    loop.decision_engine = engine
    return loop, registry


def to_the_end(loop: AgentLoop) -> None:
    for _ in range(20):
        loop.step()
        if loop.agent_run.is_terminal:
            return


def show(tag: str, loop: AgentLoop, registry: ChildRunRegistry) -> None:
    print(f"\n=== {tag} ===")
    print("  子 Run 真正的死因（B-12 写在 trace 上）:")
    for e in reversed(loop.trace.entries):
        if e.kind == "run.finished":
            print("      ", e.payload)
            break

    events = [e for e in loop.kernel.outbox.pending(limit=50)]
    result = None
    for e in events:
        if "child_run" in str(getattr(e, "event_type", "")):
            result = dict(e.payload.get("result") or {})
            print("  事件 payload['result']:")
            print("      ", result)
            break
    if result is None:
        print("  事件 payload: （没找到 child_run 事件）")

    handle = registry.for_child("child_1")
    waker = ChildRunWaker(registry=registry, recovery=None, saga=None, driver=None)
    print("  父 Run 会听到的 reason:")
    print("      ", repr(waker._reason(handle)))


def main() -> None:
    # A：子 Run 死于预算耗尽
    loop, registry = build_child(
        planner=ScriptedPlanner(), max_steps=2, engine=AlwaysToolCall()
    )
    to_the_end(loop)
    show("A 子 Run 预算耗尽", loop, registry)

    # B：子 Run 死于换不出新计划
    loop2, registry2 = build_child(
        planner=StubbornPlanner(), max_steps=6, engine=AlwaysReplan()
    )
    to_the_end(loop2)
    show("B 子 Run 换不出新计划", loop2, registry2)


if __name__ == "__main__":
    main()
