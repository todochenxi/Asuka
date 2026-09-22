"""python -m apps.wakeup_controller"""
from __future__ import annotations

from apps._bootstrap import RuntimeConfig, build_wakeup_controller, stop_signal
from apps._entrypoint import main


def build():
    config = RuntimeConfig.from_env()
    # 审批结果由存储提供（A-10 / PR-13）；这里尚未接线，接上后从这里读。
    return build_wakeup_controller(config, approvals=None, signal=stop_signal())


if __name__ == "__main__":
    main(build)
