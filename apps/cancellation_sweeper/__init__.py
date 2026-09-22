"""cancellation_sweeper 进程（M21）。

取消意图 → 终态收敛（§15 第四段）。常驻部署单元。

判定规则在 `packages/execution_kernel/cancellation.py::CancellationService.sweep`：
只有持有**有效 Lease** 的 RUNNING 才等 Worker 协作，其余一律直接判死。
"""
from .app import CancellationSweeperApp, CancellationSweeperConfig

__all__ = ["CancellationSweeperApp", "CancellationSweeperConfig"]
