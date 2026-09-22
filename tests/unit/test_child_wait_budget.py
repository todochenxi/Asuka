"""空洞 230：一次派生的等待上限**只能**由全局默认给出（M43 / §81）。

--------------------------------------------------------------------------
这个洞的形状：一个**按次**的问题，被一个**全局**的旋钮回答着

    "这次派生该等多久"   主语是**这次派生**（child_runs : wait_until = 1 : 1）
    它唯一的落点          是**登记处的一个构造参数**（registry : wait_timeout）

这是一次基数错配 —— 与 A-3 那个"第二个 Run"同族：
不是"少了一个参数"，是"一个 1:1 的事实被挤进了一个 1:N 的格子里"。

代价是**双向**的，而且两个方向都很贵：

    · 想给那条两小时的深度研究多等一会儿 → 只能把全局调到两小时
      → **所有**派生都跟着多等两小时 → 一条真死掉的子 Run 让它的父 Run
        多挂两小时（015 / 空洞 229 刚买到的东西被稀释掉）
    · 不调 → 那条子 Agent 在第 30 分钟被判"等不到"，而它其实马上就要
      回来了 —— 平台**制造了一个谎言**（PR-19：等不到 ≠ 它没干成）

--------------------------------------------------------------------------
为什么"开入口"必须同时"定边界"

等待上限属于**控制**（Harness），不属于**决策**（Intelligence）：

    · "这次委派大概要跑多久"确实是决策的一部分 —— 只有 Intelligence 知道
      它派出的是个两小时的深度研究还是两秒钟的查表
    · 但"父 Run 什么时候不再等"是控制 —— 它决定一条挂住的 Run 能挂多久，
      而"挂住的父 Run"正是 M37~M42 连续四轮在治的形状

所以只开一半比不开更糟：不开的时候至少还有全局 30 分钟兜着，
开了却不管边界，一个幻觉出来的 `wait_timeout_seconds: 99999999`
就能让父 Run 挂三年，界面上还显示"在等子 Agent，一切正常"。

--------------------------------------------------------------------------
这里的世界是**真跑起来的**

`BudgetWorld` 让父 Run 真派生一条子 Run、真挂起、真落快照。
手搓 `wait_until` 的测试只会证明"这个字段被赋值了"，
而真跑一遍才会撞上 `bind()`、闸门与那一道裁决（PR-28）。
"""
from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.agent_domain.business.snapshot import state_from_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import (
    Action,
    ActionType,
    CompensationSpec,
)
from packages.agent_runtime.assembly import assemble_runtime_stack
from packages.agent_runtime.child_wait import ChildRunWaitExpirer
from packages.agent_runtime.child_wake import ChildRunWaker
from packages.agent_runtime.delegation import (
    DEFAULT_CHILD_WAIT_TIMEOUT,
    MAX_CHILD_WAIT_TIMEOUT,
    ChildRunKind,
    ChildRunRegistry,
    ChildRunRequest,
    InProcessChildRunSpawner,
)
from packages.agent_runtime.driving import InProcessRunDriver
from packages.agent_runtime.reducer import CHILD_RUN_UNKNOWN
from packages.agent_runtime.saga import SagaCoordinator

from .test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)
from .test_child_run_wake import _handle
from .test_child_wait_deadline import WaitWorld, _compensable


MIGRATION_016 = "016_child_wait_ceiling.sql"

#: 内存登记处的替身跑不到 PG 的 CHECK，但 016 会被加载并在 sqlite 上执行 ——
#: 于是"这份迁移至少能跑起来"这件事在这里也被验过一遍（迁移卫生测试要求）。
SCHEMA = (
    "008_child_runs.sql",
    "010_child_run_result.sql",
    "012_child_run_cancel_request.sql",
    "015_child_wait_deadline.sql",
    MIGRATION_016,
)


def _budgeted(run_id: str, seconds: Any) -> Action:
    """一次委派，payload 里**带着**这次派生的等待声明。"""
    payload: dict[str, Any] = {"agent_id": "researcher"}
    if seconds is not None:
        payload["wait_timeout_seconds"] = seconds
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload=payload,
        compensation=CompensationSpec(
            tool="cancel_ticket",
            args={"ticket_id": "t-1"},
            result_keys=(),
            description="撤销子 Agent 建的那张工单",
        ),
    )


class BudgetWorld(WaitWorld):
    """`WaitWorld`，但全局默认回到 30 分钟。

    `WaitWorld` 把默认设成 1 毫秒是为了让"到期"立刻成立；
    而这里要看的是"这次派生**自己**要了多久"，
    一个 1 毫秒的全局默认会把它盖掉 —— 于是回到平台默认的 30 分钟。
    """

    #: 这次派生声明的等待上限（秒）。`None` = payload 里不写这个键。
    budget: Any = None

    def setUp(self) -> None:
        super().setUp()
        self.registry = ChildRunRegistry()
        self.waker = ChildRunWaker(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),
            driver=InProcessRunDriver(recovery=self.recovery),
        )
        self.expirer = ChildRunWaitExpirer(
            registry=self.registry,
            recovery=self.recovery,
            saga=SagaCoordinator(store=self.compensations),
            driver=InProcessRunDriver(recovery=self.recovery),
        )

    def _parent(self, run_id: str = "run_parent") -> Any:
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_budgeted(run_id, self.budget)]),
            gateway=_gateway(),
            tool_runtime=_tool_runtime(),
            kernel=self.kernel,
            snapshots=self.snapshots,
            compensations=self.compensations,
            spawner=InProcessChildRunSpawner(
                factory=self._factory, registry=self.registry
            ),
            max_steps=6,
        )
        stack.loop.start("go", run_id=run_id)
        return stack.loop

    def _unknown_observations(self) -> list[Any]:
        """父 Run 那笔"等不到"的 Observation —— 从**存储**里读，不走 rebuild()。"""
        return [
            obs
            for obs in state_from_dict(self._latest().state).observations
            if obs.kind == CHILD_RUN_UNKNOWN
        ]


# ---------------------------------------------------------------- 控制组


class TheHoleTest(unittest.TestCase):
    """先证明这个洞存在：唯一的旋钮是**全局**的，于是它一动就动到所有人。"""

    def test_a_two_hour_delegation_is_declared_unreachable_at_thirty_minutes(self) -> None:
        """不调全局默认的那半边代价：一条还在跑的子 Run 被判"等不到"。"""
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_long"))
        assert handle.wait_until is not None
        self.assertTrue(
            handle.is_overdue(handle.spawned_at + timedelta(minutes=31)),
            "第 31 分钟它就被判等不到了 —— 而它其实还要跑一个半小时",
        )

    def test_the_only_knob_is_the_registry_so_it_moves_everyone(self) -> None:
        """调全局默认的那半边代价：**所有**派生跟着一起变长。"""
        patient = ChildRunRegistry(wait_timeout=timedelta(hours=2))
        handle = patient.bind(_handle("child_quick"))
        now = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertFalse(
            handle.is_overdue(now),
            "一条 5 分钟前就该被判等不到的死派生，因为全局动了一下而还挂在队列外",
        )


# ---------------------------------------------------------------- D-31


class D31TheDerivationDeclaresItsOwnBudgetTest(unittest.TestCase):
    """D-31：这次派生可以自己说等多久；它没说，才听登记处的。"""

    def _request(self, **over: Any) -> ChildRunRequest:
        base: dict[str, Any] = dict(
            parent_run_id="run_parent",
            parent_execution_id="exec_1",
            kind=ChildRunKind.AGENT,
            target="researcher",
            action=_compensable("run_parent"),
            parent_task_id="task_1",
        )
        base.update(over)
        return ChildRunRequest(**base)

    def test_a_declaration_reaches_the_frozen_deadline(self) -> None:
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(hours=2))

    def test_seconds_are_normalized_to_a_duration(self) -> None:
        """payload 里来的是数字，拿在手上的必须是 `timedelta`（B-7）。"""
        self.assertEqual(self._request(wait_timeout=7200).wait_timeout, timedelta(hours=2))
        self.assertEqual(
            self._request(wait_timeout=timedelta(minutes=90)).wait_timeout,
            timedelta(minutes=90),
        )

    def test_without_a_declaration_the_registry_default_wins(self) -> None:
        registry = ChildRunRegistry(wait_timeout=timedelta(minutes=7))
        handle = registry.bind(_handle("child_1"))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(minutes=7))

    def test_a_declaration_may_be_shorter_than_the_default(self) -> None:
        """短不是"没用到"：知道它两秒钟就该回来的调用方可以**早点**认输。"""
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1", wait_timeout=timedelta(seconds=2)))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(seconds=2))

    def test_a_retry_cannot_move_the_deadline_it_froze(self) -> None:
        """D-1 的另一半：第二次派生拿回第一条，上限也是**第一条**那次说的。"""
        registry = ChildRunRegistry()
        first = registry.bind(
            _handle("child_1", parent_execution_id="exec_1", wait_timeout=timedelta(minutes=5))
        )
        again = replace(
            _handle("child_2", parent_execution_id="exec_1"),
            wait_timeout=timedelta(hours=2),
        )
        second = registry.bind(again)
        self.assertEqual(second.child_run_id, "child_1")
        assert second.wait_until is not None and second.spawned_at is not None
        self.assertEqual(second.wait_until, first.wait_until)
        self.assertEqual(second.wait_until - second.spawned_at, timedelta(minutes=5))


class D31TheProposalComesFromTheActionTest(BudgetWorld):
    """真跑一遍：模型 payload 里那句话，真的走到了派生登记处（PR-28）。"""

    budget = 7200

    def test_the_child_is_spawned_with_the_declared_budget(self) -> None:
        loop, _child_id = self._spawned()
        handle = loop.pending_child
        assert handle is not None and handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(hours=2))
        # 30 分钟时它**没有**被判等不到 —— 这正是那个洞原来的样子
        self.assertFalse(handle.is_overdue(handle.spawned_at + timedelta(minutes=31)))


class D31WithoutADeclarationTheDefaultStillAppliesTest(BudgetWorld):
    """控制组：payload 里没有那句话时，落点是全局默认（行为不变）。"""

    budget = None

    def test_the_default_is_still_what_it_agreed_to(self) -> None:
        loop, _child_id = self._spawned()
        handle = loop.pending_child
        assert handle is not None and handle.wait_until is not None
        self.assertEqual(
            handle.wait_until - handle.spawned_at, DEFAULT_CHILD_WAIT_TIMEOUT
        )


# ---------------------------------------------------------------- D-32


class D32TheBudgetIsBoundedTest(unittest.TestCase):
    """D-32：声明必须落在 `(0, MAX]` 内。越界是**点名拒绝**，不是静默处理。"""

    def _request(self, **over: Any) -> ChildRunRequest:
        base: dict[str, Any] = dict(
            parent_run_id="run_parent",
            parent_execution_id="exec_1",
            kind=ChildRunKind.AGENT,
            target="researcher",
            action=_compensable("run_parent"),
            parent_task_id="task_1",
        )
        base.update(over)
        return ChildRunRequest(**base)

    def _refused(self, **over: Any) -> str:
        with self.assertRaises(InvariantViolation) as cm:
            self._request(**over)
        return str(cm.exception)

    def test_a_budget_of_zero_is_refused(self) -> None:
        """0 不是"立刻到期"，是"连一次被等的机会都没有"（015 的 CHECK 同款）。"""
        self.assertIn("D-32", self._refused(wait_timeout=0))

    def test_a_negative_budget_is_refused(self) -> None:
        self.assertIn("D-32", self._refused(wait_timeout=-1))
        self.assertIn("D-32", self._refused(wait_timeout=timedelta(minutes=-5)))

    def test_a_budget_longer_than_the_ceiling_is_refused_not_clamped(self) -> None:
        """截断是最糟的那种处理：父 Run 会在一个**没人同意过的时刻**被判等不到。"""
        message = self._refused(wait_timeout=timedelta(hours=7))
        self.assertIn("D-32", message)
        # 点名：要了多少、上限是多少
        self.assertIn("7:00:00", message)
        self.assertIn("6:00:00", message)

    def test_the_refusal_is_not_a_silent_fallback_to_the_default(self) -> None:
        """静默回退默认 = 一次**被吞掉的拒绝**：调用方拿到的是正常返回值。"""
        registry = ChildRunRegistry(wait_timeout=timedelta(minutes=7))
        with self.assertRaises(InvariantViolation):
            registry.bind(_handle("child_1", wait_timeout=timedelta(hours=7)))
        self.assertIsNone(registry.for_child("child_1"), "拒绝之后什么都没登记")

    def test_a_misconfigured_default_is_refused_too(self) -> None:
        """`bind()` 那一道守的是**全局默认被配错** —— 请求侧看不见它。"""
        registry = ChildRunRegistry(wait_timeout=timedelta(hours=7))
        with self.assertRaises(InvariantViolation) as cm:
            registry.bind(_handle("child_1"))
        self.assertIn("D-32", str(cm.exception))

    def test_true_is_not_a_one_second_budget(self) -> None:
        """`True` 是 `int` 的子类 —— 不挡掉就是"等 1 秒"被静默采纳。"""
        self.assertIn("bool", self._refused(wait_timeout=True))

    def test_a_string_is_not_guessed(self) -> None:
        """`"3600"` 与 `3600` 是两件事；替它猜一个含义不如让它带着原因失败。"""
        self.assertIn("str", self._refused(wait_timeout="3600"))

    def test_forever_is_not_a_deadline(self) -> None:
        self.assertIn("D-32", self._refused(wait_timeout=float("inf")))
        self.assertIn("D-32", self._refused(wait_timeout=float("nan")))

    def test_the_registry_ceiling_can_only_be_tighter(self) -> None:
        """部署可以更严格，不能更宽松 —— 平台上限是 016 的一条 CHECK。"""
        with self.assertRaises(InvariantViolation):
            ChildRunRegistry(max_wait_timeout=timedelta(hours=7))
        with self.assertRaises(InvariantViolation):
            ChildRunRegistry(max_wait_timeout=timedelta(0))

        strict = ChildRunRegistry(max_wait_timeout=timedelta(hours=1))
        ok = strict.bind(_handle("child_1", wait_timeout=timedelta(minutes=30)))
        assert ok.wait_until is not None
        self.assertEqual(ok.wait_until - ok.spawned_at, timedelta(minutes=30))
        with self.assertRaises(InvariantViolation):
            strict.bind(_handle("child_2", parent_execution_id="e2",
                                wait_timeout=timedelta(hours=2)))

    def test_a_hand_built_handle_is_judged_too(self) -> None:
        """归一化钉在**裁决**那一处，不是"谁造了这个 handle"那一处（PR-23）。

        探针实录（M43）：手搓一个 `wait_timeout=90` 的 handle 直接 `bind()`，
        在实现把归一化挪进 `freeze_wait_deadline` 之前，炸出来的是

            TypeError: unsupported operand type(s) for +:
                       'datetime.datetime' and 'int'

        响是响了，可它没说出**是谁要了多少**（PR-19），
        而"绕过 `ChildRunRequest` 就绕过了判据"正是 D-32 不该有的形状。
        """
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1", wait_timeout=90))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(seconds=90))

    def test_a_hand_built_handle_with_a_bool_is_refused_by_name(self) -> None:
        """`True` 走的是同一道门 —— 手搓的路径不许是那条没门的侧门。"""
        registry = ChildRunRegistry()
        with self.assertRaises(InvariantViolation) as cm:
            registry.bind(_handle("child_1", wait_timeout=True))
        self.assertIn("bool", str(cm.exception))


# ---------------------------------------------------------------- D-33


class D33TheArbitrationHappensBeforeTheWriteTest(unittest.TestCase):
    """D-33：裁决发生在**写库之前**，抛的是会说话的错误。"""

    def setUp(self) -> None:
        from packages.agent_runtime.adapters.postgres import PostgresChildRunRegistry

        from .sqlite_shim import connect, load_schema_sql

        self.conn = connect(schema_sql=load_schema_sql(*SCHEMA))
        self.addCleanup(self.conn.close)
        self.registry = PostgresChildRunRegistry(self.conn)

    def test_a_budget_beyond_the_ceiling_writes_nothing(self) -> None:
        with self.assertRaises(InvariantViolation) as cm:
            self.registry.bind(_handle("child_1", wait_timeout=timedelta(hours=7)))
        # 不是 `IntegrityError` —— 那句不会说出是谁要求了多少（PR-19）
        self.assertIn("D-32", str(cm.exception))
        self.assertIsNone(self.registry.for_child("child_1"), "一行都没写进去")

    def test_a_declaration_within_the_ceiling_lands_in_the_row(self) -> None:
        bound = self.registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        row = self.registry.for_child("child_1")
        assert row is not None and row.wait_until is not None
        self.assertEqual(row.wait_until - row.spawned_at, timedelta(hours=2))
        self.assertEqual(row.wait_until, bound.wait_until)

    def test_the_ceiling_is_the_same_knob_everywhere(self) -> None:
        """替身与真库共用 `_resolve_ceiling` —— 部署能不能更宽松只有一个答案。"""
        from packages.agent_runtime.adapters.postgres import PostgresChildRunRegistry

        with self.assertRaises(InvariantViolation):
            PostgresChildRunRegistry(self.conn, max_wait_timeout=timedelta(hours=7))


# ---------------------------------------------------------------- D-34


class D34OneDeadlinePerDerivationTest(unittest.TestCase):
    """D-34：冻结之后 handle 上只剩**一个**上限。"""

    def test_the_declaration_is_gone_after_the_freeze(self) -> None:
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        self.assertIsNone(
            handle.wait_timeout,
            "一个 handle 上同时挂着'要了多久'和'约定到什么时候'就有了两个上限（B-7）",
        )

    def test_the_only_answer_left_is_the_deadline_minus_the_spawn(self) -> None:
        registry = ChildRunRegistry()
        handle = registry.bind(_handle("child_1", wait_timeout=timedelta(hours=2)))
        assert handle.wait_until is not None
        self.assertEqual(handle.wait_until - handle.spawned_at, timedelta(hours=2))

    def test_the_memory_registry_and_postgres_give_the_same_answer(self) -> None:
        """替身与真库不许给出两个答案（PR-23 的那一半）。"""
        from packages.agent_runtime.adapters.postgres import PostgresChildRunRegistry

        from .sqlite_shim import connect, load_schema_sql

        conn = connect(schema_sql=load_schema_sql(*SCHEMA))
        self.addCleanup(conn.close)

        spawned = datetime(2026, 1, 1, tzinfo=timezone.utc)
        in_memory = ChildRunRegistry().bind(
            _handle("child_1", spawned_at=spawned, wait_timeout=timedelta(hours=2))
        )
        pg = PostgresChildRunRegistry(conn)
        pg.bind(_handle("child_1", spawned_at=spawned, wait_timeout=timedelta(hours=2)))
        read_back = pg.for_child("child_1")
        assert read_back is not None
        assert in_memory.wait_until is not None
        self.assertEqual(read_back.wait_until, in_memory.wait_until)
        self.assertIsNone(read_back.wait_timeout)
        self.assertIsNone(in_memory.wait_timeout)


# ---------------------------------------------------------------- 可查


class TheAgreedBudgetIsVisibleWhereItIsJudgedTest(BudgetWorld):
    """上限可以按次声明之后，"为什么等了这么久才报等不到"必须答得出来。"""

    budget = 0.001

    def test_the_observation_says_how_long_it_agreed_to_wait(self) -> None:
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        unknown = self._unknown_observations()
        self.assertTrue(unknown, "父 Run 该留下一笔 child_run.unknown")
        last = unknown[-1]
        self.assertEqual(last.content["child_run_id"], child_id)
        self.assertAlmostEqual(last.content["agreed_wait_seconds"], 0.001, places=6)
        self.assertIsNotNone(last.content["wait_until"])

    def test_without_a_declaration_the_observation_names_the_default(self) -> None:
        """控制组：没声明时那一笔写的是全局默认 —— 于是两者可以区分。"""
        self.budget = None
        _loop, child_id = self._gone()
        self.expirer.expire(child_id)

        unknown = self._unknown_observations()
        self.assertTrue(unknown)
        self.assertAlmostEqual(
            unknown[-1].content["agreed_wait_seconds"],
            DEFAULT_CHILD_WAIT_TIMEOUT.total_seconds(),
            places=3,
        )


# ---------------------------------------------------------------- 016 / SQL


class TheCeilingIsPhysicalTest(unittest.TestCase):
    """016 那条 CHECK 与 `MAX_CHILD_WAIT_TIMEOUT` 必须是同一个数。"""

    def _sql(self) -> str:
        root = Path(__file__).resolve().parents[2]
        return (root / "infrastructure" / "postgres" / MIGRATION_016).read_text(
            encoding="utf-8"
        )

    def test_the_sql_ceiling_is_the_python_ceiling(self) -> None:
        """与 015 那句回填是同一条规矩：改一边忘另一边会红。"""
        found = re.findall(
            r"interval\s*'(\d+)\s*(second|minute|hour|day)s?'", self._sql()
        )
        self.assertEqual(len(found), 1, f"016 里应当只有一处 interval：{found}")
        number, unit = found[0]
        seconds = float(number) * {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
        }[unit]
        self.assertEqual(seconds, MAX_CHILD_WAIT_TIMEOUT.total_seconds())

    def test_the_ceiling_is_not_the_default(self) -> None:
        """两个数是两个问题（默认 vs 上限），混成一个就没有"更严格"这回事了。"""
        self.assertNotEqual(MAX_CHILD_WAIT_TIMEOUT, DEFAULT_CHILD_WAIT_TIMEOUT)
        self.assertGreater(MAX_CHILD_WAIT_TIMEOUT, DEFAULT_CHILD_WAIT_TIMEOUT)

    def test_the_constraint_has_a_name_ops_can_read(self) -> None:
        self.assertIn("child_runs_wait_deadline_ceiling", self._sql())

    def test_sqlite_refuses_a_raw_insert_beyond_the_ceiling(self) -> None:
        """裸 INSERT 绕过了 Python —— 那一道由数据库守（D-33 的第二道）。"""
        from sqlite3 import IntegrityError

        from .sqlite_shim import connect, load_schema_sql

        conn = connect(schema_sql=load_schema_sql(*SCHEMA))
        self.addCleanup(conn.close)
        with self.assertRaises(IntegrityError) as cm:
            conn.cursor().execute(
                "INSERT INTO child_runs (child_run_id, kind, parent_run_id, "
                "parent_execution_id, parent_task_id, target, action, spawned_at, "
                "wait_until) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "child_1",
                    "agent",
                    "run_parent",
                    "exec_1",
                    "task_1",
                    "researcher",
                    '{"action_type": "AGENT_DELEGATION"}',
                    "2020-01-01 00:00:00.000000",
                    # 以前夹具里写的是 2099 年；016 之后它不再合法
                    "2021-01-01 00:00:00.000000",
                ),
            )
        self.assertIn("child_runs_wait_deadline_ceiling", str(cm.exception))
