"""python -m apps.outbox_publisher"""
from __future__ import annotations

from apps._bootstrap import RuntimeConfig, build_outbox_publisher, stop_signal
from apps._entrypoint import main


def build():
    config = RuntimeConfig.from_env()
    return build_outbox_publisher(config, signal=stop_signal())


if __name__ == "__main__":
    main(build)
