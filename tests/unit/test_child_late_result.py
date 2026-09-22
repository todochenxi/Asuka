"""空洞 231：迟到的结果 —— 等它的人早就不等了（M39）。

--------------------------------------------------------------------------
起因：M38 冻结时 D-20 只说了一半

D-20 说"到期不是终态 —— 它路回来了照样认"。这句话的前半段有实现
（`undelivered()` 的谓词里没有 `wait_expired_at`），后半段**没有**：

    "照样认"认完之后要干什么，没有人回答过。

探针（`probe39.py`，已删）跑出来的现状是两件事同时发生：

    · `wake()` 返回 `ALREADY_DELIVERED`
      —— 这名字说的是"已经交过了"，而事实上**从来没有人接过它**

    · 账本上那行仍然写着 "WE DO NOT KNOW whether it is still running"
      —— 真相到了，账本没动。它从"缺一格"变成"**错一格**"（PR-19）

错一格比缺一格坏：缺一格会让运维去看一眼，错一格会让运维**不去看**。

--------------------------------------------------------------------------
这一轮买的两件事

    D-22  迟到不交付（等它的人已经不等了），但必须记账 + 让出队列
    D-23  "不知道"必须被**收回** —— 只补还开着的账，已处置的是历史
    D-24  迟到的结果必须看得见 —— 不能并进"已交付"那个数里

--------------------------------------------------------------------------
这里的世界是**真跑起来的**（PR-28）

`WaitWorld`（`test_child_wait_deadline.py`）会真派生、真挂起、真到期、
真落快照。手搓 handle + 快照的测试会让"迟到"退化成几个对象互相赋值。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.compensation import CompensationStatus
from packages.agent_domain.business.snapshot import state_from_dict
from packages.agent_runtime.child_wake import ChildWakeOutcome
from packages.agent_runtime.reducer import CHILD_RUN_FINISHED
from packages.agent_runtime.saga import SagaCoordinator

from .test_child_wait_deadline import WaitWorld, _compensable

UNKNOWN_MARKER = "WE DO NOT KNOW"


class _LateWorld(WaitWorld):
    """`WaitWorld` + 一条**已经到期、然后又回来了**的派生。"""

    def _late(
        self,
        status: str = "completed",
        result: dict | None = None,
    ) -> tuple[Any, str]:
        """派生 → 子 Run 的进程没了 → 到期 → 它**后来**跑完了。

        `result` 刻意带内容：它是"这条子 Run 到底干了什么"的答案，
        而这一整轮要验的正是"这个答案有没有人接"。
        """
        loop, child_id = self._gone()
        self.expirer.expire(child_id, now=self._now())
        self.registry.mark_finished(
            child_id, status, result or {"summary": "报告写完了"}
        )
        return loop, child_id

    def _rows(self) -> list[Any]:
        return list(self.compensations._by_id.values())  # type: ignore[attr-access]

    def _observations(self, run_id: str = "run_parent") -> list[Any]:
        """从**快照**读，不走 `rebuild()`。

        到期那一支会把父 Run 推到终态（D-27），R-3 之后就恢复不出来了。
        但 Observation 已经写进快照（R-4：账本必须延续）——
        要问的是"它相信过什么"，不是"能不能再来一次"。
        """
        snapshot = self.snapshots.latest(run_id)
        assert snapshot is not None, f"{run_id} 一份快照都没落"
        return list(state_from_dict(snapshot.state).observations)


class D22TheLateResultIsNotDeliveredTest(_LateWorld):
    """D-22：不交付，但要记账、要让出队列。"""

    def test_the_outcome_is_late_not_already_delivered(self) -> None:
        """`ALREADY_DELIVERED` 是一句谎话：从来没有人接过它（PR-19）。"""
        _loop, child_id = self._late()
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.LATE)

    def test_the_parent_never_receives_it(self) -> None:
        """父 Run 一条新的 Observation 都不该多出来。

        它已经不在等这条子 Run 了；把旧结果插回去等于让一个已经往前走过
        的人收到一条他不再期待的消息。
        """
        _loop, child_id = self._late()
        before = self._observations()

        self.waker.wake(child_id)

        after = self._observations()
        self.assertEqual(len(after), len(before), "父 Run 不该被迟到结果动过")
        finished = [
            o
            for o in after
            if o.kind is CHILD_RUN_FINISHED
            and o.content.get("child_run_id") == child_id
        ]
        self.assertEqual(finished, [], "迟到的结果不许伪装成'子 Run 已完成'")

    def test_it_leaves_the_undelivered_queue(self) -> None:
        """R-13 同款：`undelivered()` 也是 `ORDER BY ... LIMIT`。

        不登记交付，这一行就永久占着队首，后面所有迟到的结果全进不来。
        """
        _loop, child_id = self._late()
        self.assertEqual(
            [h.child_run_id for h in self.registry.undelivered()], [child_id]
        )
        self.waker.wake(child_id)
        self.assertEqual([h.child_run_id for h in self.registry.undelivered()], [])
        self.assertEqual(self.waker.sweep().total, 0, "扫第二次不该再有活")

    def test_the_control_group_an_in_time_result_is_still_delivered(self) -> None:
        """控制组：**没有**到期的结果必须照原样交付。

        没有这一条，"不交付"就可能被实现成"一律不交付" ——
        那是一个每样都绿的假实现。
        """
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {"n": 1})
        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.DELIVERED)


class D23TheLedgerStopsSayingWeDoNotKnowTest(_LateWorld):
    """D-23：真相到达时，账本上那句"不知道"必须被收回。"""

    def test_the_unknown_is_withdrawn(self) -> None:
        """⚠️ 用 `failed` 而不是 `completed`：

        `completed` 那一支在 M41 之后归 **D-25** —— 真相证明副作用确实发生了，
        于是那笔账从"撤销不了"升级成"待撤销"，行上的 `reason` 按约定清空，
        来龙去脉改走事件（见 `test_child_late_upgrade.py`）。

        D-23 自己管的现在是"**仍然撤销不了**"这几支：`failed` / `cancelled` /
        撤销参数取不到 —— 它们的理由仍在行上，且必须带着 WITHDRAWN。
        """
        _loop, child_id = self._late(status="failed", result={"summary": "炸了"})
        self.assertIn(UNKNOWN_MARKER, self._rows()[0].reason)  # 到期那一刻：实话

        self.waker.wake(child_id)

        rows = self._rows()
        self.assertEqual(len(rows), 1, "补真相不得再造一条（S-2）")
        self.assertNotIn(UNKNOWN_MARKER, rows[0].reason)
        self.assertIn("WITHDRAWN", rows[0].reason)
        self.assertIn("failed", rows[0].reason, "得说清它最后是**怎么结束**的")

    def test_the_side_effect_is_still_unresolved(self) -> None:
        """收回的是"不知道"这三个字，**不是**那笔副作用（S-15）。

        把它一并结掉，就是把"我们终于知道它干了什么"
        说成"它干的事已经处理好了"。

        ⚠️ 同样用 `failed`：`completed` 那一支现在会升成 PENDING（D-25），
        因为那不是"结掉"，是"**它现在撤销得掉了**"。
        """
        _loop, child_id = self._late(status="failed", result={"summary": "炸了"})
        self.waker.wake(child_id)
        rows = self._rows()
        self.assertIs(rows[0].status, CompensationStatus.UNRESOLVED)

    def test_a_failed_child_also_withdraws_the_unknown(self) -> None:
        """真相不止一种：`failed` 也是真相，也得写进去。

        只处理 `completed` 的实现会把"它失败了"继续留在"不知道"里 ——
        于是账本上那行仍然是一句错话。
        """
        _loop, child_id = self._late(status="failed", result={"summary": "炸了"})
        self.waker.wake(child_id)
        rows = self._rows()
        self.assertNotIn(UNKNOWN_MARKER, rows[0].reason)
        self.assertIn("failed", rows[0].reason)

    def test_a_record_that_has_already_been_disposed_of_is_left_alone(self) -> None:
        """已经处置过的是**历史**，不是草稿。

        人工把它从 UNRESOLVED 拉回 PENDING（S-14 允许的唯一例外）之后，
        "补一句真相"改的就是昨天的账。
        """
        saga = SagaCoordinator(store=self.compensations)
        record = saga.record_unresolved(
            run_id="run_handled",
            step_id="step_1",
            task_id="task_1",
            execution_id="exec_handled",
            action=_compensable("run_handled"),
            reason="we do not know yet",
        )
        assert record is not None
        record.transition(CompensationStatus.PENDING)
        self.compensations.save(record)

        self.assertIsNone(
            saga.amend_unresolved(
                execution_id="exec_handled", reason="now we know: it completed"
            )
        )
        after = self.compensations.get_by_execution("exec_handled")
        assert after is not None
        self.assertIs(after.status, CompensationStatus.PENDING)
        self.assertNotIn("now we know", after.reason)

    def test_amending_a_row_that_does_not_exist_creates_nothing(self) -> None:
        """没有这一行就返回 `None` —— "补一句"绝不能变成"多一笔账"（S-2）。"""
        saga = SagaCoordinator(store=self.compensations)
        self.assertIsNone(
            saga.amend_unresolved(execution_id="exec_never", reason="truth")
        )
        self.assertEqual(list(self.compensations._by_id.values()), [])  # type: ignore[attr-access]


class D24TheLateResultIsVisibleTest(_LateWorld):
    """D-24 / 空洞 227：它必须**看得见**，而且不能并进"已交付"那个数。"""

    def test_late_and_delivered_are_counted_separately(self) -> None:
        _loop, child_id = self._late()
        result = self.waker.sweep()
        self.assertEqual(result.late, (child_id,))
        self.assertEqual(result.delivered, (), "迟到不是交付")
        self.assertEqual(result.total, 1)

    def test_the_control_group_a_normal_delivery_is_counted_as_delivered(self) -> None:
        _loop, child_id = self._spawned()
        self.registry.mark_finished(child_id, "completed", {"n": 1})
        result = self.waker.sweep()
        self.assertEqual(result.delivered, (child_id,))
        self.assertEqual(result.late, ())


class TheParentIsAlreadyTerminalTest(_LateWorld):
    """父 Run 在结果回来**之后**才终态 —— 两张脸叠在一起。"""

    def test_the_late_result_still_withdraws_the_unknown(self) -> None:
        loop, child_id = self._gone()
        self.expirer.expire(child_id, now=self._now())
        loop.cancel(reason="not needed", by="alice")

        self.registry.mark_finished(child_id, "completed", {"n": 1})
        before = len(self._rows())

        self.assertIs(self.waker.wake(child_id), ChildWakeOutcome.LATE)

        self.assertEqual(len(self._rows()), before, "迟到的结果不得再造一条（S-2）")
        reasons = [r.reason for r in self._rows()]
        self.assertTrue(
            any(UNKNOWN_MARKER not in r for r in reasons),
            f"没有一行收回了那句'不知道'：{reasons}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
