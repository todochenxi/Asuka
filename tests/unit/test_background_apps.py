"""M21：apps/ 进程层 · 三个常驻扫进程。

    PR-11  有内部节奏的对象必须跨 tick 存活 —— 否则兜底路径变成死代码
    PR-12  收敛型进程不需要领地；判据是"这个动作重复做会不会改变结果"
    PR-13  唤醒谓词必须每轮现取审批结果（A-10 在进程层的落点）

`apps/outbox_publisher` 那一半（PR-1~PR-10）在 `test_outbox_publisher_app.py`。
这里只测"复用同一个进程骨架跑另外三个扫进程"这件事，
以及每个进程各自**独有**的那条不变量 —— 通用生命周期不再重复测。

每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from apps.cancellation_sweeper import (
    CancellationSweeperApp,
    CancellationSweeperConfig,
)
from apps.recovery_controller import (
    RecoveryControllerApp,
    RecoveryControllerConfig,
)
from apps.wakeup_controller import WakeupControllerApp, WakeupControllerConfig
from packages.agent_domain.execution import (
    ExecutionStatus,
    SuspensionReason,
)
from packages.execution_kernel import (
    ExecutionKernel,
    InMemoryExecutionRepository,
    InMemoryOutbox,
    KernelConfig,
    ManualClock,
    Scheduler,
)
from packages.execution_kernel.adapters.redis import RedisLeaseIndex
from packages.execution_kernel.cancellation import CancellationService
from packages.execution_kernel.recovery_controller import RecoveryController
from packages.execution_kernel.wakeup_controller import (
    WakeupController,
    approval_satisfied,
)

from .fake_redis import FakeRedis
from .helpers import make_task

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NO_SLEEP = lambda _seconds: None  # noqa: E731


class SpyRepository(InMemoryExecutionRepository):
    """数一数 `list_with_expired_lease` 被调了几次。

    这是"PG 安全网有没有真的扫过"的唯一外部可见证据 ——
    `scan()` 走 Redis 索引时一次都不会碰到它。
    """

    def __init__(self) -> None:
        super().__init__()
        self.expired_calls = 0

    def list_with_expired_lease(self, now, limit: int = 100):
        self.expired_calls += 1
        return super().list_with_expired_lease(now, limit)


def build_kernel(*, ttl_seconds: int = 30):
    clock = ManualClock()
    repo = SpyRepository()
    kernel = ExecutionKernel(
        repository=repo,
        outbox=InMemoryOutbox(),
        clock=clock,
        lease_index=RedisLeaseIndex(FakeRedis()),
        config=KernelConfig(default_lease_ttl=timedelta(seconds=ttl_seconds)),
    )
    return kernel, Scheduler(kernel), clock, repo


def running_execution(kernel, scheduler) -> str:
    """submit → dispatch → RUNNING。

    `suspend` 只接受 RUNNING（E-1：PENDING 还没开工，谈不上"挂起"），
    所以唤醒相关的用例都得先真的开工。
    """
    execution = kernel.submit(make_task())
    scheduler.dispatch(worker_id="w1", limit=1)
    return execution.execution_id


# ---------------------------------------------------------------------------
# PR-11：控制器实例必须跨 tick 存活
# ---------------------------------------------------------------------------


class RecoveryProcessTest(unittest.TestCase):
    def test_pr11_the_pg_safety_net_fires_on_schedule(self) -> None:
        """sweep_every=3，跑 6 轮 → 第 3、6 轮各兜底扫一次 PG。"""
        kernel, _, clock, repo = build_kernel()
        app = RecoveryControllerApp(
            kernel=kernel,
            config=RecoveryControllerConfig(sweep_every=3),
            clock=clock,
            sleep=_NO_SLEEP,
        )
        app.run(max_ticks=6)
        self.assertEqual(repo.expired_calls, 2)

    def test_pr11_the_control_a_fresh_controller_every_tick_never_sweeps(self) -> None:
        """控制组：每轮新建控制器，`ticks` 永远是 1 → 安全网一次都不触发。

        所以上一条的 2 次不是"反正都会扫"。这正是最容易写错的形式：
        控器看起来无状态（`run_once()` 就一个调用），其实它带着节奏。
        """
        kernel, _, _, repo = build_kernel()
        for _ in range(6):
            RecoveryController(kernel=kernel, sweep_every=3).run_once()
        self.assertEqual(repo.expired_calls, 0)

    def test_pr11_the_safety_net_is_what_catches_a_lost_redis(self) -> None:
        """Redis 索引被清空 → 快路径扫不到任何东西，只有 PG 兜底能救回来。"""
        kernel, scheduler, clock, repo = build_kernel()
        execution = kernel.submit(make_task())
        scheduler.dispatch(worker_id="w1", limit=1)

        clock.advance(timedelta(seconds=31))
        kernel.lease_index.clear()                 # Redis 全丢

        app = RecoveryControllerApp(
            kernel=kernel,
            config=RecoveryControllerConfig(sweep_every=1),
            clock=clock,
            sleep=_NO_SLEEP,
        )
        app.run(max_ticks=1)

        self.assertEqual(repo.expired_calls, 1)
        self.assertEqual(
            app.controller.last_recovered, [execution.execution_id]
        )
        # E-23：救回来的路径是新 Attempt + 新 fencing_token，不是原地复活
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.RUNNING)


# ---------------------------------------------------------------------------
# PR-13：唤醒谓词每轮现取
# ---------------------------------------------------------------------------


class WakeupProcessTest(unittest.TestCase):
    def _approve_me(self, kernel, scheduler, approver: str = "risk-team") -> str:
        execution_id = running_execution(kernel, scheduler)
        kernel.suspend(
            execution_id,
            reason=SuspensionReason.HUMAN_APPROVAL,
            wait_condition={"approver": approver},
        )
        return execution_id

    def test_pr13_an_approval_that_lands_midflight_wakes_on_the_next_tick(self) -> None:
        kernel, scheduler, clock, _ = build_kernel()
        approvals: dict[str, str] = {}
        app = WakeupControllerApp(
            kernel=kernel,
            approvals=lambda: dict(approvals),
            clock=clock,
            sleep=_NO_SLEEP,
        )
        execution_id = self._approve_me(kernel, scheduler)

        self.assertEqual(app.tick(), 0)                  # 还没人审
        approvals["risk-team"] = "approved"              # 审批落库
        self.assertEqual(app.tick(), 1)                  # 下一轮就被扫到
        self.assertEqual(kernel.status_of(execution_id), ExecutionStatus.PENDING)

    def test_pr13_the_control_a_snapshotted_predicate_never_sees_it(self) -> None:
        """控制组：谓词在构造时快照一次 → 审批到了也永远唤不醒。

        所以上一条的"下一轮就被扫到"不是因为别的原因。
        而快照写法的现象极难排查：审批系统显示已通过，执行却一直挂着。
        """
        kernel, scheduler, _, _ = build_kernel()
        approvals: dict[str, str] = {}
        controller = WakeupController(kernel)
        snapshot = approval_satisfied(dict(approvals))    # ← 快照

        self._approve_me(kernel, scheduler)
        self.assertEqual(controller.run_once(snapshot), [])
        approvals["risk-team"] = "approved"
        self.assertEqual(controller.run_once(snapshot), [])   # 依然唤不醒

    def test_pr13_a_timer_wakes_only_when_its_time_comes(self) -> None:
        kernel, scheduler, clock, _ = build_kernel(ttl_seconds=600)
        execution_id = running_execution(kernel, scheduler)
        kernel.suspend(
            execution_id,
            reason=SuspensionReason.TIMER,
            wait_condition={"at": T0 + timedelta(seconds=60)},
        )
        app = WakeupControllerApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP)

        self.assertEqual(app.tick(), 0)                       # 现在才 T0
        clock.advance(timedelta(seconds=60))
        self.assertEqual(app.tick(), 1)
        self.assertEqual(kernel.status_of(execution_id), ExecutionStatus.PENDING)


# ---------------------------------------------------------------------------
# PR-12：收敛型进程不需要领地
# ---------------------------------------------------------------------------


class CancellationSweeperTest(unittest.TestCase):
    def test_pr12_two_sweepers_converge_to_the_same_result(self) -> None:
        """两个 sweeper 扫同一批：第一个判死，第二个是 no-op，不会二次生效。"""
        kernel, _, clock, _ = build_kernel()
        execution = kernel.submit(make_task())
        CancellationService(kernel).request(execution.execution_id, reason="stop", by="alice")

        first = CancellationSweeperApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP)
        second = CancellationSweeperApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP)

        self.assertEqual(first.tick(), 1)
        self.assertEqual(second.tick(), 0)          # 已终态 → 收敛，不是又取消一次
        self.assertEqual(
            kernel.status_of(execution.execution_id), ExecutionStatus.CANCELLED
        )

    def test_pr12_a_running_execution_with_a_live_lease_is_left_to_the_worker(self) -> None:
        """协作式取消优先：Lease 有效时 sweeper 不抢 Worker 的活。"""
        kernel, scheduler, clock, _ = build_kernel()
        execution = kernel.submit(make_task())
        scheduler.dispatch(worker_id="w1", limit=1)
        CancellationService(kernel).request(execution.execution_id, reason="stop", by="alice")

        app = CancellationSweeperApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP)
        self.assertEqual(app.tick(), 0)
        self.assertEqual(kernel.status_of(execution.execution_id), ExecutionStatus.RUNNING)

    def test_pr12_the_control_once_the_lease_expires_the_sweeper_takes_it(self) -> None:
        """控制组：Lease 一过期，同一条立刻被判死。

        所以上一条的 0 不是"sweep 什么都不做"，而是它真的在等 Worker。
        """
        kernel, scheduler, clock, _ = build_kernel()
        execution = kernel.submit(make_task())
        scheduler.dispatch(worker_id="w1", limit=1)
        CancellationService(kernel).request(execution.execution_id, reason="stop", by="alice")

        clock.advance(timedelta(seconds=31))
        app = CancellationSweeperApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP)
        self.assertEqual(app.tick(), 1)
        self.assertEqual(
            kernel.status_of(execution.execution_id), ExecutionStatus.CANCELLED
        )


# ---------------------------------------------------------------------------
# 骨架复用
# ---------------------------------------------------------------------------


class SkeletonReuseTest(unittest.TestCase):
    def test_all_three_run_on_the_same_skeleton(self) -> None:
        """三个扫进程共用 `ProcessRuntime`：同一套生命周期 / 退避 / 健康语义。"""
        kernel, _, clock, _ = build_kernel()
        apps = [
            RecoveryControllerApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP),
            WakeupControllerApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP),
            CancellationSweeperApp(kernel=kernel, clock=clock, sleep=_NO_SLEEP),
        ]

        for app in apps:
            report = app.run(max_ticks=2)
            self.assertEqual(report.state.value, "stopped")
            self.assertEqual(report.ticks, 2)
            # 停了就不再是 alive —— 健康检查报的是"这一刻"，不是"曾经"
            self.assertFalse(app.health()["alive"])
            self.assertEqual(app.health()["state"], "stopped")

    def test_an_idle_sweeper_backs_off_instead_of_spinning(self) -> None:
        """没活的时候退避，不是把 PG 打成热点。"""
        kernel, _, clock, _ = build_kernel()
        sleeps: list[float] = []
        app = CancellationSweeperApp(
            kernel=kernel,
            config=CancellationSweeperConfig(idle_sleep=1.0, max_idle_sleep=4.0),
            clock=clock,
            sleep=sleeps.append,
        )
        app.run(max_ticks=8)
        self.assertEqual(len(sleeps), 8)
        self.assertEqual(max(sleeps), 4.0)          # 顶到上限（PR-7）
