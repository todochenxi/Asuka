"""Cancellation 的三段式。

    Durable Intent      PostgreSQL：Execution.cancellation_requested = True（事实来源）
    Fast Signal         Redis：execution:{id}:cancel = 1（低延迟通知，**不是**事实来源）
    Runtime Propagation CancellationToken：Worker 在协作点检查

E-17  Cancellation 生命周期管理属 Kernel
X-11  Harness 只能发起请求，不能自己把状态改成 CANCELLED

取消是**协作式**的：Worker 必须在安全点检查 Token。
已经发出去的外部副作用不会被"取消"抹掉 —— 那要靠 Idempotency + 补偿（Saga，M10）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from packages.agent_domain.execution import ExecutionStatus

if TYPE_CHECKING:  # pragma: no cover
    from .kernel import ExecutionKernel

#: 需要"等 Worker 协作"的状态 —— 只有它们可能在真的跑
_AWAITING_COOPERATION = frozenset({ExecutionStatus.RUNNING})


@dataclass(frozen=True)
class CancellationToken:
    """传给 Worker 的运行期传播载体。

    Worker 在安全点调用 `is_cancelled()`；不检查就无法取消（这是协作式取消的代价）。
    """

    execution_id: str
    _check: Callable[[], bool]

    def is_cancelled(self) -> bool:
        return self._check()


@dataclass
class CancellationService:
    kernel: "ExecutionKernel"

    def request(
        self, execution_id: str, *, reason: str = "", by: str = ""
    ) -> None:
        """Harness / Runtime / User 发起取消请求。

        M48 / 空洞 228：`reason` / `by` 必填（由聚合根按 B-8 / A-8 校验）。
        在此之前这一层只有 Execution 上一个布尔位，于是"谁叫停、为什么"
        在整个 Execution 级链路上无处可写。
        """
        self.kernel.request_cancel(execution_id, reason=reason, by=by)

    def is_requested(self, execution_id: str) -> bool:
        """快路径：先看 Redis 信号，再回落到 PG 事实（信号可能丢失）。"""
        signals = self.kernel.cancel_signals
        if signals is not None and signals.get(execution_id):
            return True
        execution = self.kernel.repository.get(execution_id)
        return bool(execution and execution.cancellation_requested)

    def token_for(self, execution_id: str) -> CancellationToken:
        return CancellationToken(
            execution_id=execution_id,
            _check=lambda: self.is_requested(execution_id),
        )

    def cancel(self, execution_id: str, *, token: int | None = None) -> None:
        """Kernel 执行真正的取消（Worker 响应 / 系统级终止）。"""
        self.kernel.cancel(execution_id, token=token)

    def sweep(self) -> list[str]:
        """把"取消意图"推进到"终态" —— 少了这一环，取消会悬在半空。

        为什么必须存在：`request_cancel()` 只写意图（PG 的 `cancellation_requested`），
        真正改状态靠两条路：
          · Worker 在安全点检查 Token（协作式，只对**正在跑**的有意义）
          · 本方法（系统级，兜住所有"根本没人会来响应"的情况）

        判定规则很朴素：**只有真正持有有效 Lease 的 RUNNING 才需要等 Worker 协作；
        其余一律直接判死。**

            PENDING    还没开工 → 立刻 CANCELLED
            SUSPENDED  没在跑   → 立刻 CANCELLED
            STALE      Worker 已被判定失联 → 立刻 CANCELLED
            RUNNING + Lease 已过期 → Worker 其实已经不算持有者了 → CANCELLED
            RUNNING + Lease 有效     → 继续等（Worker 会在安全点响应）

        这里**没有**额外的 grace 参数（M21 删掉了一个写了但没生效的）：
        "给 Worker 多久时间响应"这件事已经由 Lease 表达了 ——
        两种宽限期并存会互相掩盖，出事时没人说得清到底等了多久。
        """
        now = self.kernel.clock.now()
        cancelled: list[str] = []
        for status in (
            ExecutionStatus.PENDING,
            ExecutionStatus.SUSPENDED,
            ExecutionStatus.STALE,
            ExecutionStatus.RUNNING,
        ):
            for execution in self.kernel.repository.list_by_status(status):
                if not execution.cancellation_requested:
                    continue
                if status is ExecutionStatus.RUNNING and execution.lease is not None:
                    if not execution.lease.is_expired(now):
                        continue                    # 交给 Worker 协作取消
                self.kernel.cancel(execution.execution_id)
                cancelled.append(execution.execution_id)
        return cancelled
