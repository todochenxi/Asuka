"""探针的实现。

纯层（`check_live`）与库层（`check_ready`）分开，理由与迁移器一样：
**"文件多旧"这件事不需要数据库就能测**，于是它不需要一台 PG 才能被验证。

一条容易踩的规矩：这里**不**自己拼连接。`check_ready` 复用
`apps.migrate.connect` —— 于是"连不上库"这句话在整个系统里只有一种说法
（`NO_DSN` / `NO_PG_DRIVER` / `CONNECT_FAILED`），
而 DSN 里的口令也只在一个地方被抹掉（B-7）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

#: 心跳超过这个秒数没跳 = 判死。
#:
#: ⚠️ 它**必须严格大于**每一个进程的 `max_idle_sleep`，否则一个
#: 只是在空闲退避中的进程会被当成死的 —— 而 `live` 红的代价是真的重启。
#:
#: 这不是理论风险：本机第一版写的是 30.0，而 `cancellation_sweeper` 与
#: `run_cancellation_sweeper` 的 `max_idle_sleep` 恰好也是 30.0。
#: 于是空闲时心跳刚好卡在阈值上，两个 sweeper 进入
#: "起来 → 空转 → 判死 → 重启"的循环，日志里每条都是
#: `signal ticks=42 work=0` —— 一个**正常退出**的进程被反复重启，
#: 而没有任何一条日志指向探活。
#:
#: 取 90.0 = 最大退避（30 秒）的 3 倍：一轮慢查询 + 一次 GC 停顿
#: 都吃不掉这个余量。这条约束由 `tests/unit/test_probe.py` 看着 ——
#: 以后谁把某个进程的退避调到 60 秒以上，那条测试会红。
DEFAULT_LIVE_MAX_AGE = 90.0


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的结果。

    `detail` 不是给人看着玩的：它会进 `kubectl describe pod` 的 Events。
    一句 "not ok" 在那里等于什么都没说。
    """

    ok: bool
    detail: str = ""


def check_live(path: str, max_age: float = DEFAULT_LIVE_MAX_AGE) -> ProbeResult:
    """心跳还新鲜吗？"""
    if not path:
        return ProbeResult(
            False,
            "no heartbeat file configured (set AGENTOS_HEARTBEAT_FILE)",
        )
    if not os.path.exists(path):
        return ProbeResult(False, f"heartbeat file {path} does not exist")

    age = time.time() - os.path.getmtime(path)
    if age > max_age:
        return ProbeResult(
            False, f"heartbeat is {age:.1f}s old (limit {max_age:.1f}s)"
        )
    return ProbeResult(True, f"heartbeat is {age:.1f}s old")


def check_ready(
    dsn: str,
    *,
    connect: Callable[[str], Any] | None = None,
) -> ProbeResult:
    """依赖还能连吗？

    `connect` 只在测试里注入 —— 生产走 `apps.migrate.connect`，
    于是"连库"这件事（含口令脱敏与缺驱动的报错）只有一份实现。
    """
    if not dsn:
        from apps._dsn import hint

        return ProbeResult(False, f"no dsn configured; {hint()}")

    opener = connect
    if opener is None:
        from apps.migrate import connect as _connect  # 惰性：别把迁移器拖进 import

        opener = _connect

    try:
        conn = opener(dsn)
    except Exception as e:
        # `MigrationError` 已经把口令抹掉了；别的异常原样说出类型
        return ProbeResult(False, f"cannot connect: {e}")

    try:
        conn.cursor().execute("SELECT 1")
        return ProbeResult(True, "database reachable")
    except Exception as e:
        return ProbeResult(False, f"database query failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
