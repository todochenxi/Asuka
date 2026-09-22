"""python -m apps.recovery_controller"""
from __future__ import annotations

from apps._bootstrap import RuntimeConfig, build_recovery_controller, stop_signal
from apps._entrypoint import main


def build():
    config = RuntimeConfig.from_env()
    return build_recovery_controller(config, signal=stop_signal())


if __name__ == "__main__":
    main(build)
