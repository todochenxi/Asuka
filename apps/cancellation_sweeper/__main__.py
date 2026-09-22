"""python -m apps.cancellation_sweeper"""
from __future__ import annotations

from apps._bootstrap import RuntimeConfig, build_cancellation_sweeper, stop_signal
from apps._entrypoint import main


def build():
    config = RuntimeConfig.from_env()
    return build_cancellation_sweeper(config, signal=stop_signal())


if __name__ == "__main__":
    main(build)
