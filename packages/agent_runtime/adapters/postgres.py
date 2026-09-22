"""RunSnapshot 的 PostgreSQL 实现（M20）。

表结构见 `infrastructure/postgres/004_run_snapshots.sql`。

**为什么这块必须持久，不能省：**

快照存在的唯一理由就是**活过进程重启**。放内存里等于没写 ——
服务一重启，待批列表还在（M19 保证了），但 Run 没了，
于是人看得见待批事项却点不动它（404 `RUN_NOT_FOUND`）。

**为什么整块状态存 JSONB：**

State / Plan / Observation 是 Intelligence 层的对象，形状随版本演进。
拆成列等于让 DB schema 去钉死领域模型 —— 那正是 §14 想避免的
"用表结构决定领域模型"。这里 DB 只负责原样存、原样读。

但会被查询的字段（`run_id` / `status` / `step_count` / `pending_approval_id`）
必须提成列：恢复要按 `run_id` 取最新一条，运维要看"哪些 Run 挂在等审批"。
全塞 JSONB 里就只能全表扫 JSON —— 那是把热路径建在最慢的地方。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from packages.agent_domain.business.compensation import (
    CompensationRecord,
    CompensationStatus,
)
from packages.agent_domain.business.snapshot import (
    RunSnapshot,
    action_from_dict,
    action_to_dict,
    parse_dt,
)
from packages.agent_domain.errors import InvariantViolation

from ..cancellation import DEFAULT_CANCELLATION_GRACE, RunCancellation
from ..cancellation_events import emit as _emit_cancellation_event
from ..compensation_events import (
    COMPENSATION_AMENDED,
    COMPENSATION_UPGRADED,
    emit,
    status_event_type,
    transition_event_type,
)
from ..delegation import (
    DEFAULT_CHILD_WAIT_TIMEOUT,
    ChildRunHandle,
    ChildRunKind,
    _resolve_ceiling,
    freeze_wait_deadline,
)

# 009：R-6 —— 挂起可能是"在等子 Run"，不只是"在等人"
SELECT_SNAPSHOT = """
SELECT snapshot_id, run_id, agent_id, status, step_count, consecutive_denials,
       pending_approval_id, pending_child_id, current_step_id, state, steps,
       spent, trace, progress, reason, created_at
  FROM run_snapshots
"""

INSERT_SNAPSHOT = """
INSERT INTO run_snapshots (
    snapshot_id, run_id, agent_id, status, step_count, consecutive_denials,
    pending_approval_id, pending_child_id, current_step_id, state, steps,
    spent, trace, progress, reason, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _load(raw: Any, fallback: Any) -> Any:
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _row_to_snapshot(row: Mapping[str, Any]) -> RunSnapshot:
    return RunSnapshot(
        snapshot_id=row["snapshot_id"],
        run_id=row["run_id"],
        agent_id=row["agent_id"] or "",
        status=row["status"],
        state=_load(row["state"], {}),
        steps=tuple(_load(row["steps"], [])),
        current_step_id=row["current_step_id"] or "",
        step_count=int(row["step_count"] or 0),
        consecutive_denials=int(row["consecutive_denials"] or 0),
        pending_approval_id=row["pending_approval_id"],
        # R-6：等子 Run 与等审批同等 —— 都得还原，否则恢复出来的 Run 叫不醒
        pending_child_id=row["pending_child_id"],
        spent=_load(row["spent"], {}),
        trace=tuple(_load(row["trace"], [])),
        # R-7（M86）：注入的 Intelligence 实现自述的进度。缺列的行（018 之前）
        # 走 `_load` 的 fallback → `{}`，语义正好是"这份快照没带走进度"。
        progress=_load(row["progress"], {}),
        reason=row["reason"] or "",
        created_at=parse_dt(row["created_at"]) or datetime.now(),
    )


# ---------------------------------------------------------------- 补偿账本
SELECT_COMPENSATION = """
SELECT compensation_id, run_id, step_id, task_id, execution_id, action_type,
       tool, args, description, status, reason, attempts,
       created_at, updated_at, version
  FROM compensations
"""

INSERT_COMPENSATION = """
INSERT INTO compensations (
    compensation_id, run_id, step_id, task_id, execution_id, action_type,
    tool, args, description, status, reason, attempts,
    created_at, updated_at, version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# S-4：认领必须是**一次** UPDATE。rowcount = 0 就是没抢到。
UPDATE_CLAIM_COMPENSATION = """
UPDATE compensations
   SET status = %s, attempts = attempts + 1, updated_at = %s, version = version + 1
 WHERE compensation_id = %s AND status = 'pending'
"""

UPDATE_COMPENSATION = """
UPDATE compensations
   SET status = %s, reason = %s, attempts = %s, updated_at = %s, version = %s
 WHERE compensation_id = %s AND version = %s
"""

#: X-15 要求"状态**真的**变了"才发事件，而"变没变"要拿**改之前**那一行比。
#: 旧状态只能靠这一句读 —— PG 的 `RETURNING` 只给新值。
#:
#: 两个语句两个快照，为什么仍然安全：两次都按**同一个 `version`** 取值。
#: `version` 只增不回退，所以"版本 X"在时间上是唯一的 ——
#: 读到了版本 X、而 UPDATE 又**成功**匹配到版本 X，中间就不可能有人改过
#: （改过的话版本就不是 X 了，UPDATE 会匹配 0 行并抛 E-13）。
#: 换句话说：并发不是靠"读得早"挡住的，是靠 `WHERE version = ?` 挡住的，
#: 这里读到的旧状态只是**描述**那次成功覆盖，不参与判胜负。
SELECT_COMPENSATION_STATUS_AT = """
SELECT status FROM compensations WHERE compensation_id = %s AND version = %s
"""

#: D-25：迟到的结果把"撤销不了"变成"待撤销"。
#:
#: 三个条件**全部**都是判据本身，不是"顺手加个条件"：
#:
#:     status = 'unresolved'   只动还开着的账（已处置的是历史，S-14）
#:     args = '{}'::jsonb      只动**当初因为缺撤销参数**才记成 UNRESOLVED 的那一行。
#:                             `args` 非空 = 参数一直都在，它是撤销动作跑失败了
#:                             （S-5/S-6），要不要重试是人的决定
#:     version = version + 1   留版本号线索（E-25）
#:
#: 与 S-4 / D-23 同一形状：判胜负靠 rowcount，不靠"先读一下"。
UPDATE_UPGRADE_COMPENSATION = """
UPDATE compensations
   SET args = %s, status = 'pending', reason = '', updated_at = %s,
       version = version + 1
 WHERE execution_id = %s AND status = 'unresolved' AND args = '{}'::jsonb
"""

#: D-23：收回"不知道"。
#:
#: `status = 'unresolved'` 这一半是**判据本身**，不是"顺手加个条件"：
#: 已经处置过的行（compensated / not_needed / 人工拉回的 pending）是历史，
#: 改它就是拿今天查明的真相去涂改昨天的账（S-14 同族）。
#:
#: 与 S-4 同一形状：判胜负靠 rowcount，不靠"先读一下状态"。
#: 两个进程同时补同一行时，两条 UPDATE 都会成功（它仍是 unresolved），
#: 但写进去的是**同一个真相** —— 幂等，不需要抢锁。
UPDATE_AMEND_COMPENSATION_REASON = """
UPDATE compensations
   SET reason = %s, updated_at = %s, version = version + 1
 WHERE execution_id = %s AND status = 'unresolved'
"""


def _row_to_compensation(row: Mapping[str, Any]) -> "CompensationRecord":
    return CompensationRecord(
        compensation_id=row["compensation_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        task_id=row["task_id"],
        execution_id=row["execution_id"],
        action_type=row["action_type"],
        tool=row["tool"],
        args=_load(row["args"], {}),
        description=row["description"],
        status=CompensationStatus(row["status"]),
        reason=row["reason"] or "",
        attempts=int(row["attempts"] or 0),
        created_at=parse_dt(row["created_at"]) or datetime.now(),
        updated_at=parse_dt(row["updated_at"]) or datetime.now(),
        version=int(row["version"] or 1),
    )


class PostgresRunSnapshotStore:
    """`RunSnapshotStore` 的 PostgreSQL 实现。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, snapshot: RunSnapshot) -> None:
        cur = self.conn.cursor()
        cur.execute(
            INSERT_SNAPSHOT,
            (
                snapshot.snapshot_id,
                snapshot.run_id,
                snapshot.agent_id,
                snapshot.status,
                snapshot.step_count,
                snapshot.consecutive_denials,
                snapshot.pending_approval_id,
                snapshot.pending_child_id,
                snapshot.current_step_id,
                _json(snapshot.state),
                _json(list(snapshot.steps)),
                _json(snapshot.spent),
                _json(list(snapshot.trace)),
                _json(snapshot.progress),
                snapshot.reason,
                snapshot.created_at,
            ),
        )

    def latest(self, run_id: str) -> RunSnapshot | None:
        cur = self.conn.cursor()
        cur.execute(
            SELECT_SNAPSHOT + " WHERE run_id = %s ORDER BY created_at DESC, snapshot_id DESC",
            (run_id,),
        )
        row = cur.fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def list_for(self, run_id: str) -> Sequence[RunSnapshot]:
        cur = self.conn.cursor()
        cur.execute(
            SELECT_SNAPSHOT + " WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        )
        return [_row_to_snapshot(r) for r in cur.fetchall()]


# ================================================================ 补偿账本（M10）


class PostgresCompensationStore:
    """`CompensationStore` 的 PostgreSQL 实现（表见 005_compensations.sql）。

    **为什么这一块必须持久：**
    账本丢了 → 副作用还在，但没人知道要撤销 —— 按 A-12 的判据这是**变错**，不是变慢。

    **S-2 的物理保证：** `UNIQUE(execution_id)`。
    约定层面"一条副作用一条记录"是守不住的（两个 Coordinator 各自扫一遍就重了），
    约束层面守得住 —— 重了就是插入失败，而不是撤销两次。

    **S-4 的物理保证：** `claim()` 是 `UPDATE ... WHERE status='pending'`，
    rowcount = 0 就是没抢到。与 A-11 完全同源：
    "先读一下是不是 PENDING 再写"在两个进程同时到达时两边都会通过。
    """

    def __init__(self, conn: Any, events: Any | None = None) -> None:
        """`events` 是事件落点（X-3：必须与状态写入**同一事务**）。

        组合根必须传 `PostgresOutboxStore(conn)` —— **同一个 conn**。
        传另一个连接上的 outbox，就等于把"这一行改了"和"通知下游"
        放进两个事务：第一个提交、第二个回滚，账本就又变回"改了没人知道"。
        """
        self.conn = conn
        self.events = events

    # ---------------------------------------------------------------- 写
    def add(self, record: CompensationRecord) -> None:
        cur = self.conn.cursor()
        cur.execute(
            INSERT_COMPENSATION,
            (
                record.compensation_id,
                record.run_id,
                record.step_id,
                record.task_id,
                record.execution_id,
                record.action_type,
                record.tool,
                _json(dict(record.args)),
                record.description,
                record.status.value,
                record.reason,
                record.attempts,
                record.created_at,
                record.updated_at,
                record.version,
            ),
        )
        object.__setattr__(record, "_store_version", record.version)
        emit(self.events, record, event_type=status_event_type(record.status))

    def claim(self, compensation_id: str) -> CompensationRecord | None:
        """S-4：原子认领。抢不到返回 `None`（不是抛异常 —— 抢不到是正常的）。"""
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_CLAIM_COMPENSATION,
            ("running", datetime.now(timezone.utc), compensation_id),
        )
        if cur.rowcount == 0:
            return None
        record = self.get(compensation_id)
        if record is not None:
            emit(
                self.events,
                record,
                event_type=transition_event_type(
                    CompensationStatus.PENDING, CompensationStatus.RUNNING
                ),
            )
        return record

    def save(self, record: CompensationRecord) -> None:
        cur = self.conn.cursor()
        before = self._status_at(
            record.compensation_id, record.store_version
        )
        cur.execute(
            UPDATE_COMPENSATION,
            (
                record.status.value,
                record.reason,
                record.attempts,
                record.updated_at,
                record.version,
                record.compensation_id,
                record.store_version,
            ),
        )
        if cur.rowcount == 0:
            from packages.agent_domain.errors import ConcurrentStateError

            raise ConcurrentStateError(
                f"E-13: concurrent update on compensation {record.compensation_id} "
                f"(expected version {record.store_version})"
            )
        object.__setattr__(record, "_store_version", record.version)
        if before is not None:
            emit(
                self.events,
                record,
                event_type=transition_event_type(before, record.status),
            )

    def _status_at(
        self, compensation_id: str, version: int
    ) -> CompensationStatus | None:
        """写回前那一行是什么状态（见 `SELECT_COMPENSATION_STATUS_AT`）。

        返回 `None` = 这一行现在不在版本 `version` 上 —— 接下来的 UPDATE
        必然匹配 0 行并抛 E-13，所以"没有旧状态"不会变成"没有事件"，
        它变成一次失败。
        """
        cur = self.conn.cursor()
        cur.execute(
            SELECT_COMPENSATION_STATUS_AT, (compensation_id, version)
        )
        row = cur.fetchone()
        return CompensationStatus(row["status"]) if row is not None else None

    def upgrade_to_compensable(
        self, execution_id: str, *, args: Mapping[str, Any], reason: str
    ) -> CompensationRecord | None:
        """D-25：把一条"没有撤销参数"的 UNRESOLVED 升级成 PENDING。

        返回 `None` 的三种情形（都由 WHERE 判定，不由 Python `if` 判定）：

            · 这条 Execution 没有账本行
            · 这一行已经处置过了（不是 UNRESOLVED）
            · 这一行的 `args` **非空** —— 它不是因为缺参数才撤销不了的

        第三种尤其重要：那种行是"撤销动作跑失败了"（S-5/S-6），
        真相到达不构成自动重试的理由（S-14 唯一例外是**人工**拉回）。

        `reason` 只进事件，不进这一行：PENDING 的 `reason` 按约定留空
        （见 `CompensationRecord.become_compensable`）。
        """
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_UPGRADE_COMPENSATION,
            (
                _json(dict(args)),
                datetime.now(timezone.utc),
                execution_id,
            ),
        )
        if cur.rowcount == 0:
            return None
        record = self.get_by_execution(execution_id)
        if record is not None:
            # 行上的 `reason` 按约定留空（PENDING 不带理由），
            # 完整来龙去脉走事件 payload —— 那是 M40 刚建好的通道。
            emit(
                self.events, record,
                event_type=COMPENSATION_UPGRADED, reason=reason,
            )
        return record

    def amend_reason(
        self, execution_id: str, *, reason: str
    ) -> CompensationRecord | None:
        """D-23：只改仍然 UNRESOLVED 的那一行（判据在 SQL 的 WHERE 里）。"""
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_AMEND_COMPENSATION_REASON,
            (reason, datetime.now(timezone.utc), execution_id),
        )
        if cur.rowcount == 0:
            return None
        record = self.get_by_execution(execution_id)
        if record is not None:
            emit(self.events, record, event_type=COMPENSATION_AMENDED)
        return record

    # ---------------------------------------------------------------- 读
    def get(self, compensation_id: str) -> CompensationRecord | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_COMPENSATION + " WHERE compensation_id = %s", (compensation_id,))
        row = cur.fetchone()
        return _row_to_compensation(row) if row is not None else None

    def get_by_execution(self, execution_id: str) -> CompensationRecord | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_COMPENSATION + " WHERE execution_id = %s", (execution_id,))
        row = cur.fetchone()
        return _row_to_compensation(row) if row is not None else None

    def open_for(self, run_id: str) -> Sequence[CompensationRecord]:
        """S-3：倒序返回未完成的（后发生的先撤销）。"""
        cur = self.conn.cursor()
        cur.execute(
            SELECT_COMPENSATION
            + " WHERE run_id = %s AND status IN ('pending','running')"
            " ORDER BY created_at DESC, compensation_id DESC",
            (run_id,),
        )
        return [_row_to_compensation(r) for r in cur.fetchall()]

    def unresolved_for(self, run_id: str) -> Sequence[CompensationRecord]:
        cur = self.conn.cursor()
        cur.execute(
            SELECT_COMPENSATION
            + " WHERE run_id = %s AND status = 'unresolved' ORDER BY created_at",
            (run_id,),
        )
        return [_row_to_compensation(r) for r in cur.fetchall()]


# ================================================================ 子 Run 派生（M26）
SELECT_CHILD_RUN = """
SELECT child_run_id, kind, parent_run_id, parent_execution_id, parent_task_id,
       target, action, status, spawned_at, result, completed_at, delivered_at,
       cancel_requested_at, cancel_reason, cancel_requested_by,
       wait_until, wait_expired_at
  FROM child_runs
"""

#: D-1 的落点：`ON CONFLICT ... DO NOTHING` 让"绑定"是一个**原子**动作。
#:
#: 内存版 `ChildRunRegistry.bind()` 写的是"先查再派" —— 那在单进程里没问题，
#: 但在两个进程同时派生时会双双通过（A-11 与 S-4 是同一个陷阱的另外两个副本：
#: "先读一下是不是 PENDING 再写"）。这里靠 rowcount 判定胜负，不靠先读。
INSERT_CHILD_RUN = """
INSERT INTO child_runs (
    child_run_id, kind, parent_run_id, parent_execution_id, parent_task_id,
    target, action, status, spawned_at, wait_until
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (parent_execution_id) DO NOTHING
"""

#: M30 / 空洞 209：结果落库。
#:
#: `WHERE completed_at IS NULL` 是**幂等判据本身**，不是"顺手加个条件"：
#: at-least-once 投递下同一条 `child_run.completed` 会来第二次，
#: 那时唯一正确的行为是"什么都不做并返回第一次的结果"。
#: 用 rowcount 判胜负（与 `INSERT_CHILD_RUN` 同一形状），不靠先读。
UPDATE_CHILD_RUN_RESULT = """
UPDATE child_runs
   SET status = %s, result = %s, completed_at = %s
 WHERE child_run_id = %s
   AND completed_at IS NULL
"""

#: 交付登记。`completed_at IS NOT NULL` 这一半与 `delivered_at IS NULL` 一样
#: 都是判据，不是冗余：`010` 的 CHECK 是**兜底**（PR-26），
#: 判胜负靠 rowcount，于是"没结果却来登记交付"能被分辨出来而不是静默无效。
UPDATE_CHILD_RUN_DELIVERED = """
UPDATE child_runs
   SET delivered_at = %s
 WHERE child_run_id = %s
   AND completed_at IS NOT NULL
   AND delivered_at IS NULL
"""

#: 空洞 224 / D-14：登记**取消请求**。刻意不动 `status` / `completed_at`。
#:
#: 两个 `WHERE` 条件各自都是判据，不是冗余：
#:   `completed_at IS NULL`       → D-15：终态改不动。这是赛跑里"完成赢"
#:                                  那一半的物理判据，用 rowcount 判胜负。
#:   `cancel_requested_at IS NULL` → 幂等，且不覆盖**第一次**的原因
#:                                  （第二次叫停通常来自另一个人，A-8）。
UPDATE_CHILD_RUN_CANCEL_REQUEST = """
UPDATE child_runs
   SET cancel_requested_at = %s, cancel_reason = %s, cancel_requested_by = %s
 WHERE child_run_id = %s
   AND completed_at IS NULL
   AND cancel_requested_at IS NULL
"""

#: D-20 / 空洞 229：登记"这次等待已经处置过"，让它退出 `overdue()` 的队首。
#:
#: `completed_at IS NULL` 这一半是**判据**不是冗余：结果一旦产生，
#: 这条派生就不再属于"等不到"，它该走唤醒路径（D-7）。
#: 让它在这里被结掉，等于把"有结果"和"没结果"用同一个动作处置 ——
#: 那正是 PR-19 不许的那件事。
#:
#: `wait_expired_at IS NULL` 这一半是幂等 + **让出队首**（R-13 同款）：
#: 行留在队列里，它的 `wait_until` 永远最小，于是它会永久占着队首。
UPDATE_CHILD_RUN_WAIT_EXPIRED = """
UPDATE child_runs
   SET wait_expired_at = %s
 WHERE child_run_id = %s
   AND completed_at IS NULL
   AND wait_expired_at IS NULL
"""


def _row_to_child_run(row: Mapping[str, Any]) -> "ChildRunHandle":
    return ChildRunHandle(
        child_run_id=row["child_run_id"],
        kind=ChildRunKind(row["kind"]),
        parent_run_id=row["parent_run_id"],
        parent_execution_id=row["parent_execution_id"],
        target=row["target"],
        action=action_from_dict(row["action"], run_id=row["parent_run_id"]),
        parent_task_id=row["parent_task_id"],
        status=row["status"] or "created",
        spawned_at=parse_dt(row["spawned_at"]) or datetime.now(),
        result=dict(row.get("result") or {}),
        completed_at=parse_dt(row.get("completed_at")),
        delivered_at=parse_dt(row.get("delivered_at")),
        cancel_requested_at=parse_dt(row.get("cancel_requested_at")),
        cancel_reason=row.get("cancel_reason") or "",
        cancel_requested_by=row.get("cancel_requested_by") or "",
        wait_until=parse_dt(row.get("wait_until")),
        wait_expired_at=parse_dt(row.get("wait_expired_at")),
    )


class PostgresChildRunRegistry:
    """`ChildRunRegistryPort` 的 PostgreSQL 实现（表见 `008_child_runs.sql`）。

    **为什么这一块必须持久（A-12 的判据）：**
    登记丢了 → 重启后父 Run 会派生出第二条子 Run →
    同一件事做两遍、花两份钱、产生两份外部副作用，
    而**没有任何界面会显示它**（父 Run 看起来完全正常）。这是**变错**。

    **D-1 的物理保证**是 `UNIQUE(parent_execution_id)`，不是"先查一下"。
    `bind()` 用 `ON CONFLICT DO NOTHING` + rowcount 判定：
    rowcount = 0 表示"我没赢"，于是回读赢家那一条并原样返回。

    **S-1 的落点**是 `action` 列：整条 Action（含 `compensation`）存下来，
    于是父 Run **恢复之后**等到子 Run 结果时，仍然拿得到逆操作声明。
    """

    def __init__(
        self,
        conn: Any,
        wait_timeout: "timedelta | None" = None,
        max_wait_timeout: "timedelta | None" = None,
    ) -> None:
        self.conn = conn
        #: D-18：等待上限。缺省用 `DEFAULT_CHILD_WAIT_TIMEOUT`（30 分钟），
        #: 与 `015_child_wait_deadline.sql` 的回填必须一致。
        #: `is not None` 而不是 `or`：`timedelta(0)` 是**假值**，
        #: `or` 会把它悄悄换成默认的 30 分钟 —— 于是"我等不了那么久"
        #: 这个合法的诉求被静默升级成"再等半小时"，而且没有任何报错。
        self.wait_timeout = (
            wait_timeout if wait_timeout is not None else DEFAULT_CHILD_WAIT_TIMEOUT
        )
        #: D-33：裁决上限。与内存登记处同一个 `_resolve_ceiling`，
        #: 于是"部署能不能比平台更宽松"只有一个答案。
        self.max_wait_timeout = _resolve_ceiling(max_wait_timeout)

    def bind(self, handle: "ChildRunHandle") -> "ChildRunHandle":
        if handle.action is None:
            raise InvariantViolation(
                f"S-1: child run {handle.child_run_id!r} was spawned without an "
                f"action; the compensation declaration would be lost on restore"
            )
        #: D-18：上限在**写入**这一刻固化（R-11 同款）。
        #: 已经带着上限来的（从库里读回来再写回的行）不改写 ——
        #: 改写会把"当时约定等多久"换成一个新的数。
        #:
        #: D-31/D-32/D-34：裁决与清场共用 `freeze_wait_deadline`，
        #: 与内存登记处是同一份定义（替身与真库不许给出两个答案）。
        #:
        #: D-33：裁决发生在**写库之前** —— 越界时这里抛的是点名的
        #: `InvariantViolation`，而不是数据库那句不会说话的 `IntegrityError`。
        frozen = freeze_wait_deadline(
            handle, default=self.wait_timeout, maximum=self.max_wait_timeout
        )
        wait_until = frozen.wait_until
        cur = self.conn.cursor()
        cur.execute(
            INSERT_CHILD_RUN,
            (
                handle.child_run_id,
                handle.kind.value,
                handle.parent_run_id,
                handle.parent_execution_id,
                handle.parent_task_id,
                handle.target,
                _json(action_to_dict(handle.action)),
                handle.status,
                handle.spawned_at,
                wait_until,
            ),
        )
        if cur.rowcount == 0:
            # 已经有人派生过了 —— D-1：返回**第一条**，不覆盖。
            # 覆盖等于抹掉幂等本身：第二次派出的那条从此谁也查不到，却照样在跑。
            existing = self.for_execution(handle.parent_execution_id)
            if existing is None:  # pragma: no cover - 只可能是并发删除
                raise InvariantViolation(
                    f"D-1: child run for execution {handle.parent_execution_id!r} "
                    f"conflicted on insert but cannot be read back"
                )
            return existing
        # D-34：交回去的是**冻结后**的 handle —— `wait_until` 已定、
        # `wait_timeout` 已清。调用方拿到的 handle 与库里那一行一致，
        # 也与内存登记处交回来的一致（替身与真库同一个答案）。
        return frozen

    def for_execution(self, parent_execution_id: str) -> "ChildRunHandle | None":
        cur = self.conn.cursor()
        cur.execute(
            SELECT_CHILD_RUN + " WHERE parent_execution_id = %s",
            (parent_execution_id,),
        )
        row = cur.fetchone()
        return _row_to_child_run(row) if row is not None else None

    def for_child(self, child_run_id: str) -> "ChildRunHandle | None":
        """唤醒路径手上只有 `child_run_id`（R-6 / CHILD_RUN_FINISHED 事件）。"""
        cur = self.conn.cursor()
        cur.execute(
            SELECT_CHILD_RUN + " WHERE child_run_id = %s", (child_run_id,)
        )
        row = cur.fetchone()
        return _row_to_child_run(row) if row is not None else None

    def children_of(self, parent_run_id: str) -> tuple["ChildRunHandle", ...]:
        cur = self.conn.cursor()
        cur.execute(
            SELECT_CHILD_RUN + " WHERE parent_run_id = %s ORDER BY spawned_at",
            (parent_run_id,),
        )
        return tuple(_row_to_child_run(r) for r in cur.fetchall())

    # ---------------------------------------------------------- 结果 / 交付（M30）
    def mark_finished(
        self,
        child_run_id: str,
        status: str,
        result: Mapping[str, Any],
        *,
        completed_at: "datetime | None" = None,
    ) -> "ChildRunHandle":
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_CHILD_RUN_RESULT,
            (
                status,
                _json(dict(result)),
                completed_at or datetime.now(timezone.utc),
                child_run_id,
            ),
        )
        if cur.rowcount == 1:
            return self._reload(child_run_id)
        # rowcount = 0：要么没有这条子 Run，要么它已经有结果了。
        # 哪一种都必须**分辨**出来 —— 一律当成"已存在"会让"结果写给了一条
        # 根本没登记过的子 Run"变成一次静默成功。
        existing = self.for_child(child_run_id)
        if existing is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered; "
                f"a result can only be recorded for a child run that was bound"
            )
        if existing.status != status:
            raise InvariantViolation(
                f"B-3: child run {child_run_id!r} is already {existing.status!r}; "
                f"it cannot become {status!r}"
            )
        return existing

    def request_cancel(
        self,
        child_run_id: str,
        *,
        reason: str,
        by: str,
        requested_at: "datetime | None" = None,
    ) -> "ChildRunHandle":
        """D-14：登记取消请求，一行 UPDATE 判出三种结局。

        rowcount = 1 → 我写进去了。
        rowcount = 0 → 回读分辨：
            · 没这条子 Run           → D-2 抛（写给了一条没登记过的派生）
            · 已终态                 → D-15，原样返回（赛跑的"完成赢"）
            · 已请求过               → 幂等，返回第一次那条的痕迹
        """
        if not reason:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty reason; "
                "a cancellation nobody can explain is unauditable"
            )
        if not by:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty 'by'; "
                "an anonymous cancellation cannot be attributed (A-8)"
            )
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_CHILD_RUN_CANCEL_REQUEST,
            (
                requested_at or datetime.now(timezone.utc),
                reason,
                by,
                child_run_id,
            ),
        )
        if cur.rowcount == 1:
            return self._reload(child_run_id)
        existing = self.for_child(child_run_id)
        if existing is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered; "
                f"a cancellation can only be requested for a child run that "
                f"was bound"
            )
        return existing

    def mark_delivered(
        self, child_run_id: str, *, delivered_at: "datetime | None" = None
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_CHILD_RUN_DELIVERED,
            (delivered_at or datetime.now(timezone.utc), child_run_id),
        )
        if cur.rowcount == 1:
            return True
        existing = self.for_child(child_run_id)
        if existing is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered"
            )
        if not existing.is_finished:
            raise InvariantViolation(
                f"D-7: child run {child_run_id!r} has no result yet; "
                f"mark_finished() must run before mark_delivered()"
            )
        return False

    def undelivered(self, limit: int = 64) -> list["ChildRunHandle"]:
        """A-12 的兜底扫入口：已终态但结果还没交回父 Run 的那些。

        它存在的唯一理由，是让"Kafka 丢了"只等于**变慢**而不是**变错**：
        没有它，唤醒路径只有一条（事件），事件一丢父 Run 就永远挂起，
        而且界面上显示的是"在等子 Run"，看起来完全正常。
        """
        cur = self.conn.cursor()
        cur.execute(
            SELECT_CHILD_RUN
            + " WHERE completed_at IS NOT NULL AND delivered_at IS NULL"
            " ORDER BY completed_at LIMIT %s",
            (limit,),
        )
        return [_row_to_child_run(r) for r in cur.fetchall()]

    def overdue(self, now: "datetime", limit: int = 64) -> list["ChildRunHandle"]:
        """D-18 的兜底扫入口：等到上限还没有结果的那些派生。

        与 `undelivered()` 的关系见端口上的注释 —— 两条队列，两种处置。

        谓词与 `idx_child_runs_overdue` **逐字一致**，这不是抄两遍：
        索引是"谁能进队首"的物理判据，这条 SQL 是它的读法。
        两边一旦漂移，真库上就会出现"索引说它到期、SQL 说不到期"，
        而那正好表现为"永远扫不到"（又是那个沉默的形状）。
        """
        cur = self.conn.cursor()
        cur.execute(
            SELECT_CHILD_RUN
            + " WHERE delivered_at IS NULL AND completed_at IS NULL"
            "   AND wait_expired_at IS NULL AND wait_until < %s"
            " ORDER BY wait_until LIMIT %s",
            (now, limit),
        )
        return [_row_to_child_run(r) for r in cur.fetchall()]

    def mark_wait_expired(
        self, child_run_id: str, *, expired_at: "datetime | None" = None
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            UPDATE_CHILD_RUN_WAIT_EXPIRED,
            (expired_at or datetime.now(timezone.utc), child_run_id),
        )
        if cur.rowcount == 1:
            return True
        existing = self.for_child(child_run_id)
        if existing is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered"
            )
        if existing.is_finished:
            # D-7：结果已经产生了。这一支不是"等不到"，不该走到期路径。
            raise InvariantViolation(
                f"D-20: child run {child_run_id!r} already produced a result "
                f"({existing.status!r}); its wait cannot expire — deliver it "
                f"instead of declaring the wait over"
            )
        return False

    def _reload(self, child_run_id: str) -> "ChildRunHandle":
        handle = self.for_child(child_run_id)
        if handle is None:  # pragma: no cover - 刚写进去的行不可能读不回来
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} vanished after update"
            )
        return handle


# ================================================================ Run 级取消（M34）

SELECT_RUN_CANCELLATION = """
SELECT run_id, reason, requested_by, requested_at, settled_at,
       abandon_after, abandoned_at
  FROM run_cancellations
"""

#: R-11：写入那一刻就把等待上限**固化**进这一行。
#:
#: `ON CONFLICT` 的 `WHERE` 里多了一句 `abandoned_at IS NULL`：
#: 放弃过的意图**不复活**（与 `settled_at IS NULL` 同一条判据，理由见
#: `InMemoryRunCancellationStore.request`）。
#:
#: 顺便把 `abandoned_at` 也写进 SET 是不必要的 —— 走到这一支说明它本来就是 NULL。
INSERT_RUN_CANCELLATION = """
INSERT INTO run_cancellations
       (run_id, reason, requested_by, requested_at, abandon_after)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO UPDATE
   SET reason = EXCLUDED.reason,
       requested_by = EXCLUDED.requested_by,
       requested_at = EXCLUDED.requested_at,
       abandon_after = EXCLUDED.abandon_after,
       settled_at = NULL
 WHERE run_cancellations.settled_at IS NULL
   AND run_cancellations.abandoned_at IS NULL
"""

SETTLE_RUN_CANCELLATION = """
UPDATE run_cancellations SET settled_at = %s
 WHERE run_id = %s AND settled_at IS NULL
"""

#: R-11：记下"我们不再等了"。
#: 只写 `abandoned_at`，**不动** `settled_at` —— 见 R-14：
#: "它停了"与"我们不等了"是两件独立的事，可以同时成立。
ABANDON_RUN_CANCELLATION = """
UPDATE run_cancellations SET abandoned_at = %s
 WHERE run_id = %s AND settled_at IS NULL AND abandoned_at IS NULL
"""


def _row_to_run_cancellation(row: Mapping[str, Any]) -> "RunCancellation":
    return RunCancellation(
        run_id=row["run_id"],
        reason=row["reason"],
        by=row["requested_by"],
        requested_at=row["requested_at"],
        settled_at=row["settled_at"],
        abandon_after=row.get("abandon_after"),
        abandoned_at=row.get("abandoned_at"),
    )


class PostgresRunCancellationStore:
    """`RunCancellationStore` 的 PostgreSQL 实现。

    表见 `011_run_cancellations.sql`（意图本身）
    与 `014_cancellation_grace.sql`（等待上限 / 放弃痕迹）。

    ------------------------------------------------------------------
    **这张表是跨进程取消的全部意义所在**

    取消意图只有落在**另一个进程看得到**的地方才有意义。
    放内存里，父进程写得再规范，子进程也一无所知 ——
    而那正是 M33 只走通一半的原因。

    ------------------------------------------------------------------
    为什么 `request()` 是 upsert 而不是 insert

    一条 Run 最多一个取消请求（`run_id` 是主键）。
    重复请求（父 Run 取消了两次、两个副本同时处理）必须收敛成一条，
    而不是变成"两条意图、两次记账"。

    `WHERE run_cancellations.settled_at IS NULL` 这一句是**不复活**（R-8）：
    已经结掉的意图不该被第二次请求重新点亮 ——
    那条 Run 早就终态了，重新点亮只会让 Sweeper 每轮撞一次 R-3。
    014 之后这里多了 `AND abandoned_at IS NULL`（R-11），同一条判据。

    判胜负靠 rowcount，不靠先读（与 D-1 的 `ON CONFLICT DO NOTHING` 同款）：
    at-least-once 语义下"读出来判一下再写"永远慢一拍。

    ------------------------------------------------------------------
    `grace` 为什么挂在 store 上

    等待上限必须在**写入那一刻**固化进那一行（`abandon_after`），
    因为"这条请求当初承诺过多久"是审计的一部分 ——
    事后调策略不该追溯地改写老意图。而只有写它的这个人知道它。
    """

    def __init__(self, conn: Any, grace: timedelta | None = None, *, events: Any = None) -> None:
        """`events` 是事件落点（X-3：必须与状态写入**同一事务**）。

        组合根必须传 `PostgresOutboxStore(conn)` —— **同一个 conn**。
        传另一个连接上的 outbox，就等于把"这一行改了"和"通知下游"
        放进两个事务：第一个提交、第二个回滚，意图就又变回"改了没人知道"。
        """
        self.conn = conn
        self.grace = grace if grace is not None else DEFAULT_CANCELLATION_GRACE
        self.events = events

    def request(self, run_id: str, *, reason: str, by: str) -> "RunCancellation":
        now = datetime.now(timezone.utc)
        cur = self.conn.cursor()
        cur.execute(
            INSERT_RUN_CANCELLATION,
            (run_id, reason, by, now, now + self.grace),
        )
        stored = self.for_run(run_id)
        if stored is None:  # pragma: no cover - 刚写进去的行不可能读不回来
            raise InvariantViolation(
                f"R-7: cancellation request for run {run_id!r} vanished after write"
            )

        # M66：rowcount = 0 ⇒ 这一行**没被改动** —— 它已经 settled 或 abandoned，
        # 按 R-8 / R-11 不复活（INSERT 的 WHERE 把它挡住了）。
        #
        # 此时**不发事件**：意图没有变化，却在审计流里多出一条
        # "有人又要求取消了一次"，是假的。内存版（`InMemoryRunCancellationStore`）
        # 一直是不发的 —— 两种实现必须同源（B-7），否则"只有一种实现里成立"的保证
        # 等于没有。这个不一致就是真库集成测试补上来之后才暴露的。
        if cur.rowcount == 1:
            _emit_cancellation_event(
                self.events, stored, event_type="cancellation.requested"
            )
        return stored

    def for_run(self, run_id: str) -> "RunCancellation | None":
        cur = self.conn.cursor()
        cur.execute(SELECT_RUN_CANCELLATION + " WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        return _row_to_run_cancellation(row) if row is not None else None

    def pending(self, limit: int = 64) -> list["RunCancellation"]:
        # R-13：谓词必须带上 `abandoned_at IS NULL`。
        # 少了这一句，"让路"根本没发生 —— 放弃过的意图照样排队首、照样占槽位，
        # 而 `014` 加的那两列就只是一份没人读的审计记录。
        cur = self.conn.cursor()
        cur.execute(
            SELECT_RUN_CANCELLATION
            + " WHERE settled_at IS NULL AND abandoned_at IS NULL "
            "ORDER BY requested_at LIMIT %s",
            (limit,),
        )
        return [_row_to_run_cancellation(r) for r in cur.fetchall()]

    def expiring(self, now: datetime, limit: int = 64) -> list["RunCancellation"]:
        """R-11：到点了还没有任何结局的那些。按"最早到点"排（不是最早请求）。"""
        cur = self.conn.cursor()
        cur.execute(
            SELECT_RUN_CANCELLATION
            + " WHERE settled_at IS NULL AND abandoned_at IS NULL "
            "AND abandon_after IS NOT NULL AND abandon_after <= %s "
            "ORDER BY abandon_after LIMIT %s",
            (now, limit),
        )
        return [_row_to_run_cancellation(r) for r in cur.fetchall()]

    def settle(self, run_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            SETTLE_RUN_CANCELLATION, (datetime.now(timezone.utc), run_id)
        )
        ok = bool(cur.rowcount)
        if ok:
            stored = self.for_run(run_id)
            if stored is not None:
                _emit_cancellation_event(self.events, stored, event_type="cancellation.settled")
        return ok

    def abandon(self, run_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            ABANDON_RUN_CANCELLATION, (datetime.now(timezone.utc), run_id)
        )
        ok = bool(cur.rowcount)
        if ok:
            stored = self.for_run(run_id)
            if stored is not None:
                _emit_cancellation_event(self.events, stored, event_type="cancellation.abandoned")
        return ok
