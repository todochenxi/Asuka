"""审批的 PostgreSQL 实现（M19）。

**A-10：审批落 PG，不落 Redis。**

审批不是一个"待办事项"，它是 Kernel 里那条 `SUSPENDED(HUMAN_APPROVAL)`
Execution 的**唤醒条件**。所以它必须活过进程重启 —— 人第二天来上班，
Run 还在等他，审批记录也还得在。

Redis 丢了会怎样：界面上什么都没有，而系统里全在等。
这是最难排查的一类故障，因为它**不报错**。

对比一下就很清楚：

| 数据 | 丢了会怎样 | 所以放哪 |
|---|---|---|
| Lease 索引 | 变慢（可从 PG 重建） | Redis（快路径） |
| 审批记录 | **变错**（Run 永远挂着，界面上看不见） | PG（事实源） |

> 判断标准不是"重不重要"，而是**"丢了之后是变慢还是变错"**。

**表结构见 `infrastructure/postgres/003_approvals.sql`。**
H-1 / H-5 / A-8 三条约束在 DB 层兜底 —— 领域里的检查是"善意"，DB 约束才是"兜底"。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from packages.agent_domain.business.snapshot import action_from_dict, action_to_dict
from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import Action

from ..approval import ApprovalRequest, ApprovalStatus

INSERT_APPROVAL = """
INSERT INTO approvals (
    approval_id, run_id, execution_id, status, reason, action,
    requested_at, expires_at, decided_by, decided_at, comment
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (approval_id) DO UPDATE SET
    run_id       = EXCLUDED.run_id,
    execution_id = EXCLUDED.execution_id,
    status       = EXCLUDED.status,
    reason       = EXCLUDED.reason,
    action       = EXCLUDED.action,
    requested_at = EXCLUDED.requested_at,
    expires_at   = EXCLUDED.expires_at,
    decided_by   = EXCLUDED.decided_by,
    decided_at   = EXCLUDED.decided_at,
    comment      = EXCLUDED.comment,
    version      = approvals.version + 1
"""

SELECT_APPROVAL = """
SELECT approval_id, run_id, execution_id, status, reason, action,
       requested_at, expires_at, decided_by, decided_at, comment
  FROM approvals
"""

#: A-11：判定与写入是一个语句。`AND status = 'pending'` 就是"我没输这场竞争"。
TRANSITION_APPROVAL = """
UPDATE approvals
   SET status = %s,
       decided_by = %s,
       decided_at = %s,
       comment = %s,
       version = version + 1
 WHERE approval_id = %s
   AND status = 'pending'
"""


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _dump_action(action: Action | None) -> str:
    """M26：改用**共享**的 `action_to_dict`。

    以前这里手写了一份，漏掉 `action.compensation`（S-8 的撤销声明）。
    不是写漏了 —— 是当时 `CompensationSpec.to_dict` 因为缩进错误根本不存在，
    压根没有通道。后果是：审批恢复出来的 Action 没有逆操作声明，
    `_record_compensation` 静默跳过，补偿账本缺一条。

    "Action 怎么落库"只允许有一处定义，否则两套答案必然漂移。
    """
    if action is None:
        return "{}"
    return _json(action_to_dict(action))


def _load_action(raw: Any, run_id: str) -> Action:
    """反序列化审批对象。

    空值不是"可以不追究"的缺省，而是**数据损坏** ——
    一条审批如果读不出"我要放行的是什么"，那它就不该被放行。
    这里绝不能默默造一个占位 Action 顶上：那等于让人在不知道批的是什么的情况下签字。
    """
    try:
        return action_from_dict(raw, run_id=run_id)
    except InvariantViolation as exc:
        raise InvariantViolation(
            f"approval for run {run_id!r} has no action stored; refusing to load "
            f"({exc})"
        ) from exc


def _row_to_approval(row: Mapping[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        action=_load_action(row["action"], row["run_id"]),
        reason=row["reason"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        execution_id=row["execution_id"],
        status=ApprovalStatus(row["status"]),
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        comment=row["comment"] or "",
    )


class PostgresApprovalStore:
    """`ApprovalStore` 的 PostgreSQL 实现。

    与 `InMemoryApprovalStore` 共享同一套语义（尤其是 `transition` 的
    "只认 PENDING"），否则"并发下只有一个赢"这条保证只在一种实现里成立。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, request: ApprovalRequest) -> None:
        cur = self.conn.cursor()
        cur.execute(
            INSERT_APPROVAL,
            (
                request.approval_id,
                request.run_id,
                request.execution_id,
                request.status.value,
                request.reason,
                _dump_action(request.action),
                request.requested_at,
                request.expires_at,
                request.decided_by,
                request.decided_at,
                request.comment,
            ),
        )

    def get(self, approval_id: str) -> ApprovalRequest | None:
        cur = self.conn.cursor()
        cur.execute(SELECT_APPROVAL + " WHERE approval_id = %s", (approval_id,))
        row = cur.fetchone()
        return _row_to_approval(row) if row is not None else None

    def pending(self, run_id: str | None = None) -> Sequence[ApprovalRequest]:
        cur = self.conn.cursor()
        if run_id is None:
            cur.execute(SELECT_APPROVAL + " WHERE status = 'pending' ORDER BY requested_at")
        else:
            cur.execute(
                SELECT_APPROVAL
                + " WHERE status = 'pending' AND run_id = %s ORDER BY requested_at",
                (run_id,),
            )
        return [_row_to_approval(r) for r in cur.fetchall()]

    def transition(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            TRANSITION_APPROVAL,
            (status.value, by, decided_at, comment, approval_id),
        )
        # rowcount = 0：它已经不是 PENDING 了 —— 有人抢先决定了（A-11）
        return cur.rowcount == 1
