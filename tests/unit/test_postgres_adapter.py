"""阶段 5：PostgreSQL Adapter 的语义测试。

重点不是"SQL 能跑通"，而是**不变量在数据库层面也成立**：
Domain 里的检查是"善意"，DB 约束才是"兜底"。两者都要有。

    E-6    UNIQUE(execution_id, attempt_no)
    E-8    CHECK(SUSPENDED ⇔ suspension_reason)
    E-13   UPDATE ... WHERE version = ? → rowcount = 0 即并发冲突
    E-19   UNIQUE(task_id)
    E-21   UNIQUE(idempotency_key)
    X-3    Outbox：append → pending → mark_published

跑在 sqlite 上的 PG 方言替身（tests/unit/sqlite_shim.py），
但 schema 直接读 `infrastructure/postgres/001_kernel.sql` 原文。
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta

from packages.agent_domain.errors import ConcurrentStateError, InvariantViolation
from packages.agent_domain.events.event import EXECUTION_CREATED, EXECUTION_RUNNING
from packages.agent_domain.execution import (
    Attempt,
    AttemptStatus,
    ErrorInfo,
    Execution,
    ExecutionStatus,
    FailureClass,
    Lease,
    Suspension,
    SuspensionReason,
)
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOutboxStore,
)
from packages.execution_kernel.inmemory import (
    InMemoryCancelSignals,
    ManualClock,
)
from packages.execution_kernel.kernel import ExecutionKernel

from .helpers import make_task
from .sqlite_shim import connect

T0 = ManualClock().now()


def _insert_execution_raw(conn, execution_id: str, task_id: str, **over) -> None:
    """绕过 Domain 直接写库 —— 用来证明**数据库自己**会拒绝非法状态。"""
    base = {
        "execution_id": execution_id,
        "task_id": task_id,
        "idempotency_key": over.pop("idempotency_key", execution_id),
        "execution_mode": "task",
        "status": over.pop("status", "PENDING"),
        "current_attempt_no": 0,
        "cancellation_requested": False,
        "version": 1,
    }
    base.update(over)
    cols = ", ".join(base)
    holes = ", ".join("%s" for _ in base)
    conn.cursor().execute(
        f"INSERT INTO executions ({cols}) VALUES ({holes})", tuple(base.values())
    )


class PgTestCase(unittest.TestCase):
    """统一收尾：关掉 sqlite 连接，避免 ResourceWarning 噪音。"""

    def tearDown(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()


class SchemaTest(PgTestCase):
    def test_schema_file_parses(self) -> None:
        conn = connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        tables = {r["name"] for r in cur.fetchall()}
        self.assertEqual(
            tables,
            {"tasks", "executions", "attempts", "kernel_checkpoints",
             "run_checkpoints", "outbox_events"},
        )

    def test_status_check_constraint(self) -> None:
        conn = connect()
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_execution_raw(conn, "exe_bad", "task_bad", status="NOT_A_STATUS")


class ExecutionRepositoryTest(PgTestCase):
    def setUp(self) -> None:
        self.conn = connect()
        self.repo = PostgresExecutionRepository(self.conn)

    # ---------------------------------------------------------------- 读写
    def test_add_and_get_roundtrip(self) -> None:
        task = make_task()
        execution = Execution(task_id=task.task_id)
        self.repo.add(execution)

        loaded = self.repo.get(execution.execution_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.execution_id, execution.execution_id)
        self.assertEqual(loaded.task_id, task.task_id)
        self.assertEqual(loaded.status, ExecutionStatus.PENDING)
        self.assertEqual(loaded.idempotency_key, execution.execution_id)  # E-21
        self.assertEqual(loaded.version, 1)
        self.assertIsNone(loaded.lease)

    def test_get_by_task(self) -> None:
        task = make_task()
        execution = Execution(task_id=task.task_id)
        self.repo.add(execution)
        self.assertIsNotNone(self.repo.get_by_task(task.task_id))
        self.assertIsNone(self.repo.get_by_task("task_missing"))

    def test_save_persists_lease_and_suspension(self) -> None:
        execution = Execution(task_id=make_task().task_id)
        self.repo.add(execution)

        with execution.mutating():
            execution.status = ExecutionStatus.SUSPENDED
            execution.suspension = Suspension(
                reason=SuspensionReason.HUMAN_APPROVAL,
                wait_condition={"approver": "ops"},
                suspended_at=T0,
            )
            execution.version = 2
        self.repo.save(execution)

        loaded = self.repo.get(execution.execution_id)
        assert loaded is not None
        self.assertEqual(loaded.status, ExecutionStatus.SUSPENDED)
        assert loaded.suspension is not None
        self.assertEqual(loaded.suspension.reason, SuspensionReason.HUMAN_APPROVAL)
        self.assertEqual(loaded.suspension.wait_condition, {"approver": "ops"})
        self.assertEqual(loaded.version, 2)

    def test_explicit_expected_version_overrides_previous_version(self) -> None:
        """显式传 expected_version 时以调用方说的为准（跨服务写入场景）。"""
        execution = Execution(task_id=make_task().task_id)
        self.repo.add(execution)                       # DB version = 1

        with execution.mutating():
            execution.version = 2
        self.repo.save(execution, expected_version=1)  # "我基于 1 写" → 成立

        with execution.mutating():
            execution.version = 3
        with self.assertRaises(ConcurrentStateError):
            self.repo.save(execution, expected_version=1)  # DB 已经是 2 了

    # ---------------------------------------------------------------- E-13
    def test_stale_save_raises_concurrent_state_error(self) -> None:
        execution = Execution(task_id=make_task().task_id)
        self.repo.add(execution)

        stale = self.repo.get(execution.execution_id)  # version = 1
        fresh = self.repo.get(execution.execution_id)  # version = 1

        with fresh.mutating():
            fresh.version = 2
        self.repo.save(fresh)                          # DB → 2

        with stale.mutating():
            stale.version = 2
        with self.assertRaises(ConcurrentStateError) as ctx:
            self.repo.save(stale)                      # 还以为 DB 是 1
        self.assertIn("E-13", str(ctx.exception))

    def test_save_after_successful_write_updates_previous_version(self) -> None:
        execution = Execution(task_id=make_task().task_id)
        self.repo.add(execution)
        self.assertEqual(execution.previous_version, 1)

        with execution.mutating():
            execution.version = 2
        self.repo.save(execution)
        self.assertEqual(execution.previous_version, 2)

        with execution.mutating():
            execution.version = 3
        self.repo.save(execution)  # 不再冲突
        self.assertEqual(execution.previous_version, 3)

    # ---------------------------------------------------------------- 扫描
    def test_list_by_status(self) -> None:
        for _ in range(3):
            self.repo.add(Execution(task_id=make_task().task_id))
        self.assertEqual(len(self.repo.list_by_status(ExecutionStatus.PENDING)), 3)
        self.assertEqual(len(self.repo.list_by_status(ExecutionStatus.RUNNING)), 0)

    def test_list_with_expired_lease_only_returns_expired(self) -> None:
        expired = Execution(task_id=make_task().task_id)
        self.repo.add(expired)
        with expired.mutating():
            expired.status = ExecutionStatus.RUNNING
            expired.lease = Lease(
                execution_id=expired.execution_id,
                attempt_no=1,
                worker_id="w1",
                fencing_token=1,
                acquired_at=T0,
                expires_at=T0 + timedelta(seconds=30),
                heartbeat_at=T0,
            )
            expired.version = 2
        self.repo.save(expired)

        alive = Execution(task_id=make_task().task_id)
        self.repo.add(alive)
        with alive.mutating():
            alive.status = ExecutionStatus.RUNNING
            alive.lease = Lease(
                execution_id=alive.execution_id,
                attempt_no=1,
                worker_id="w2",
                fencing_token=1,
                acquired_at=T0,
                expires_at=T0 + timedelta(hours=1),
                heartbeat_at=T0,
            )
            alive.version = 2
        self.repo.save(alive)

        pending = Execution(task_id=make_task().task_id)
        self.repo.add(pending)

        found = self.repo.list_with_expired_lease(T0 + timedelta(minutes=5))
        self.assertEqual([e.execution_id for e in found], [expired.execution_id])


class ConstraintTest(PgTestCase):
    """Domain 会拒绝，DB 也必须拒绝 —— 两者缺一不可。"""

    def setUp(self) -> None:
        self.conn = connect()

    def test_e19_one_execution_per_task(self) -> None:
        _insert_execution_raw(self.conn, "exe_a", "task_1")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_execution_raw(self.conn, "exe_b", "task_1")

    def test_e21_idempotency_key_unique(self) -> None:
        _insert_execution_raw(self.conn, "exe_a", "task_1")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_execution_raw(self.conn, "exe_b", "task_2", idempotency_key="exe_a")

    def test_e8_suspended_requires_reason(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_execution_raw(self.conn, "exe_a", "task_1", status="SUSPENDED")

    def test_e8_reason_requires_suspended(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_execution_raw(
                self.conn, "exe_a", "task_1", status="PENDING", suspension_reason="timer"
            )

    def test_e8_suspended_with_reason_is_allowed(self) -> None:
        _insert_execution_raw(
            self.conn, "exe_a", "task_1", status="SUSPENDED", suspension_reason="timer"
        )
        row = self.conn.cursor().execute(
            "SELECT status FROM executions WHERE execution_id = %s", ("exe_a",)
        ).fetchone()
        self.assertEqual(row["status"], "SUSPENDED")

    def test_e6_attempt_no_unique_per_execution(self) -> None:
        _insert_execution_raw(self.conn, "exe_a", "task_1")
        cur = self.conn.cursor()
        sql = """
            INSERT INTO attempts (attempt_id, execution_id, attempt_no, status, version)
            VALUES (%s, %s, %s, %s, %s)
        """
        cur.execute(sql, ("att_1", "exe_a", 1, "RUNNING", 1))
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(sql, ("att_2", "exe_a", 1, "FAILED", 1))
        cur.execute(sql, ("att_3", "exe_a", 2, "RUNNING", 1))  # 不同 attempt_no 允许

    def test_e6_attempt_requires_execution(self) -> None:
        cur = self.conn.cursor()
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO attempts (attempt_id, execution_id, attempt_no, status, version)"
                " VALUES (%s, %s, %s, %s, %s)",
                ("att_1", "exe_missing", 1, "RUNNING", 1),
            )


class AttemptRepositoryTest(PgTestCase):
    def setUp(self) -> None:
        self.conn = connect()
        self.executions = PostgresExecutionRepository(self.conn)
        self.attempts = PostgresAttemptRepository(self.conn)
        self.execution = Execution(task_id=make_task().task_id)
        self.executions.add(self.execution)

    def test_save_and_get_roundtrip(self) -> None:
        attempt = Attempt(
            execution_id=self.execution.execution_id,
            attempt_no=1,
            status=AttemptStatus.RUNNING,
            started_at=T0,
        )
        self.attempts.save(attempt)

        loaded = self.attempts.get(self.execution.execution_id, 1)
        assert loaded is not None
        self.assertEqual(loaded.attempt_id, attempt.attempt_id)
        self.assertEqual(loaded.status, AttemptStatus.RUNNING)
        self.assertIsNone(loaded.error)

    def test_error_info_is_persisted_with_failure_class(self) -> None:
        attempt = Attempt(
            execution_id=self.execution.execution_id,
            attempt_no=1,
            status=AttemptStatus.FAILED,
            started_at=T0,
            finished_at=T0 + timedelta(seconds=1),
            error=ErrorInfo(
                code="TIMEOUT", message="tool timed out", failure_class=FailureClass.TRANSIENT
            ),
        )
        self.attempts.save(attempt)

        loaded = self.attempts.get(self.execution.execution_id, 1)
        assert loaded is not None
        assert loaded.error is not None
        self.assertEqual(loaded.error.code, "TIMEOUT")
        self.assertEqual(loaded.error.failure_class, FailureClass.TRANSIENT)

    def test_save_is_upsert_not_duplicate(self) -> None:
        attempt = Attempt(
            execution_id=self.execution.execution_id,
            attempt_no=1,
            status=AttemptStatus.RUNNING,
            started_at=T0,
        )
        self.attempts.save(attempt)
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.finished_at = T0 + timedelta(seconds=2)
        self.attempts.save(attempt)

        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE execution_id = %s AND attempt_no = %s",
            (self.execution.execution_id, 1),
        )
        self.assertEqual(cur.fetchone()["n"], 1)
        self.assertEqual(
            self.attempts.get(self.execution.execution_id, 1).status,
            AttemptStatus.SUCCEEDED,
        )

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.attempts.get(self.execution.execution_id, 9))


class OutboxTest(PgTestCase):
    def setUp(self) -> None:
        self.conn = connect()
        self.outbox = PostgresOutboxStore(self.conn)

    def test_pending_and_mark_published(self) -> None:
        from packages.agent_domain.events.event import new_event

        events = [
            new_event(
                aggregate_type="execution",
                aggregate_id="exe_1",
                event_type=EXECUTION_CREATED,
                payload={"status": "PENDING"},
                aggregate_version=1,
            ),
            new_event(
                aggregate_type="execution",
                aggregate_id="exe_1",
                event_type=EXECUTION_RUNNING,
                payload={"worker": "w1"},
                aggregate_version=2,
            ),
        ]
        self.outbox.append(events)

        pending = self.outbox.pending()
        self.assertEqual([e.event_type for e in pending],
                         [EXECUTION_CREATED, EXECUTION_RUNNING])
        self.assertEqual(pending[0].payload, {"status": "PENDING"})

        self.outbox.mark_published([pending[0].event_id])
        remaining = self.outbox.pending()
        self.assertEqual([e.event_type for e in remaining], [EXECUTION_RUNNING])

        self.outbox.mark_published([e.event_id for e in remaining])
        self.assertEqual(self.outbox.pending(), [])

    def test_mark_published_empty_is_noop(self) -> None:
        self.outbox.mark_published([])  # 不应抛错


class KernelOnPostgresTest(PgTestCase):
    """同一个 Kernel，换掉 Repository 实现 —— 证明端口抽象是真的。"""

    def setUp(self) -> None:
        self.conn = connect()
        self.clock = ManualClock()
        self.repo = PostgresExecutionRepository(self.conn)
        self.attempts = PostgresAttemptRepository(self.conn)
        self.outbox = PostgresOutboxStore(self.conn)

        def new_kernel() -> ExecutionKernel:
            """同一个库上的"另一个进程"。"""
            return ExecutionKernel(
                repository=PostgresExecutionRepository(self.conn),
                attempts=PostgresAttemptRepository(self.conn),
                outbox=PostgresOutboxStore(self.conn),
                clock=self.clock,
                cancel_signals=InMemoryCancelSignals(),
            )

        self.new_kernel = new_kernel
        self.kernel = new_kernel()

    def test_full_lifecycle_persisted(self) -> None:
        task = make_task()
        execution = self.kernel.submit(task)
        attempt, lease = self.kernel.claim(execution.execution_id, worker_id="w1")
        self.assertEqual(attempt.attempt_no, 1)
        self.kernel.complete(execution.execution_id, token=lease.fencing_token,
                             result={"ok": True})

        # 换一个 Kernel（= 另一个进程 / 重启后），状态必须能从 PG 读回来
        restarted = self.new_kernel()
        self.assertEqual(
            restarted.status_of(execution.execution_id), ExecutionStatus.COMPLETED
        )
        # Attempt 历史也必须读得回来（E-4）
        self.assertEqual(
            [a.attempt_no for a in self.attempts.list_by_execution(execution.execution_id)],
            [1],
        )
        self.assertEqual(
            self.attempts.get(execution.execution_id, 1).status, AttemptStatus.SUCCEEDED
        )

        persisted = self.repo.get(execution.execution_id)
        assert persisted is not None
        self.assertEqual(persisted.status, ExecutionStatus.COMPLETED)
        self.assertEqual(persisted.current_attempt_no, 1)
        self.assertIsNone(persisted.lease)  # 完成后 Lease 必须释放

    def test_events_land_in_outbox(self) -> None:
        execution = self.kernel.submit(make_task())
        self.kernel.claim(execution.execution_id, worker_id="w1")
        pending = self.outbox.pending()
        # Lease 事件的 aggregate 是 lease_id，所以按 aggregate_type 分流断言
        exec_events = [e for e in pending if e.aggregate_type == "execution"]
        types = [e.event_type for e in exec_events]
        self.assertIn(EXECUTION_CREATED, types)
        self.assertIn(EXECUTION_RUNNING, types)          # X-3：每次状态变更都有事件
        self.assertTrue(all(e.aggregate_id == execution.execution_id for e in exec_events))

    def test_duplicate_submit_rejected_by_e19(self) -> None:
        task = make_task()
        self.kernel.submit(task)
        with self.assertRaises(InvariantViolation):
            self.kernel.submit(task)

    def test_concurrent_complete_raises_e13(self) -> None:
        """两个 Kernel 同时拿着同一个 Execution：后写的那个必须失败。"""
        execution = self.kernel.submit(make_task())
        _, lease = self.kernel.claim(execution.execution_id, worker_id="w1")

        other = self.new_kernel()
        # 关键：另一个 Kernel 必须在第一个写入**之前**就把它读进内存（模拟并发读）
        self.assertEqual(other.status_of(execution.execution_id), ExecutionStatus.RUNNING)

        self.kernel.complete(execution.execution_id, token=lease.fencing_token)
        with self.assertRaises(ConcurrentStateError):
            other.complete(execution.execution_id, token=lease.fencing_token)

    # ---------------------------------------------------------------- E-25
    def test_e25_resume_is_two_transitions_but_one_write(self) -> None:
        """E-25：`resume()` = SUSPENDED → PENDING → RUNNING，两次跃迁、一次落库。

        这条用例存在的理由：乐观锁比的是"**我读到的**版本"，
        不是"我上次内存自增前的版本"。自增 2 次只落 1 次库，
        拿 `previous_version` 去比会在**根本没有并发**的情况下自己报 E-13。
        """
        execution = self.kernel.submit(make_task())
        self.kernel.claim(execution.execution_id, worker_id="w1")
        self.kernel.suspend(
            execution.execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approval_id": "apr_1"},
        )
        before = self.repo.get(execution.execution_id)
        assert before is not None
        self.assertEqual(before.version, 3)

        self.kernel.resume(execution.execution_id, worker_id="w1")

        after = self.repo.get(execution.execution_id)
        assert after is not None
        self.assertEqual(after.status, ExecutionStatus.RUNNING)
        # 两次跃迁 → 版本号跳了 2，但只对应**一次** UPDATE
        self.assertEqual(after.version, 5)
        self.assertEqual(
            [a.attempt_no for a in self.attempts.list_by_execution(execution.execution_id)],
            [1, 2],
        )

    def test_e25_the_control_previous_version_would_not_have_worked(self) -> None:
        """对照：证明上面那条不是白测的 —— 换成 previous_version 就会炸。

        这条不是"多写一份保险"，而是把**修之前的行为**留在仓库里，
        免得哪天有人把 `store_version` 改回 `previous_version` 而没人发现。
        """
        execution = self.kernel.submit(make_task())
        self.kernel.claim(execution.execution_id, worker_id="w1")
        self.kernel.suspend(
            execution.execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approval_id": "apr_1"},
        )

        loaded = self.repo.get(execution.execution_id)
        assert loaded is not None
        self.assertEqual(loaded.store_version, 3)      # 存储边界 = 3

        # 模拟"一次操作内两次自增"（resume 的真实形态）
        with loaded.mutating():
            loaded.version = 4
        with loaded.mutating():
            loaded.version = 5

        stale_expected = loaded.previous_version       # 4 —— 内存自增前，库里**没有** 4
        self.assertEqual(loaded.store_version, 3)      # 存储边界 —— 库里是 3

        self.repo.save(loaded)                         # 用 store_version：成立
        with self.assertRaises(ConcurrentStateError):
            self.repo.save(loaded, expected_version=stale_expected)

    def test_e25_stale_writer_is_still_caught(self) -> None:
        """E-25 不能顺手把 E-13 削弱：真并发照样要报错。"""
        execution = self.kernel.submit(make_task())
        self.kernel.claim(execution.execution_id, worker_id="w1")
        self.kernel.suspend(
            execution.execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approval_id": "apr_1"},
        )

        other = self.new_kernel()
        self.assertEqual(other.status_of(execution.execution_id), ExecutionStatus.SUSPENDED)

        self.kernel.resume(execution.execution_id, worker_id="w1")
        with self.assertRaises(ConcurrentStateError):
            other.resume(execution.execution_id, worker_id="w2")

    def test_lease_expiry_visible_to_repository_scan(self) -> None:
        execution = self.kernel.submit(make_task())
        self.kernel.claim(execution.execution_id, worker_id="w1",
                          ttl=timedelta(seconds=30))
        self.assertEqual(self.repo.list_with_expired_lease(self.clock.now()), [])

        self.clock.advance(timedelta(seconds=31))
        found = self.repo.list_with_expired_lease(self.clock.now())
        self.assertEqual([e.execution_id for e in found], [execution.execution_id])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
