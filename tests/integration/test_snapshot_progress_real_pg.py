"""真 PostgreSQL 上的 R-7：进度进快照表 → 读回来 → 接到新引擎上。

--------------------------------------------------------------------------
为什么这一条不能只留在单元测试里

`tests/unit/test_snapshot_carries_port_progress.py` 有同一条判据，
但它跑在 `sqlite_shim` 上。本文件把同一条流程搬到真 PG，
于是下面这些第一次被真正的存储层验到：

    018 那一列真的存在于真库里（shim 是我手写的翻译，可能翻译错了）
    `PostgresRunSnapshotStore` 的 16 列 INSERT（含 018 的新列）
    `progress` 作为 JSONB 的**往返保真**（字典进去、字典出来，
                                      而不是被 jsonb 规范化成别的东西）
    `jsonb_typeof(progress) = 'object'` 那条 CHECK 在真 PG 上真的生效

最后一条尤其重要：它在 shim 上是被 `json_type()` **翻译**过去的，
"翻译等价"是我读文档得出的判断。真 PG 上跑一遍才是证据（PR-23）。

--------------------------------------------------------------------------
"重启"在这里是真的

第二个 loop 拿到的是**全新**的 snapshot store 与**全新**的引擎对象，
唯一共享的东西是那一个数据库。内存态一律不共享。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.snapshot import RunSnapshot, state_to_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.goal import Budget, Goal
from packages.agent_domain.intelligence.plan import Plan, PlanNode
from packages.agent_domain.intelligence.state import State
from packages.agent_runtime.adapters.postgres import PostgresRunSnapshotStore
from packages.agent_runtime.loop import AgentLoop

from tests.unit.test_agent_loop import MinimalLoopTest, ScriptedInterpreter

from ._pg import RealPostgresCase


class _StatelessPlanner:
    def plan(self, state: State) -> Plan:
        return Plan(run_id=state.run_id, nodes=(PlanNode(node_id="n0", name="go"),))


class _ResumableEngine:
    """`DemoDecisionEngine` 的同形：`progress()` + `resume()` 成对。"""

    def __init__(self, calls: int = 0) -> None:
        self.calls = calls

    def progress(self) -> object:
        return {"calls": self.calls}

    def resume(self, progress: object) -> None:
        assert isinstance(progress, dict)
        self.calls = int(progress["calls"])

    def decide(self, state: State) -> Decision:
        self.calls += 1
        kind = ActionType.LLM_CALL if self.calls == 1 else ActionType.FINISH
        return Decision(
            run_id=state.run_id,
            selected_action=Action(run_id=state.run_id, action_type=kind),
            rationale="resumable",
        )


def _state(run_id: str) -> State:
    return State(
        run_id=run_id,
        goal=Goal(
            run_id=run_id,
            objective="answer",
            success_criteria=("an answer is produced",),
            budget=Budget(max_steps=5),
        ),
    )


class PortProgressSurvivesRealPostgresTest(RealPostgresCase):
    def setUp(self) -> None:
        super().setUp()
        self.snapshots = PostgresRunSnapshotStore(self.conn)

    def _loop(self, engine: Any) -> AgentLoop:
        base = MinimalLoopTest("test_full_loop_reaches_goal")
        base.setUp()
        return AgentLoop(
            kernel=base.kernel,
            worker=base.worker,
            interpreter=ScriptedInterpreter(),
            planner=_StatelessPlanner(),
            decision_engine=engine,
        )

    def _save(self, run_id: str, engine: Any) -> RunSnapshot:
        snapshot = RunSnapshot(
            run_id=run_id,
            agent_id="agent-it",
            status="RUNNING",
            state=state_to_dict(_state(run_id)),
            progress={"planner": None, "decision_engine": engine.progress()},
        )
        self.snapshots.save(snapshot)
        return snapshot

    # ------------------------------------------------------- 往返保真
    def test_progress_survives_a_round_trip_through_jsonb(self) -> None:
        """字典进去，**字典**出来 —— 不是被 jsonb 规范化成别的东西。"""
        run_id = "run_r7_roundtrip"
        self._save(run_id, _ResumableEngine(calls=3))

        loaded = self.snapshots.latest(run_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.progress["decision_engine"], {"calls": 3})
        self.assertIsNone(loaded.progress["planner"])

    def test_a_fresh_engine_is_resumed_from_the_database(self) -> None:
        """★ 本文件的核心：恢复时接的是**库里的**那个值。

        一条 Run 走过 2 步 → 落库 → 进程重启（全新引擎 calls=0）→
        从库里读回来 → 必须接成 2，而不是从 0 重来。
        """
        run_id = "run_r7_resume"
        self._save(run_id, _ResumableEngine(calls=2))

        snapshot = self.snapshots.latest(run_id)
        assert snapshot is not None

        fresh = _ResumableEngine(calls=0)          # 组合根重装配出的新引擎
        loop = self._loop(fresh)
        loop.restore(snapshot)

        self.assertEqual(fresh.calls, 2)

    def test_a_rewound_engine_is_still_refused_on_real_pg(self) -> None:
        """接不回去的实现（没有 `resume`）→ 真 PG 上同样是点名拒绝。"""

        class _NoResume:
            def __init__(self, calls: int = 0) -> None:
                self.calls = calls

            def progress(self) -> object:
                return {"calls": self.calls}

            def decide(self, state: State) -> Decision:
                return Decision(
                    run_id=state.run_id,
                    selected_action=Action(
                        run_id=state.run_id, action_type=ActionType.FINISH
                    ),
                    rationale="x",
                )

        run_id = "run_r7_refused"
        self._save(run_id, _NoResume(calls=4))

        snapshot = self.snapshots.latest(run_id)
        assert snapshot is not None

        loop = self._loop(_NoResume(calls=0))
        with self.assertRaises(InvariantViolation) as ctx:
            loop.restore(snapshot)

        self.assertIn("decision_engine", str(ctx.exception))

    # ------------------------------------------------------- 物理约束
    def test_the_check_rejects_a_non_object_progress(self) -> None:
        """`jsonb_typeof(progress) = 'object'` 在**真 PG** 上真的生效。

        这条在 shim 上是靠 `json_type()` 翻译过去的 ——
        "翻译等价"是我读文档得出的判断。真 PG 跑一遍才是证据。

        一个数组形态的 progress 会让 `stored.get("planner")` 静默拿到
        None → 误判成"两边都没说" → **恢复被放行**。所以这个约束值钱。
        """
        import psycopg

        run_id = "run_r7_array"
        # 约束在 INSERT 那一刻就判（不是 commit 时）——
        # PG 的 CHECK 是行级即时判定，把断言放在 `execute` 上。
        with self.assertRaises(psycopg.errors.CheckViolation) as ctx:
            self.conn.execute(
                """
                INSERT INTO run_snapshots (
                    snapshot_id, run_id, agent_id, status, step_count,
                    consecutive_denials, current_step_id, state, steps,
                    spent, trace, progress, reason, created_at
                ) VALUES (
                    'snap_bad', %s, 'a', 'RUNNING', 0, 0, '', '{}'::jsonb, '[]'::jsonb,
                    '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '', now()
                )
                """,
                (run_id,),
            )
        # 点名是哪条约束 —— "某处会拦"不等于"这一处会拦"
        self.assertIn("progress_is_object", str(ctx.exception))
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
