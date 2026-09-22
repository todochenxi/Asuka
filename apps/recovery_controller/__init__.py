"""recovery_controller 进程（M21）。

STALE 检测 + Recovery 触发，常驻部署单元。

业务判断全在 `packages/execution_kernel/recovery_controller.py`：
快路径走 Redis 到期索引，低频兜底扫 PG，STALE → 新 Attempt + 新 fencing_token。

这里只回答"要不要跑下一轮、多久跑一次、什么时候停"。
"""
from .app import RecoveryControllerApp, RecoveryControllerConfig

__all__ = ["RecoveryControllerApp", "RecoveryControllerConfig"]
