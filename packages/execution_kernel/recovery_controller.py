"""Recovery Controller：把 Lease 过期的 Execution 救回来。

流程（E-23）：

    Lease Expired
        ↓
    Execution = STALE          （不是简单回到 QUEUED）
        ↓
    Recovery                    （开新 Attempt + 新 fencing_token）
        ↓
    RUNNING                     （重新可被心跳 / 完成）

它**不做** Agent Replanning，也**不做** Retry 决策：
    Retry     = 失败后要不要再试（RetryPolicy + FailureClass）
    Recovery  = 出了故障如何救回 Execution（本文件）
    Replanning= Agent 下一步怎么办（Runtime）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from packages.agent_domain.errors import LeaseRequired
from packages.agent_domain.execution import ExecutionStatus

if TYPE_CHECKING:  # pragma: no cover
    from .kernel import ExecutionKernel


@dataclass
class RecoveryController:
    kernel: "ExecutionKernel"
    worker_id: str = "recovery-controller"
    default_ttl: timedelta | None = None
    sweep_every: int = 10
    """每 N 个周期额外做一次 PG 全量兜底扫。

    为什么不能"索引空了就回落到 PG"：正常空闲时索引本来就是空的，
    那样等于每轮都全表扫，索引就白建了。
    正确做法是**低频兜底** —— Redis 丢了最多让 STALE 晚 N 个周期被发现，
    不会永远漏掉。Redis 是快路径，PG 是安全网。
    """
    ticks: int = 0
    last_scan: list[str] = field(default_factory=list)
    last_recovered: list[str] = field(default_factory=list)

    def scan(self) -> list[str]:
        """快路径：走 Redis 到期索引（没有索引时直接退化为 PG 扫）。"""
        return self._mark(self._candidates())

    def sweep(self) -> list[str]:
        """安全网：直接扫 PG。慢，但不依赖 Redis。"""
        return self._mark(self.kernel.repository.list_with_expired_lease(self.kernel.clock.now()))

    def _mark(self, candidates) -> list[str]:
        stale: list[str] = []
        for execution in candidates:
            try:
                if self.kernel.mark_stale(execution.execution_id):
                    stale.append(execution.execution_id)
            except LeaseRequired:
                continue
        return stale

    def _candidates(self):
        index = getattr(self.kernel, "lease_index", None)
        if index is None:
            return self.kernel.repository.list_with_expired_lease(self.kernel.clock.now())

        repository = self.kernel.repository
        found = []
        for execution_id in index.due(self.kernel.clock.now()):
            execution = repository.get(execution_id)
            if execution is None or execution.lease is None:
                index.forget(execution_id)          # 索引里有脏条目，顺手清掉
                continue
            # 索引可能过期/被驱逐，"是否真的过期"只有 PG 说了算
            if execution.lease.is_expired(self.kernel.clock.now()):
                found.append(execution)
        return found

    def recover(self, execution_ids: list[str] | None = None) -> list[str]:
        """STALE → 新 Attempt + 新 fencing_token → RUNNING。"""
        targets = execution_ids or [
            e.execution_id
            for e in self.kernel.repository.list_by_status(ExecutionStatus.STALE)
        ]
        recovered: list[str] = []
        for execution_id in targets:
            self.kernel.recover(
                execution_id,
                worker_id=self.worker_id,
                ttl=self.default_ttl,
            )
            recovered.append(execution_id)
        self.last_recovered = recovered
        return recovered

    def run_once(self) -> dict[str, list[str]]:
        """一个调度周期：索引快扫（+ 周期性 PG 兜底），再 recover。"""
        self.ticks += 1
        stale = self.scan()
        if self.sweep_every and self.ticks % self.sweep_every == 0:
            for execution_id in self.sweep():
                if execution_id not in stale:
                    stale.append(execution_id)
        self.last_scan = stale
        return {"stale": stale, "recovered": self.recover()}
