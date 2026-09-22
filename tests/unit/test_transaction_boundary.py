"""事务边界（X-3）：一个请求 / 一个 tick = 一个事务（M29）。

--------------------------------------------------------------------------
为什么要有这个文件

`UnitOfWork` 从 M15 起就写在基线的"Kernel 持久化端口"清单里，
Worker 五件事的第 5 件也写着"outbox 同一事务内写事件"。
而代码里它**只有定义、零处使用** —— 整个仓库没有任何一处 `commit()`。

于是 X-3（状态写 + 事件写同一事务）**从未成立**：
`UPDATE executions` 与 `INSERT outbox_events` 之间进程一崩，
状态变了而事件没写出去，下游永远不知道这件事发生过，而系统**不报错**。
这正是 Outbox 模式存在的唯一理由被静默取消。

--------------------------------------------------------------------------
每条不变量配一个控制组。
"""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from apps._runtime import ProcessRuntime, _NullTransaction
from packages.execution_kernel.adapters.postgres import (
    InMemoryUnitOfWork,
    PostgresUnitOfWork,
)

ROOT = Path(__file__).resolve().parents[2]


class FakeUow:
    """会记账的 UnitOfWork。`atomic=True` 是**撒谎**，但这里只用它的记账。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self) -> "FakeUow":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class UnitOfWorkShapeTest(unittest.TestCase):
    """PR-28：替身**不许**声称自己支持事务。"""

    def test_the_pg_uow_is_atomic(self) -> None:
        self.assertTrue(PostgresUnitOfWork.atomic)

    def test_the_control_the_in_memory_uow_does_not_claim_to_be_atomic(self) -> None:
        """控制组：内存版明说自己**不**原子。

        少了这条，就可以靠把 `atomic` 全设成 True 让上一条通过 ——
        那等于在替身里把 X-3 判成通过，正是 PR-28 要防的。
        """
        self.assertFalse(InMemoryUnitOfWork.atomic)

    def test_committing_twice_is_counted(self) -> None:
        """提交次数是可断言的事实 —— "到底提交了没有"不该靠读代码确认。"""
        uow = InMemoryUnitOfWork()
        with uow:
            pass
        with uow:
            pass
        self.assertEqual((uow.commits, uow.rollbacks), (2, 0))


class ContextManagerTest(unittest.TestCase):
    def test_success_commits(self) -> None:
        uow = FakeUow()
        with uow:
            pass
        self.assertEqual((uow.commits, uow.rollbacks), (1, 0))

    def test_an_exception_rolls_back(self) -> None:
        uow = FakeUow()
        with self.assertRaises(RuntimeError):
            with uow:
                raise RuntimeError("boom")
        self.assertEqual((uow.commits, uow.rollbacks), (0, 1))

    def test_the_control_the_exception_still_propagates(self) -> None:
        """控制组：回滚**不是**吞异常。

        回滚之后如果异常被吃掉了，进程会以为这一轮干成了 ——
        tick 记成成功、退避归零，而那批写其实一起消失了。
        """
        uow = FakeUow()
        raised = False
        try:
            with uow:
                raise ValueError("x")
        except ValueError:
            raised = True
        self.assertTrue(raised)
        self.assertEqual(uow.rollbacks, 1)


class ProcessRuntimeTransactionTest(unittest.TestCase):
    """PR-30：一个 tick = 一个事务。

    放在共享骨架里，五个进程就都对了；写五遍，漏掉的那个不报错。
    """

    def _runtime(self, uow: FakeUow | None) -> ProcessRuntime:
        return ProcessRuntime(
            name="probe",
            idle_sleep=0.0,
            error_sleep=0.0,
            max_consecutive_failures=99,
            sleep=lambda _: None,
            uow=uow,
        )

    def test_one_commit_per_tick(self) -> None:
        uow = FakeUow()
        report = self._runtime(uow).run(lambda: 1, max_ticks=3)
        self.assertEqual(report.ticks, 3)
        self.assertEqual((uow.commits, uow.rollbacks), (3, 0))

    def test_a_failing_tick_rolls_back(self) -> None:
        uow = FakeUow()

        def boom() -> int:
            raise RuntimeError("infra down")

        report = self._runtime(uow).run(boom, max_ticks=2)
        self.assertEqual(report.errors, 2)
        self.assertEqual((uow.commits, uow.rollbacks), (0, 2))

    def test_the_control_success_and_failure_in_the_same_run(self) -> None:
        """控制组：成一次败一次，两边各记一次 —— 上一条不是因为"只回滚"。"""
        uow = FakeUow()
        state = {"n": 0}

        def flaky() -> int:
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("one bad tick")
            return 1

        report = self._runtime(uow).run(flaky, max_ticks=3)
        self.assertEqual(report.ticks, 3)
        self.assertEqual(uow.commits, 2)
        self.assertEqual(uow.rollbacks, 1)

    def test_without_a_uow_there_is_no_transaction_and_no_crash(self) -> None:
        """没有 uow 时照样能跑（纯内存测试要用它），但**没有**事务边界。

        刻意不抛错：逼测试造一个假事务只会得到"替身里通过"的证据（PR-28）。
        """
        runtime = self._runtime(None)
        self.assertIsInstance(runtime._transaction(), _NullTransaction)
        report = runtime.run(lambda: 1, max_ticks=2)
        self.assertEqual(report.ticks, 2)


class EveryProcessHasATransactionBoundaryTest(unittest.TestCase):
    """一个进程如果自己建连接却没接事务边界，它的写会**静默丢失**。

    `autocommit=False` 之后这件事变成了真的：忘了提交，写就没了，且不报错。
    所以"这个进程有没有事务边界"必须被静态断言，不能靠自觉。
    """

    #: 这些 `build_*` 都会自己 `pg_connection(config)`，必须同时接上 uow
    _BUILDERS = (
        "build_api",
        "build_worker_app",
        "build_outbox_publisher",
        "build_recovery_controller",
        "build_wakeup_controller",
        "build_cancellation_sweeper",
        "build_run_cancellation_sweeper",
        "build_child_run_consumer",
    )

    def test_every_process_builder_wires_a_unit_of_work(self) -> None:
        source = (ROOT / "apps" / "_bootstrap.py").read_text(encoding="utf-8")
        tree = inspect.getsource(__import__("apps._bootstrap", fromlist=["x"]))
        missing = []
        for name in self._BUILDERS:
            self.assertIn(f"def {name}(", source, f"{name} 不见了")
            # 取函数体，避免把别的函数的 pg_unit_of_work 算进来
            head = source.index(f"def {name}(")
            nxt = source.find("\ndef ", head + 1)
            body = source[head : nxt if nxt > 0 else len(source)]
            if "pg_unit_of_work(" not in body:
                missing.append(name)
        self.assertEqual(
            missing, [],
            f"这些进程自己建连接却没有事务边界，写会静默丢失：{missing}",
        )
        del tree

    def test_the_control_the_builder_list_is_not_stale(self) -> None:
        """控制组：`_BUILDERS` 里每个名字在组合根里都真的存在。

        少一个没人发现的话，上面那条会**因为列表变短而继续绿** ——
        那正是"检查项自己过期"这一类假证据。
        """
        source = (ROOT / "apps" / "_bootstrap.py").read_text(encoding="utf-8")
        for name in self._BUILDERS:
            self.assertIn(f"def {name}(", source)

    def test_the_control_every_builder_that_opens_a_connection_is_listed(self) -> None:
        """控制组的控制组：**新增**一个自己建连接的 builder 必须被发现。

        上一条只保证"列表里的都存在"，不保证"存在的都在列表里" ——
        M30 加 `build_child_run_consumer` 时列表里没有它，两条都照样绿。
        判据是 PR-32 那句：一条测试绿着，不代表它还在说真话。

        所以这里反过来扫源码：凡是函数体里提到 `uow` 的 `build_*`，都必须在列表里。

        判据为什么是"提到 uow"而不是"建了连接"：
        `build_kernel` 与 `build_control_plane` 也自己 `pg_connection(config)`，
        但它们**不跑 tick** —— 事务边界由调用它们的那个进程 builder 负责。
        用"建连接"当判据会把这两个拉进来，于是列表里混进不该有的名字，
        下一次真少了一个的时候反而看不出来。
        """
        source = (ROOT / "apps" / "_bootstrap.py").read_text(encoding="utf-8")
        found = []
        heads = [
            i for i in range(len(source)) if source.startswith("def build_", i)
        ]
        for i, head in enumerate(heads):
            name = source[head:].split("(", 1)[0][len("def "):]
            nxt = heads[i + 1] if i + 1 < len(heads) else len(source)
            body = source[head:nxt]
            if "uow" in body:
                found.append(name)
        self.assertTrue(found, "没扫到任何一个 build_*，扫描逻辑自己失效了")
        self.assertEqual(
            sorted(set(found) - set(self._BUILDERS)),
            [],
            "这些 builder 自己建连接却不在 _BUILDERS 里，没人检查它们的事务边界",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
