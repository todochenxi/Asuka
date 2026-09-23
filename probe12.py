"""M12 探针：验证 PlanNode.kind 与 Action 的真实分派边界。"""
from __future__ import annotations

from datetime import timedelta

from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_runtime.loop import (
    PLAN_NODE_ACTION_TYPES,
    AgentLoop,
    StepOutcome,
    plan_defects,
)


def _loop(kind: str, action_type: ActionType, **kwargs):
    from tests.unit.test_agent_loop import (
        MinimalLoopTest,
        ScriptedDecisionEngine,
        ScriptedInterpreter,
    )

    base = MinimalLoopTest("test_full_loop_reaches_goal")
    base.setUp()
    loop = AgentLoop(
        kernel=base.kernel,
        worker=base.worker,
        interpreter=ScriptedInterpreter(),
        planner=_Planner(kind),
        decision_engine=ScriptedDecisionEngine([]),
    )
    state = loop.start("2+3=?")
    loop.decision_engine = ScriptedDecisionEngine(
        [
            Action(
                run_id=state.run_id,
                action_type=action_type,
                payload=kwargs.pop("payload", {}),
                **kwargs,
            )
        ]
    )
    return loop


class _Planner:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def plan(self, state):  # noqa: ANN001, ANN201
        return Plan(
            run_id=state.run_id,
            nodes=(PlanNode(node_id="n0", name=f"{self.kind}-node", kind=self.kind),),
        )


def main() -> None:
    print("=== M12 / I-21: supported plan kinds ===")
    print("supported:", [kind.value for kind in sorted(PLAN_NODE_ACTION_TYPES, key=lambda x: x.value)])
    for kind, actions in PLAN_NODE_ACTION_TYPES.items():
        print(f"  {kind.value}: {[action.value for action in sorted(actions, key=lambda x: x.value)]}")

    print("\n=== tool kind + matching tool action ===")
    loop = _loop(
        "tool",
        ActionType.TOOL_CALL,
        payload={"tool": "calculator", "args": {"expr": "2+3"}},
    )
    print("outcome:", loop.step().value)
    print("steps:", loop.steps, "step nodes:", [s.plan_node_id for s in loop.steps_of_run])

    print("\n=== tool kind + mismatching LLM action ===")
    loop = _loop("tool", ActionType.LLM_CALL, payload={"prompt": "wrong action"})
    print("outcome:", loop.step().value)
    print("steps:", loop.steps, "created steps:", [s.plan_node_id for s in loop.steps_of_run])
    print("terminal:", loop.agent_run.status.value)
    print("trace reason:", [e.payload.get("reason") for e in loop.trace.entries if e.kind == "run.finished"])

    print("\n=== human kind + matching approval action ===")
    loop = _loop(
        "human",
        ActionType.HUMAN_APPROVAL,
        timeout=timedelta(seconds=30),
        rationale="please approve",
    )
    print("outcome:", loop.step().value)
    print("pending approval:", loop.pending_approval is not None)
    print("steps:", loop.steps)

    print("\n=== unsupported kinds are refused at plan gate ===")
    run_id = "run_probe12"
    for kind in ("agent", "decision"):
        plan = Plan(run_id=run_id, nodes=(PlanNode(node_id="n0", name=kind, kind=kind),))
        defects = plan_defects(plan, run_id=run_id)
        print(kind, "->", [(defect.code, defect.detail) for defect in defects])


if __name__ == "__main__":
    main()
