"""python -m apps.child_run_consumer"""
from __future__ import annotations

from apps._bootstrap import RuntimeConfig, build_child_run_consumer, stop_signal
from apps._entrypoint import main


def build():
    config = RuntimeConfig.from_env()
    return build_child_run_consumer(config, signal=stop_signal())


if __name__ == "__main__":
    main(build)
