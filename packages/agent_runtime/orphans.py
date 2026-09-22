"""D-13 孤儿副作用的**唯一**落点（M37 / 空洞 226）。

--------------------------------------------------------------------------
为什么这个文件要单独存在

"孤儿怎么记"此前只有一个调用方：`ChildRunWaker._record_orphan`
（父 Run 已终态、子 Run 结果无处可交的那一支）。

空洞 226 带来第二个调用方：

    一条被叫停的子 Run 再也没有回音
      → 系统放弃等待（R-11）
      → 它的副作用**还没有人记过**，而且从此再也不会有人记

两处都要往同一张账本写同一种记录。抄成两份之后，
"`step_id` 填什么、`args` 为什么恒空、S-2 的判重做没做、理由里要不要点名赛跑"
就会有**两个答案** —— 而它们必须只有一个（B-7）。

所以这里放唯一一份定义，两边都来调。

--------------------------------------------------------------------------
为什么不记"我们猜它是怎么结束的"

走到这里的两种情形有一个共同点：**没有可信的结局**。

    唤醒路径  父已终态，结果交不出去 —— 但至少知道结局是什么
    放弃路径  **连结局都不知道**（进程没了、也没留下终态）

放弃路径能写的只有一句真话：

    "我们叫过它停，等到点也没有回音，所以**不知道**它最后是停了还是跑完了。"

把它写成 `cancelled` 就是在为一个我们看不见的对象作证（PR-19）：
那条 Run 可能已经把邮件发出去了，而账本上写着"已取消，无副作用"。
一张说谎的账本，比一张写着"不知道"的账本坏得多 ——
后者至少会让运维去看一眼。

--------------------------------------------------------------------------
`step_id` 为什么是空的，而且这是**如实**而非敷衍

父 Run 已终态 ⟹ `rebuild()` 抛 `IllegalTransition`（R-3）⟹
拿不到它的 Step 对象。所以这里**说不出**是哪一步。

刻意不填一个假的：账本缺一格是可查的缺失，
而一个填错的 step_id 会把排查引到错误的那一步去 ——
那正是 PR-19 那一类错（报错说的和真实发生的不是同一件事）。
`task_id` 与 `execution_id` 是**精确**的，S-1 要求的
"哪条 Task 产生的副作用"说得清；理由里也写明了 step 为什么是空的。
"""
from __future__ import annotations

from typing import Any

from packages.agent_domain.errors import InvariantViolation

#: 见文件头"step_id 为什么是空的"。用一个具名常量而不是就地写字面量，
#: 是为了让"这里是刻意的"这件事在调用方也看得见。
ORPHAN_STEP_ID = ""


def orphan_reason(handle: Any, *, headline: str) -> str:
    """拼出一条孤儿记录的理由。

    `headline` 是**调用方**说的那一句（两种情形说的不一样，
    排障要找的人也不一样）：

        唤醒路径  "父 Run 已终态，结果无处可交"
        放弃路径  "叫停之后等不到回音，结局未知"

    尾部那两段是**公共**的，而且不能由调用方决定：
    step_id 为什么空（否则调用方会顺手填一个），
    以及这条孤儿是不是赛跑的产物（D-16）。
    """
    tail = (
        f" step_id is empty because a terminal parent cannot be rebuilt (R-3) to "
        f"name the step; task_id and execution_id are exact"
    )
    return f"{headline}{tail}{_race_note(handle)}"


def _race_note(handle: Any) -> str:
    """D-16：这条孤儿是不是**取消与完成赛跑**的结果？

    为什么要单独说一句（而不是让上面那段通用理由盖住它）：
    赛跑留下来的孤儿，责任方是**取消的人**；
    普通孤儿的责任方是**崩掉的那条 Run**。
    排障要找的人不一样，而账本理由里不写，
    运维只能靠猜（PR-19：说的和发生的必须是同一件事）。
    """
    if not handle.finished_after_cancel_request:
        return ""
    return (
        f" — D-16: this is a cancellation/outcome race; the parent asked it to "
        f"stop (reason {handle.cancel_reason!r}, by "
        f"{handle.cancel_requested_by!r}) before it finished, and it finished "
        f"{handle.status!r} anyway, so the result was produced but never "
        f"consumed by anyone"
    )


def _late_result_head(handle: Any) -> str:
    """迟到的结果到达时，那句"不知道"被收回 —— 这一段两种处置共用。

    ⚠️ 这里**不许**把当初那句 "WE DO NOT KNOW" 原样引用进来。

    运维找"还没查清楚的那些"最自然的动作是

        WHERE reason LIKE '%WE DO NOT KNOW%'

    而一条引用了旧措辞的记录会被它命中 —— 于是"已经查清楚的那条"
    继续出现在"还不知道的那些"里。收回了，却仍然搜得到，
    等于没收回（PR-19：说的和能被查到的必须是同一件事）。

    所以这里改用小写、且不完整的措辞回顾它（"the outcome was UNKNOWN"），
    审计线索由 `version` / `updated_at` 承担。
    """
    return (
        f"D-23: the earlier note that this child run's outcome was UNKNOWN is now "
        f"WITHDRAWN: child run {handle.child_run_id} ended {handle.status!r} after "
        f"its wait had been declared over at {handle.wait_until}; the parent run "
        f"{handle.parent_run_id!r} had already stopped waiting, so nobody consumed "
        f"this result (D-22)"
    )


def late_result_reason(handle: Any, *, tail: str = "") -> str:
    """D-23：迟到的结果到达时，账本上那句话该**换成**什么（**仍然撤销不了**那一支）。

    与 `orphan_reason` 的分工：那条是**开**一笔账（副作用从此没人负责），
    这条是**换**一句话（账已经开着，当初写的"不知道"现在被真相顶掉）。

    换完之后那一行**仍是 UNRESOLVED** —— 副作用还在外部世界没人撤销。
    变的只有一件事：**我们不再是不知道**（S-15）。

    `tail` 是 D-26 那一半：仍然撤销不了的时候，必须说清**是哪一样**挡着，
    不能停在"当初不知道"那句话上 —— 那句话已经被上面这段收回了。
    """
    return (
        f"{_late_result_head(handle)} — the side effect is still out there and "
        f"still UNRESOLVED, only the 'outcome unknown' part has been corrected"
        f"{tail}{_race_note(handle)}"
    )


def upgraded_result_reason(handle: Any) -> str:
    """D-25：迟到的结果**证明副作用确实发生了** —— 账本改说的那句话。

    与 `late_result_reason` 的区别不是措辞：那一条说"仍然撤销不了"，
    这一条说"**变得撤销得掉了，已经排上队**"。

    两者混成一句的话，运维看板上"撤销不了的那几笔"里会混进
    "其实撤销得掉、只是还没轮到"的那些 —— 而这两批要去做的事完全不同。
    """
    return (
        f"{_late_result_head(handle)} — the child run really did finish, so the "
        f"side effect is now KNOWN to have happened: this record is no longer "
        f"'impossible to undo' but 'awaiting undo' (D-25); nobody has run the "
        f"undo yet{_race_note(handle)}"
    )


def reconcile_late_result(saga: Any, handle: Any) -> Any:
    """D-25 / D-26：真相到了，账本该变成什么样 —— **唯一**定义。

    与 `record_child_orphan` 同住一个文件的同一个理由：
    "迟到的结果怎么落到账本上"只许有一个答案（B-7）。
    唤醒路径（`ChildRunWaker._reconcile_late`）是它唯一的调用方。

    三种结局，每一种都必须**如实**，不许停在"不知道"那句话上：

        completed   副作用**确实发生了** → 升级成"待撤销"（D-25）
                    撤销参数由 S-8 自己的 `materialize(result)` 补，
                    绝不手搓

        failed / cancelled
                    S-11 的前提**没变**：它没做成 / 被叫停了，
                    副作用**发没发生仍然不知道** → 保持 UNRESOLVED。
                    自动撤销一个可能并不存在的东西，
                    比留着它更糟（S-11 原文）

        completed 但撤销参数取不到
                    → 保持 UNRESOLVED，理由写明**缺哪个键**（D-26）
    """
    action = handle.action
    if action is None or action.compensation is None:
        return None
    if handle.status != "completed":
        # S-11：没做完 / 被叫停 ⟹ 副作用发没发生仍然不知道
        return saga.amend_unresolved(
            execution_id=handle.parent_execution_id,
            reason=late_result_reason(
                handle,
                tail=(
                    f" — D-26: it ended {handle.status!r}, so whether the side "
                    f"effect happened is still unknown (S-11); compensating now "
                    f"would risk undoing something that never existed"
                ),
            ),
        )
    try:
        args = action.compensation.materialize(handle.result)
    except InvariantViolation as exc:
        return saga.amend_unresolved(
            execution_id=handle.parent_execution_id,
            reason=late_result_reason(
                handle,
                tail=f" — D-26: the undo arguments still cannot be built: {exc}",
            ),
        )
    return saga.upgrade_to_compensable(
        execution_id=handle.parent_execution_id,
        args=args,
        reason=upgraded_result_reason(handle),
    )


def record_child_orphan(saga: Any, handle: Any, *, headline: str) -> Any:
    """把这条子 Run 的副作用记成"无人负责"。返回 `CompensationRecord | None`。

    ------------------------------------------------------------------
    不记会怎样

    父 Run 已终态 ⟹ 没有任何一条 `step()` 会再走 ⟹
    委派那一步永远不会有 `child_completed` / `_finish_child`，
    于是补偿账本**永远缺这一条**。

    而子 Run 是**已经跑过**的：它可能建了工单、发了邮件、派生了它自己的子 Run。
    那些东西留在外部世界，账本里一行都没有 ——
    运维看板上这次委派像从未发生过。

    ------------------------------------------------------------------
    返回 `None` 的两种情形（与 `SagaCoordinator.record_unresolved` 一致）

        · Action 没声明逆操作 —— 那就没有可记的账（S-1 的前提不成立）
        · 这条 Execution 已经有一条了（S-2 判重）

    第二种**正是放弃路径需要的**：
    先放弃等待（记一条"结局未知"），后来真相到来（唤醒路径再记一次）
    —— 第二次撞 S-2 返回 None，账本里不会多出一条自相矛盾的记录。
    代价是账本留着的是"未知"那条，而真相其实已经查得到了；
    这是**可接受**的：两种情形要运维做的事是同一件 —— 去看一眼那条子 Run。
    """
    action = handle.action
    if action is None or action.compensation is None:
        return None
    return saga.record_unresolved(
        run_id=handle.parent_run_id,
        step_id=ORPHAN_STEP_ID,
        task_id=handle.parent_task_id,
        execution_id=handle.parent_execution_id,
        action=action,
        reason=orphan_reason(handle, headline=headline),
    )


__all__ = [
    "ORPHAN_STEP_ID",
    "late_result_reason",
    "orphan_reason",
    "reconcile_late_result",
    "record_child_orphan",
    "upgraded_result_reason",
]
