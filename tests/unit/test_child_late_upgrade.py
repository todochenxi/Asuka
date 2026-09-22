"""空洞 233：迟到的结果让 S-8 的前提不再成立（M41）。

--------------------------------------------------------------------------
起因：M39 只收回了"不知道"，没收回收不回"没法撤销"

D-23 说"真相到了，账本上那句'不知道'必须被收回"。它做到了一半：

    · "我们不知道它还在不在跑"   → 被收回 ✅
    · "这笔账撤销不了"           → **没动** ❌

而第二句在真相到达的那一刻**同样失效了**：子 Run 真的跑完了 ⟹
副作用确实发生了 ⟹ 那笔账现在撤销得掉。

--------------------------------------------------------------------------
探针（`probe41.py`，已删）跑出来的现状

    status : unresolved          ← 说"撤销不了"
    args   : {}                  ← 撤销参数还空着

    --- 人工点"重试"之后 ---
    payload: {'tool': 'cancel_ticket', 'args': {}}
    outcome: compensated=1       ← **报成功**

三句谎话叠在一起：

    1. 行上说"撤销不了"，其实撤销得掉
    2. 撤销工具被用**空参数**调用 —— S-8 明文写过这个后果：
       "带着空 id 去调撤销接口，最可能的后果是撤销了别的东西，
        或者**静默成功**"
    3. 静默成功之后记成 `compensated`（"已经撤销完了"）

第 2 条是会**真的动到外部世界**的那一条 —— 它不是显示问题。

--------------------------------------------------------------------------
这一轮买的两件事

    D-25  真相证明副作用确实发生了 ⟹ 账本必须升级成"待撤销"：
          撤销参数由 S-8 自己的 `materialize(result)` 补，状态回到 PENDING
    D-26  升级不了时留在 UNRESOLVED，理由必须说清**是哪一样**挡着
          （没做完 / 被叫停 / 撤销参数取不到），不许停在"不知道"那句话上

--------------------------------------------------------------------------
这里的世界是**真跑起来的**（PR-28）

`_LateWorld` 会真派生、真到期、真等到迟到的结果。
手搓 handle 的测试会让"升级"退化成几个对象互相赋值。
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from packages.agent_domain.business.compensation import (
    CompensationRecord,
    CompensationSpec,
    CompensationStatus,
)
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.child_wake import ChildWakeOutcome
from packages.agent_runtime.compensation_events import COMPENSATION_AGGREGATE
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator
from packages.execution_kernel.inmemory import InMemoryOutbox

from tests.unit.test_child_run import (
    ScriptedDecisionEngine,
    ScriptedInterpreter,
    ScriptedPlanner,
    _gateway,
    _tool_runtime,
)
from tests.unit.test_child_run_wake import assemble_runtime_stack

from .sqlite_shim import connect, load_schema_sql
from .test_child_late_result import _LateWorld
from .test_child_wait_deadline import InProcessChildRunSpawner

UNKNOWN_MARKER = "WE DO NOT KNOW"
_SCHEMA = ("001_kernel.sql", "005_compensations.sql")


def _compensable_from_result(run_id: str) -> Action:
    """撤销参数**只能**来自子 Run 的结果。

    与 M39 那个 `_compensable` 的区别是 `result_keys=("ticket_id",)`：
    静态 `args` 让"args 空不空"变成一个**恒真**的命题，
    于是"参数有没有补上"这条断言会假绿。
    这里让撤销参数**只有**迟到的结果能提供 —— 补不上就真的补不上。
    """
    return Action(
        run_id=run_id,
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher"},
        compensation=CompensationSpec(
            tool="cancel_ticket",
            args={},
            result_keys=("ticket_id",),
            description="撤销子 Agent 建的那张工单",
        ),
    )


class _UpgradeWorld(_LateWorld):
    """`_LateWorld` + 一个**参数只能来自结果**的逆操作声明。"""

    def setUp(self) -> None:
        super().setUp()
        # M40：账本的每一次变化都要有事件，所以给这本账接上 outbox。
        self.outbox = InMemoryOutbox()
        self.compensations.events = self.outbox

    def _parent(self, run_id: str = "run_parent") -> Any:
        stack = assemble_runtime_stack(
            agent_id="parent",
            interpreter=ScriptedInterpreter(),
            planner=ScriptedPlanner(),
            decision_engine=ScriptedDecisionEngine([_compensable_from_result(run_id)]),
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

    def _row(self) -> Any:
        rows = self._rows()
        assert len(rows) == 1, f"账本上应当只有一条，实际 {len(rows)} 条（S-2）"
        return rows[0]

    def _events(self) -> list[Any]:
        return [
            e for e in self.outbox.all() if e.aggregate_type == COMPENSATION_AGGREGATE
        ]


class D25TheLedgerBecomesUndoableTest(_UpgradeWorld):
    """D-25：真相证明副作用确实发生了 ⟹ 账本必须变成"待撤销"。"""

    def test_the_row_stops_saying_it_cannot_be_undone(self) -> None:
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.assertIs(self._row().status, CompensationStatus.UNRESOLVED)  # 到期那一刻

        self.waker.wake(child_id)

        self.assertIs(self._row().status, CompensationStatus.PENDING)

    def test_the_undo_args_come_from_the_late_result(self) -> None:
        """撤销参数由 **S-8 自己的** `materialize(result)` 补 —— 不是手搓的。"""
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)
        self.assertEqual(dict(self._row().args), {"ticket_id": "t-9"})

    def test_the_undo_tool_is_no_longer_called_with_empty_args(self) -> None:
        """**本轮真正要闭合的那一条**（S-8）。

        升级之前：`cancel_ticket` 被用 `{}` 调用，并且报成功
        （"撤销一个 id 为空的东西，或者静默成功"）。
        """
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)

        seen: list[Any] = []

        def executor(action: Any) -> bool:
            seen.append(action)
            return True

        outcome = self.waker.saga.compensate("run_parent", executor=executor)

        self.assertEqual(outcome.compensated, 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["tool"], "cancel_ticket")
        self.assertEqual(
            seen[0].payload["args"],
            {"ticket_id": "t-9"},
            "撤销工具必须拿到**真的**工单号 —— 空参数是 S-8 明文警告的后果",
        )

    def test_the_provenance_goes_to_the_event_not_the_row(self) -> None:
        """行上的 `reason` 按约定留空（PENDING 不带理由），来龙去脉走事件。

        没有这条，"升级"会把那句来龙去脉一起抹掉：
        运维看不到"这一笔当初是因为等不到结果才记的"。
        """
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)

        self.assertEqual(self._row().reason, "", "PENDING 的行不带理由（S-5 的约定）")

        events = self._events()
        self.assertEqual([e.event_type for e in events][-1], "compensation.upgraded")
        payload = events[-1].payload
        self.assertIn("D-25", payload["reason"])
        self.assertIn("WITHDRAWN", payload["reason"])
        self.assertEqual(payload["status"], "pending")
        self.assertNotIn(UNKNOWN_MARKER, payload["reason"])

    def test_it_is_not_confused_with_a_manual_retry(self) -> None:
        """`reopened` 与 `upgraded` 是同一个迁移（UNRESOLVED → PENDING），
        但**起因**不同 —— 下游对两者的反应也不同（重试次数 vs 待撤销笔数）。
        """
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)
        types = [e.event_type for e in self._events()]
        self.assertIn("compensation.upgraded", types)
        self.assertNotIn("compensation.reopened", types)

    def test_it_does_not_book_a_second_record(self) -> None:
        """S-2：升级是对**已有**那一行做手术，不是再造一条。"""
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)
        self.assertEqual(len(self._rows()), 1)

    def test_waking_again_changes_nothing(self) -> None:
        """同一条迟到的结果被扫到第二次（兜底扫 / 重复投递）：幂等。"""
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了", "ticket_id": "t-9"}
        )
        self.waker.wake(child_id)
        before = (self._row().status, dict(self._row().args), self._row().version)

        self.waker.wake(child_id)  # 第二次（已 delivered → ALREADY_DELIVERED）

        after = (self._row().status, dict(self._row().args), self._row().version)
        self.assertEqual(before, after)

    def test_the_control_group_an_in_time_result_still_goes_the_normal_way(self) -> None:
        """对照：没迟到的结果走的是**老路径**，不该被这一轮的实现接管。

        它也会在账本上留一行（委派成功 ⟹ 副作用待撤销，这是 S-1 的正常产物），
        但那一行是 `record()` 记的：`compensation.recorded` + 真参数。
        `upgraded` 只该出现在"迟到"那一支 ——
        否则"待撤销笔数"里会混进根本没迟到过的那些。

        ------------------------------------------------------------------
        D-27 之后多出来的那一笔事件不是噪音，是**证据**

        父 Run 现在会被推进到 COMPLETED，于是 S-16 结案（`not_needed`）。
        所以事件流是 `recorded → not_needed` 两步，而"迟到"那一支是
        `unresolved → ... → upgraded`。两者从第一条事件就分开了：
        这一支的**第一条**是 `recorded`（payload 里 `status=pending`），
        而那一支的第一条是 `unresolved`。
        """
        _loop, child_id = self._gone()
        self.registry.mark_finished(child_id, "completed", {"ticket_id": "t-9"})
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)

        row = self._row()
        # NOT_NEEDED 只能从 PENDING 迁过来（S-14），所以它仍然证明"记的时候是 PENDING"
        self.assertIs(row.status, CompensationStatus.NOT_NEEDED)
        self.assertEqual(dict(row.args), {"ticket_id": "t-9"})

        events = self._events()
        types = [e.event_type for e in events]
        self.assertEqual(types[0], "compensation.recorded")
        self.assertEqual(events[0].payload["status"], "pending")
        self.assertNotIn("compensation.upgraded", types, "没迟到过的不该走升级那条路")
        self.assertNotIn("compensation.unresolved", types)


class D26WhenItStillCannotBeUndoneTest(_UpgradeWorld):
    """D-26：升级不了必须**说清是哪一样挡着**，不许停在"不知道"那句话上。"""

    def test_a_failed_child_stays_unresolved_and_says_why(self) -> None:
        """S-11：它没做成 ⟹ 副作用**发没发生仍然不知道**。

        自动撤销一个可能并不存在的东西，比留着它更糟。
        """
        _loop, child_id = self._late(status="failed", result={"summary": "炸了"})
        self.waker.wake(child_id)

        row = self._row()
        self.assertIs(row.status, CompensationStatus.UNRESOLVED)
        self.assertIn("S-11", row.reason)
        self.assertIn("failed", row.reason)
        self.assertNotIn(UNKNOWN_MARKER, row.reason, "那句话已经被收回了")
        self.assertEqual(dict(row.args), {}, "参数取不到就不许带着猜的去撤销")

    def test_a_cancelled_child_stays_unresolved_and_says_why(self) -> None:
        """被叫停的那条可能已经做了一半 —— 同样是"不知道"。"""
        _loop, child_id = self._late(status="cancelled", result={"summary": "叫停了"})
        self.waker.wake(child_id)

        row = self._row()
        self.assertIs(row.status, CompensationStatus.UNRESOLVED)
        self.assertIn("S-11", row.reason)
        self.assertIn("cancelled", row.reason)

    def test_missing_undo_args_stay_unresolved_and_name_the_key(self) -> None:
        """跑完了，但结果里没有撤销要的那个键 —— 必须点名**缺哪个**。

        只说"还是撤销不了"，运维还是得自己去翻子 Run 的结果。
        """
        _loop, child_id = self._late(
            status="completed", result={"summary": "报告写完了"}  # 没有 ticket_id
        )
        self.waker.wake(child_id)

        row = self._row()
        self.assertIs(row.status, CompensationStatus.UNRESOLVED)
        self.assertIn("D-26", row.reason)
        self.assertIn("ticket_id", row.reason, "得说清缺的是哪个键")
        self.assertNotIn(UNKNOWN_MARKER, row.reason)

    def test_none_of_them_are_silently_dropped(self) -> None:
        """三支都**留着一行**、都**带着理由** —— 没有一支是静默的。"""
        for status, result in (
            ("failed", {"summary": "炸了"}),
            ("cancelled", {"summary": "叫停了"}),
            ("completed", {"summary": "报告写完了"}),
        ):
            with self.subTest(status=status):
                self.setUp()
                _loop, child_id = self._late(status=status, result=result)
                self.waker.wake(child_id)
                row = self._row()
                self.assertIs(row.status, CompensationStatus.UNRESOLVED)
                self.assertTrue(row.reason.strip(), "UNRESOLVED 必须带着理由（S-5）")
                self.assertIn("D-26", row.reason)


class PostgresUpgradeTest(unittest.TestCase):
    """判据在 SQL 的 WHERE 里（PR-23）—— 替身这边也跑一遍。

    真 JSONB 那一半（`args = '{}'::jsonb`）只有真库验得了，
    见 `tests/integration/test_child_late_upgrade_real_pg.py`。
    """

    def setUp(self) -> None:
        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        self.outbox = InMemoryOutbox()
        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(self.conn.close)
        self.store = PostgresCompensationStore(self.conn, events=self.outbox)
        self.saga = SagaCoordinator(store=self.store)

    def _book(self, *, args: dict | None = None) -> Any:
        return self.saga.record_unresolved(
            run_id="run_1",
            step_id="step_1",
            task_id="task_1",
            execution_id="exec_1",
            action=_compensable_from_result("run_1"),
            reason="we do not know yet",
        )

    def test_the_row_becomes_pending_with_the_real_args(self) -> None:
        self._book()
        upgraded = self.saga.upgrade_to_compensable(
            execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25: upgraded"
        )
        assert upgraded is not None
        self.assertIs(upgraded.status, CompensationStatus.PENDING)
        self.assertEqual(dict(upgraded.args), {"ticket_id": "t-9"})
        self.assertEqual(upgraded.reason, "")

        again = self.store.get_by_execution("exec_1")
        assert again is not None
        self.assertIs(again.status, CompensationStatus.PENDING)

    def test_it_emits_upgraded(self) -> None:
        self._book()
        self.saga.upgrade_to_compensable(
            execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25: upgraded"
        )
        events = [e for e in self.outbox.all()]
        self.assertEqual(
            [e.event_type for e in events],
            ["compensation.unresolved", "compensation.upgraded"],
        )
        self.assertIn("D-25", events[-1].payload["reason"])

    def test_a_row_with_args_is_not_touched(self) -> None:
        """`args` 非空 = 它当初不是因为缺参数才撤销不了的。

        ⚠️ `save()` 不写 `args`（只有 `add()` 与 D-25 那句 UPDATE 写），
        所以这里直接改库 —— 恰好也证明了判据活在 SQL 里，不活在对象上。
        """
        self._book()
        self.conn.cursor().execute(
            "UPDATE compensations SET args = %s WHERE execution_id = 'exec_1'",
            (json.dumps({"ticket_id": "t-1"}),),
        )

        self.assertIsNone(
            self.saga.upgrade_to_compensable(
                execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25"
            )
        )
        after = self.store.get_by_execution("exec_1")
        assert after is not None
        self.assertEqual(dict(after.args), {"ticket_id": "t-1"})


class RowsThatWereNotMissingArgsTest(unittest.TestCase):
    """`args = '{}'` 是判据本身：只有**当初缺参数**的那一行才配被自动升级。

    `args` 非空 = 撤销参数一直都在 —— 那它当初是"撤销动作跑失败了"
    （S-5/S-6），要不要再试一次是**人**的决定（S-14 唯一例外）。
    真相到达不构成自动重试的理由：让它自动重试，等于用一次真实副作用
    去覆盖一次**已经失败过**的撤销。
    """

    def setUp(self) -> None:
        self.store = InMemoryCompensationStore()
        self.saga = SagaCoordinator(store=self.store)
        self.record = CompensationRecord(
            run_id="run_1",
            step_id="step_1",
            task_id="task_1",
            execution_id="exec_1",
            action_type="agent_delegation",
            tool="cancel_ticket",
            args={"ticket_id": "t-1"},
            description="撤销子 Agent 建的那张工单",
            status=CompensationStatus.UNRESOLVED,
            reason="S-5: compensation execution did not complete",
        )
        self.store.add(self.record)

    def test_a_row_that_already_has_undo_args_is_not_auto_retried(self) -> None:
        self.assertIsNone(
            self.saga.upgrade_to_compensable(
                execution_id="exec_1",
                args={"ticket_id": "t-9"},
                reason="D-25: upgraded",
            )
        )
        self.assertIs(self.record.status, CompensationStatus.UNRESOLVED)
        self.assertEqual(dict(self.record.args), {"ticket_id": "t-1"})

    def test_a_row_that_has_been_disposed_of_is_left_alone(self) -> None:
        self.record.transition(CompensationStatus.PENDING)
        self.store.save(self.record)
        self.assertIsNone(
            self.saga.upgrade_to_compensable(
                execution_id="exec_1", args={"ticket_id": "t-9"}, reason="D-25"
            )
        )
        self.assertIs(self.record.status, CompensationStatus.PENDING)

    def test_a_row_that_does_not_exist_creates_nothing(self) -> None:
        self.assertIsNone(
            self.saga.upgrade_to_compensable(
                execution_id="nope", args={}, reason="D-25"
            )
        )
        self.assertEqual(len(self.store._by_id), 1)

    def test_the_domain_guard_says_the_same_thing(self) -> None:
        """PR-23：判据不只在 SQL 的 WHERE 里 —— 领域对象自己也得挡住。"""
        with self.assertRaises(InvariantViolation):
            self.record.become_compensable({"ticket_id": "t-9"})


if __name__ == "__main__":
    unittest.main()
