"""`python -m apps.cli` 的入口（M50）。

只做一件事：把退出码交给 shell。解析与子命令都在 `apps.cli` 里，
这样测试可以直接 `from apps.cli import main` 而不用起子进程。
"""
from __future__ import annotations

import sys

from apps.cli import main

if __name__ == "__main__":
    sys.exit(main())
