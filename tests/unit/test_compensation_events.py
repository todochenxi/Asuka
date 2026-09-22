"""空洞 232：账本的每一次变化都必须有事件（M40）。

--------------------------------------------------------------------------
起因

`compensations` 表上**任何一次写入都不发事件**。一条副作用被记下来、
被认领、被撤销、被判"存疑"、被改口 —— PG 全都写了，Kafka 那边一个字没有。

按 X-3 的判据，这是"两件事实不同事务"的同族错误：PG 里的事实改了，
事件流这条事实**没有改**。下游（Read Model / 审计 / 看板）的账本因此
永远停在"这个 Run 没有副作用"那一格 —— 而它恰恰是看板第一个要问的数：
**这个 Run 到底留下了几笔没人管的副作用**。

--------------------------------------------------------------------------
这一轮买的两件事

    X-15  事件流必须能**独立读懂** —— 所以发射点覆盖账本的**全部**写路径，
          而不是只补 D-23 那一处改口（一句没有前文的更正通知等于没说）

    （B-7）两个 `CompensationStore` 实现必须发出**同一种**事件 ——
          所以这里有 `LedgerEventsContract`：**同一段剧本跑两遍**，
          内存版与 PG 版必须得到同一串事件类型

--------------------------------------------------------------------------
为什么按迁移命名事件（不是按落地后的状态）

`reopened`（UNRESOLVED → PENDING）落地之后那一行也是 PENDING，和
`recorded` 一模一样。合成一个名字，下游就无法区分"新出现一笔待撤销"
与"早就有的那一笔，人工决定再试一次" —— 而它们的反应正好相反。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.events.event import COMPENSATION_RECORDED
from packages.agent_domain.execution.execution import ExecutionStatus
from packages.agent_runtime.compensation_events import (
    COMPENSATION_AGGREGATE,
    compensation_event,
    transition_event_type,
)
from packages.agent_runtime.saga import InMemoryCompensationStore, SagaCoordinator
from packages.execution_kernel.inmemory import InMemoryOutbox

from .sqlite_shim import connect, load_schema_sql
from .test_child_wait_deadline import _compensable

_SCHEMA = ("001_kernel.sql", "005_compensations.sql")

RUN = "run_events"
STEP = "step_1"
TASK = "task_1"


def _book(saga: SagaCoordinator, execution_id: str) -> Any:
    """正向执行成功 → 记一笔"待撤销"（PENDING）。"""
    return saga.record(
        run_id=RUN,
        step_id=STEP,
        task_id=TASK,
        execution_id=execution_id,
        action=_compensable(RUN),
        result={"ticket_id": "t-1"},
        execution_status=ExecutionStatus.COMPLETED,
    )


def _book_unknown(saga: SagaCoordinator, execution_id: str) -> Any:
    """副作用存疑 → 记一笔"不知道"（UNRESOLVED）。"""
    return saga.record_unresolved(
        run_id=RUN,
        step_id=STEP,
        task_id=TASK,
        execution_id=execution_id,
        action=_compensable(RUN),
        reason="S-11: we do not know whether the side effect happened",
    )


class LedgerEventsContract:
    """两个实现跑同一段剧本，必须得到同一串事件（B-7）。

    子类只负责给出 `build_store()`。剧本本身写在这里，写一遍 ——
    抄成两份之后，"认领算不算一次变化"就会有两个答案，
    而下游只有一个消费者。
    """

    def build_store(self, sink: Any) -> Any:  # pragma: no cover —— 子类实现
        raise NotImplementedError

    def setUp(self) -> None:
        self.sink = InMemoryOutbox()
        self.store = self.build_store(self.sink)
        self.saga = SagaCoordinator(store=self.store)

    # ---------------------------------------------------------------- 工具
    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.sink.all()]

    @property
    def last(self) -> Any:
        events = self.sink.all()
        assert events, "账本改了，但一条事件都没有 —— 这正是空洞 232"
        return events[-1]

    # ---------------------------------------------------------------- 登记
    def test_recording_a_side_effect_is_announced(self) -> None:
        self.assertIsNotNone(_book(self.saga, "exec_1"))
        self.assertEqual(self.types, ["compensation.recorded"])
        self.assertEqual(self.last.payload["status"], "pending")
        self.assertEqual(self.last.payload["execution_id"], "exec_1")

    def test_booking_an_unknown_side_effect_is_announced(self) -> None:
        self.assertIsNotNone(_book_unknown(self.saga, "exec_1"))
        self.assertEqual(self.types, ["compensation.unresolved"])
        self.assertEqual(self.last.payload["status"], "unresolved")
        self.assertIn("S-11", self.last.payload["reason"])

    def test_an_action_with_no_undo_declaration_says_nothing(self) -> None:
        """对照：没有逆操作声明就没有账，也就没有事件。

        没有这条，"账本有事件"可能只是"每次 `record()` 都无脑发一条"。
        """
        from packages.agent_domain.intelligence.action import (
            Action,
            ActionType,
            RiskLevel,
        )

        self.assertIsNone(
            self.saga.record(
                run_id=RUN,
                step_id=STEP,
                task_id=TASK,
                execution_id="exec_1",
                action=Action(
                    run_id=RUN,
                    action_type=ActionType.TOOL_CALL,
                    payload={"tool": "read_only"},
                    risk_level=RiskLevel.LOW,
                ),
                result={},
                execution_status=ExecutionStatus.COMPLETED,
            )
        )
        self.assertEqual(self.types, [])

    # ---------------------------------------------------------------- 认领
    def test_claiming_is_announced(self) -> None:
        record = _book(self.saga, "exec_1")
        self.assertIsNotNone(self.store.claim(record.compensation_id))
        self.assertEqual(self.types[-1], "compensation.claimed")
        self.assertEqual(self.last.payload["status"], "running")

    def test_losing_a_claim_announces_nothing(self) -> None:
        """对照：没抢到就没有变化。抢不到是正常的，不该有事件。"""
        record = _book(self.saga, "exec_1")
        self.assertIsNotNone(self.store.claim(record.compensation_id))
        n = len(self.types)
        self.assertIsNone(self.store.claim(record.compensation_id))
        self.assertEqual(len(self.types), n)

    # ---------------------------------------------------------------- 撤销
    def test_a_successful_undo_is_announced(self) -> None:
        _book(self.saga, "exec_1")
        outcome = self.saga.compensate(RUN, executor=lambda _action: True)
        self.assertTrue(outcome.clean)
        self.assertEqual(
            self.types,
            ["compensation.recorded", "compensation.claimed", "compensation.compensated"],
        )

    def test_an_undo_that_fails_is_announced(self) -> None:
        """撤销不掉 = **没有人**能撤销它 —— 这是最该被下游看见的一条。"""
        _book(self.saga, "exec_1")
        outcome = self.saga.compensate(RUN, executor=lambda _action: False)
        self.assertFalse(outcome.clean)
        self.assertEqual(self.types[-1], "compensation.unresolved")
        self.assertEqual(self.last.payload["status"], "unresolved")
        self.assertIn("S-5", self.last.payload["reason"])

    # ---------------------------------------------------------------- 结案
    def test_closing_a_successful_run_is_announced(self) -> None:
        """S-16：副作用按预期保留。它也是账本的一次变化。"""
        _book(self.saga, "exec_1")
        self.assertEqual(self.saga.release(RUN), 1)
        self.assertEqual(self.types[-1], "compensation.not_needed")
        self.assertEqual(self.last.payload["status"], "not_needed")

    # ---------------------------------------------------------------- 改口
    def test_withdrawing_the_unknown_is_announced(self) -> None:
        """D-23 那一处改口 —— 本轮登记的起因，但不是唯一的发射点。"""
        _book_unknown(self.saga, "exec_1")
        self.assertIsNotNone(
            self.saga.amend_unresolved(
                execution_id="exec_1", reason="D-23: now we know it completed"
            )
        )
        self.assertEqual(self.types[-1], "compensation.amended")
        self.assertIn("D-23", self.last.payload["reason"])
        # 改口**不改状态**（S-15）—— payload 必须照实说
        self.assertEqual(self.last.payload["status"], "unresolved")

    def test_amending_a_row_that_is_not_there_announces_nothing(self) -> None:
        self.assertIsNone(
            self.saga.amend_unresolved(execution_id="nope", reason="D-23: x")
        )
        self.assertEqual(self.types, [])

    # ---------------------------------------------------------------- 人工重试
    def test_a_manual_retry_is_announced_as_reopened_not_recorded(self) -> None:
        """UNRESOLVED → PENDING 落地后也是 PENDING，但它**不是**一笔新账。

        叫 `recorded` 的话，下游按 compensation_id 去重就会把这次重试
        整个丢掉；不重去则"待撤销笔数"会凭空翻倍。
        """
        _book_unknown(self.saga, "exec_1")
        record = self.saga.reopen(self.store.get_by_execution("exec_1").compensation_id)
        self.assertIs(record.status, CompensationStatus.PENDING)
        self.assertEqual(self.types[-1], "compensation.reopened")
        self.assertNotIn(COMPENSATION_RECORDED, self.types[1:])

    # ---------------------------------------------------------------- 没变
    def test_a_write_that_changes_no_status_is_not_announced(self) -> None:
        """`touch()` 只动 attempts / updated_at。账本说的事实没变，就不该有事件。

        没有这条，"有事件"会退化成"每次 `save()` 都发一条" ——
        那会让"撤销了三次"看起来像"出现了三笔副作用"。
        """
        record = _book(self.saga, "exec_1")
        assert record is not None
        n = len(self.types)
        record.touch()
        self.store.save(record)
        self.assertEqual(len(self.types), n)

    # ---------------------------------------------------------------- X-15
    def test_the_stream_reads_on_its_own(self) -> None:
        """不查 PG，光看这一串事件就能说出账本发生了什么。"""
        _book(self.saga, "exec_1")
        self.saga.compensate(RUN, executor=lambda _action: False)
        self.saga.amend_unresolved(execution_id="exec_1", reason="D-23: it completed")

        for e in self.sink.all():
            self.assertEqual(e.aggregate_type, COMPENSATION_AGGREGATE)
            self.assertEqual(e.aggregate_id, e.payload["compensation_id"])
            self.assertEqual(e.payload["run_id"], RUN)
            self.assertEqual(e.payload["execution_id"], "exec_1")
            self.assertEqual(e.payload["task_id"], TASK)
            self.assertEqual(e.payload["step_id"], STEP)
            self.assertEqual(e.payload["tool"], "cancel_ticket")

        # aggregate_version 跟着账本的版本走（下游按它排序 / 去重）
        self.assertEqual([e.aggregate_version for e in self.sink.all()], [1, 2, 3, 4])


class InMemoryLedgerEventsTest(LedgerEventsContract, unittest.TestCase):
    def build_store(self, sink: Any) -> Any:
        return InMemoryCompensationStore(events=sink)


class PostgresLedgerEventsTest(LedgerEventsContract, unittest.TestCase):
    """PG 版跑在 sqlite 上的 PG 方言替身（schema 读 `infrastructure/postgres/` 原文）。"""

    def build_store(self, sink: Any) -> Any:
        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(self.conn.close)
        return PostgresCompensationStore(self.conn, events=sink)


# ---------------------------------------------------------------- B-7
class TheTwoImplementationsAgreeTest(unittest.TestCase):
    """内存账本与 PG 账本必须发出**同一种**事件。

    抄两份实现之后，"认领算不算一次变化"、"`amend` 带不带 reason"
    就会有两个答案 —— 而下游只有一个消费者。
    """

    def _script(self, store: Any) -> list[str]:
        saga = SagaCoordinator(store=store)
        record = _book(saga, "exec_1")
        assert record is not None
        saga.compensate(RUN, executor=lambda _action: False)
        saga.amend_unresolved(execution_id="exec_1", reason="D-23: it completed")
        saga.reopen(record.compensation_id)
        _book(saga, "exec_2")
        saga.release(RUN)
        return [e.event_type for e in store.events.all()]

    def test_the_same_script_produces_the_same_stream(self) -> None:
        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        # 两本账各自**新开**一本：同一段剧本要能在两本账上原样跑一遍，
        # 而不是"第二遍靠 S-2 判重跑出另一个结果"。
        memory = self._script(InMemoryCompensationStore(events=InMemoryOutbox()))
        pg = self._script(PostgresCompensationStore(conn, events=InMemoryOutbox()))

        self.assertEqual(memory, pg)
        self.assertEqual(
            memory,
            [
                "compensation.recorded",
                "compensation.claimed",
                "compensation.unresolved",
                "compensation.amended",
                "compensation.reopened",   # 人工拉回待办 → 它又"开着"了
                "compensation.recorded",
                # `release()` 收的是**所有**还开着的：exec_2 与刚被拉回的 exec_1
                "compensation.not_needed",
                "compensation.not_needed",
            ],
        )


# ---------------------------------------------------------------- X-3
class X3EventAndStateShareOneTransactionTest(unittest.TestCase):
    """事件与状态必须写进**同一个** outbox / 同一条连接。

    这一条在单测里只能验"形状"（PG 账本 → PG outbox，同一 conn）；
    "同一事务"那半句要真 PG 才能验，见
    `tests/integration/test_compensation_events_real_pg.py`。
    """

    def setUp(self) -> None:
        from packages.execution_kernel.adapters.postgres import PostgresOutboxStore
        from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

        self.conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(self.conn.close)
        self.outbox = PostgresOutboxStore(self.conn)
        self.store = PostgresCompensationStore(self.conn, events=self.outbox)
        self.saga = SagaCoordinator(store=self.store)

    def test_the_ledger_writes_into_the_outbox(self) -> None:
        self.assertIsNotNone(_book(self.saga, "exec_1"))
        pending = self.outbox.pending()
        self.assertEqual([e.event_type for e in pending], ["compensation.recorded"])
        self.assertEqual(pending[0].aggregate_type, "compensation")

    def test_the_whole_life_lands_in_the_outbox(self) -> None:
        _book(self.saga, "exec_1")
        self.saga.compensate(RUN, executor=lambda _action: True)
        self.assertEqual(
            [e.event_type for e in self.outbox.pending()],
            ["compensation.recorded", "compensation.claimed", "compensation.compensated"],
        )

    def test_an_outbox_on_another_connection_would_be_a_different_transaction(
        self,
    ) -> None:
        """对照：事件落点必须是**这个** conn 上的 outbox。

        这条断言"组合根传进来的 sink 会被用到"，而不是被丢掉 ——
        一旦丢掉，账本就回到"改了没人知道"。
        """
        self.assertIs(self.store.events, self.outbox)


class TheCompositionRootWiresItTest(unittest.TestCase):
    """装配点必须**连着** outbox 一起给出账本 —— 这是空洞 214 的形状。

    `apps/_bootstrap.py` 里四个 `build_*` 都要账本。挨个写一遍
    `PostgresCompensationStore(conn)` 就会有四个漏掉 `events=` 的机会，
    而漏掉不报错 —— 这一本账只是从此"改了没人知道"。
    所以那里收成一个 `pg_compensation_store()`，这里钉住它。
    """

    def test_the_ledger_gets_an_outbox_on_the_same_connection(self) -> None:
        from apps._bootstrap import pg_compensation_store

        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        store = pg_compensation_store(conn)
        self.assertIs(store.conn, conn)
        self.assertIs(store.events.conn, conn, "不同 conn = 不同事务（X-3）")

    def test_the_wired_up_ledger_actually_emits(self) -> None:
        from apps._bootstrap import pg_compensation_store

        conn = connect(schema_sql=load_schema_sql(*_SCHEMA))
        self.addCleanup(conn.close)
        store = pg_compensation_store(conn)
        saga = SagaCoordinator(store=store)
        self.assertIsNotNone(_book(saga, "exec_1"))

        from packages.execution_kernel.adapters.postgres import PostgresOutboxStore

        pending = PostgresOutboxStore(conn).pending()
        self.assertEqual([e.event_type for e in pending], ["compensation.recorded"])


# ---------------------------------------------------------------- 事件表本身
class TheEventTableTest(unittest.TestCase):
    """PR-34：宁可拒绝，不许编造。"""

    def test_an_unmodelled_transition_refuses_instead_of_staying_silent(self) -> None:
        """改了却叫不出名字，就抛 —— 静默跳过会让下游永远缺一条。"""
        with self.assertRaises(InvariantViolation):
            transition_event_type(
                CompensationStatus.PENDING, CompensationStatus.COMPENSATED
            )

    def test_a_transition_to_itself_is_not_a_change(self) -> None:
        self.assertIsNone(
            transition_event_type(
                CompensationStatus.PENDING, CompensationStatus.PENDING
            )
        )

    def test_every_legal_transition_has_a_name(self) -> None:
        """`_ALLOWED_TRANSITIONS` 里的六种，一个都不能漏。

        漏一种 = 那一次账本变化"改了 PG、没发事件" —— 正是本轮要闭合的空洞。
        """
        from packages.agent_domain.business.compensation import _ALLOWED_TRANSITIONS
        from packages.agent_runtime.compensation_events import TRANSITION_EVENTS

        for before, afters in _ALLOWED_TRANSITIONS.items():
            for after in afters:
                self.assertIn(
                    (before, after),
                    TRANSITION_EVENTS,
                    f"{before.value} → {after.value} 是合法迁移，却没有事件名",
                )

    def test_the_payload_says_what_the_row_says(self) -> None:
        record = _book(SagaCoordinator(store=InMemoryCompensationStore()), "exec_1")
        event = compensation_event(record, event_type=COMPENSATION_RECORDED)
        self.assertEqual(event.payload["status"], record.status.value)
        self.assertEqual(event.payload["reason"], record.reason)
        self.assertEqual(event.payload["attempts"], record.attempts)


# ---------------------------------------------------------------- 没接 outbox
class NoSinkTest(unittest.TestCase):
    """没传事件落点 = 这一本账不发事件。这是**显式**选择，不是"忘了接"。"""

    def test_the_ledger_still_works_without_a_sink(self) -> None:
        saga = SagaCoordinator(store=InMemoryCompensationStore())
        self.assertIsNotNone(_book(saga, "exec_1"))
        self.assertEqual(saga.release(RUN), 1)
        self.assertEqual(saga.compensate(RUN, executor=lambda _a: True).attempted, 0)


if __name__ == "__main__":
    unittest.main()
