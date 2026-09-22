"""wakeup_controller 进程（M21）。

Timer / Event / Approval 唤醒，常驻部署单元。

判定逻辑在 `packages/execution_kernel/wakeup_controller.py`，
这里负责每轮**现场组装**唤醒谓词 —— 尤其是审批结果必须每轮从存储读（A-10）。
"""
from .app import WakeupControllerApp, WakeupControllerConfig

__all__ = ["WakeupControllerApp", "WakeupControllerConfig"]
