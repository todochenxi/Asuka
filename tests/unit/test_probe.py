"""M68 · 进程探针。

要害是**两种红要分得开**：

    库挂了  → ready 红、live 绿   摘流量，别杀（杀了也连不上）
    进程卡了 → live 红            重启（新进程能重新连库）

合成一个结果，这两种情况就被迫得到同一种处置，
而"一律重启"在库挂掉时会让八个进程一起 CrashLoopBackOff。
"""
from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from apps._runtime import ManualStop, ProcessRuntime
from apps.probe import DEFAULT_LIVE_MAX_AGE, check_live, check_ready

from . import fake_fastapi


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


class TestLive(unittest.TestCase):
    def test_a_fresh_heartbeat_passes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            open(path, "w").close()
            r = check_live(path)
            self.assertTrue(r.ok)
            self.assertIn("old", r.detail)

    def test_a_stale_heartbeat_fails_and_says_how_stale(self):
        """`kubectl describe pod` 只看得到这一句 —— 它必须带数字。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            open(path, "w").close()
            old = time.time() - (DEFAULT_LIVE_MAX_AGE + 5)
            os.utime(path, (old, old))

            r = check_live(path)
            self.assertFalse(r.ok)
            self.assertIn("old", r.detail)
            self.assertIn("limit", r.detail)

    def test_a_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as d:
            r = check_live(os.path.join(d, "nope"))
            self.assertFalse(r.ok)
            self.assertIn("does not exist", r.detail)

    def test_no_path_configured_is_not_silently_ok(self):
        """"忘了配"必须看得见。默认 200 会让八个容器的 liveness 全是摆设。"""
        r = check_live("")
        self.assertFalse(r.ok)
        self.assertIn("AGENTOS_HEARTBEAT_FILE", r.detail)

    def test_the_limit_is_configurable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            open(path, "w").close()
            old = time.time() - 2.0
            os.utime(path, (old, old))
            self.assertFalse(check_live(path, max_age=1.0).ok)
            self.assertTrue(check_live(path, max_age=10.0).ok)


# ---------------------------------------------------------------------------
# ready
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, boom: bool = False) -> None:
        self.boom = boom

    def execute(self, sql: str, params: Any = None) -> None:
        if self.boom:
            raise RuntimeError("connection reset by peer")


class _FakeConn:
    def __init__(self, boom: bool = False) -> None:
        self.boom = boom
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.boom)

    def close(self) -> None:
        self.closed = True


class TestReady(unittest.TestCase):
    def test_a_reachable_database_passes(self):
        r = check_ready("postgresql://x/y", connect=lambda dsn: _FakeConn())
        self.assertTrue(r.ok)

    def test_no_dsn_is_not_silently_ok(self):
        r = check_ready("")
        self.assertFalse(r.ok)
        self.assertIn("AGENTOS_PG_DSN", r.detail)

    def test_a_failing_query_fails_and_says_why(self):
        r = check_ready("postgresql://x/y", connect=lambda dsn: _FakeConn(boom=True))
        self.assertFalse(r.ok)
        self.assertIn("connection reset by peer", r.detail)

    def test_a_failing_connect_fails_and_says_why(self):
        def boom(dsn: str) -> Any:
            raise RuntimeError("could not translate host name")

        r = check_ready("postgresql://x/y", connect=boom)
        self.assertFalse(r.ok)
        self.assertIn("could not translate host name", r.detail)

    def test_the_connection_is_closed_even_when_the_query_fails(self):
        """探针每 10 秒跑一次。漏一次关闭，就是一个慢慢漏光的连接池。"""
        conn = _FakeConn(boom=True)
        check_ready("postgresql://x/y", connect=lambda dsn: conn)
        self.assertTrue(conn.closed)


# ---------------------------------------------------------------------------
# 心跳由主循环写
# ---------------------------------------------------------------------------


class TestHeartbeatIsWrittenByTheLoop(unittest.TestCase):
    def test_it_exists_right_after_startup(self):
        """起来就先跳一次 —— 否则"刚启动"会被当成"已经死了"。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            rt = ProcessRuntime(
                name="x", signal=ManualStop(), sleep=lambda _s: None,
                heartbeat_path=path,
            )
            rt.run(lambda: 0, max_ticks=0)
            self.assertTrue(os.path.exists(path))

    def test_it_is_stamped_after_every_tick(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            rt = ProcessRuntime(
                name="x", signal=ManualStop(), sleep=lambda _s: None,
                heartbeat_path=path,
            )
            rt.run(lambda: 1, max_ticks=1)
            first = os.path.getmtime(path)
            rt.run(lambda: 1, max_ticks=1)
            self.assertGreaterEqual(os.path.getmtime(path), first)
            self.assertTrue(check_live(path).ok)

    def test_a_failing_tick_does_not_beat(self):
        """一轮都跑不完，就不算在往前走。

        直接用 `_tick_once` 而不是 `run()`：后者开头会先跳一次（启动那下），
        那一下会把"失败没跳"这件事盖住，于是这条用例永远绿 ——
        不管 `_beat()` 是不是被错误地放进了失败分支。
        """
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beat")
            rt = ProcessRuntime(
                name="x", signal=ManualStop(), sleep=lambda _s: None,
                heartbeat_path=path, max_consecutive_failures=99,
            )
            rt.run(lambda: 0, max_ticks=0)      # 只做启动那一下
            baseline = os.path.getmtime(path)

            def boom() -> int:
                raise RuntimeError("db is down")

            time.sleep(0.02)
            rt._tick_once(boom)                  # 失败：不许跳
            self.assertAlmostEqual(os.path.getmtime(path), baseline, places=5)

            time.sleep(0.02)
            rt._tick_once(lambda: 1)             # 成功：要跳
            self.assertGreater(os.path.getmtime(path), baseline)

    def test_no_heartbeat_path_means_no_file_and_no_error(self):
        """没配就不写 —— 观测设施不该有能力拖垮业务进程。"""
        rt = ProcessRuntime(name="x", signal=ManualStop(), sleep=lambda _s: None)
        rt.run(lambda: 1, max_ticks=2)
        self.assertEqual(rt.ticks, 2)

    def test_a_bad_heartbeat_path_does_not_kill_the_process(self):
        rt = ProcessRuntime(
            name="x", signal=ManualStop(), sleep=lambda _s: None,
            heartbeat_path="/no/such/dir/beat",
        )
        report = rt.run(lambda: 1, max_ticks=2)
        self.assertEqual(report.ticks, 2)
        self.assertEqual(report.work, 2)


# ---------------------------------------------------------------------------
# 阈值 vs 空闲退避：一条被真实部署打出来的约束
# ---------------------------------------------------------------------------


class TestTheLimitSurvivesIdleBackoff(unittest.TestCase):
    """`DEFAULT_LIVE_MAX_AGE` 必须大于**每一个**进程的 `max_idle_sleep`。

    这条是被一次 CrashLoop 打出来的：阈值与 sweeper 的 30 秒退避相等，
    于是空闲中的进程被判死 → 重启 → 又空闲 → 又判死。
    日志里每条都是 `signal ticks=N work=0`（正常退出的样子），
    没有任何一行提到探活 —— 排查方向从一开始就错了。
    """

    _PATTERN = re.compile(r"max_idle_sleep:\s*float\s*=\s*([0-9.]+)")
    _ROOT = Path(__file__).resolve().parents[2]

    def _backoffs(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for path in sorted((self._ROOT / "apps").glob("*/app.py")):
            text = path.read_text(encoding="utf-8")
            found = self._PATTERN.search(text)
            if found:
                # 用**目录名**做键：这些文件都叫 app.py，
                # 用文件名的话整张表只剩一个条目，而"最坏的那个"也就找不到了
                out[path.parent.name] = float(found.group(1))
        return out

    def test_the_scanner_really_finds_them(self):
        """控制组：扫不到东西的话，下一条用例是"因为空集合才通过"的。"""
        backoffs = self._backoffs()
        self.assertGreaterEqual(len(backoffs), 5)
        self.assertIn("cancellation_sweeper", backoffs)

    def test_the_limit_is_strictly_greater_than_every_backoff(self):
        backoffs = self._backoffs()
        worst = max(backoffs.items(), key=lambda kv: kv[1])
        self.assertGreater(
            DEFAULT_LIVE_MAX_AGE,
            worst[1],
            f"{worst[0]} idles up to {worst[1]}s, but liveness gives up at "
            f"{DEFAULT_LIVE_MAX_AGE}s — an idle process would be killed",
        )


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


class TestReadyEndpoint(unittest.TestCase):
    """走真的 `build_app`，只是把框架换成替身。"""

    def setUp(self) -> None:
        fake_fastapi.install()
        self.addCleanup(fake_fastapi.uninstall)

    def _app(self, readiness: Any) -> Any:
        from apps.api.app import build_app
        from packages.agent_api.service import InProcessControlPlane

        # 探针不碰 control_plane，这里给一个最小的工厂即可
        plane = InProcessControlPlane(factory=lambda agent_id, approvals: None)
        return build_app(plane, readiness=readiness)

    def test_a_ready_probe_returns_200(self):
        from apps.probe.app import ProbeResult

        app = self._app(lambda: ProbeResult(True, "database reachable"))
        resp = app.call("GET", "/readyz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body["status"], "ok")

    def test_an_unready_probe_returns_503(self):
        from apps.probe.app import ProbeResult

        app = self._app(lambda: ProbeResult(False, "cannot connect: timeout"))
        resp = app.call("GET", "/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.body["status"], "unavailable")
        self.assertIn("timeout", resp.body["detail"])

    def test_no_probe_configured_is_503_not_200(self):
        """200 是"可以放流量进来"。说不出依据的 200 比 503 危险得多。"""
        app = self._app(None)
        resp = app.call("GET", "/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("no readiness probe", resp.body["detail"])

    def test_health_still_only_answers_liveness(self):
        """存活端点不许顺手把就绪也答了 —— PR-8 的分界不能被抹掉。"""
        app = self._app(lambda: None)
        self.assertEqual(app.call("GET", "/health"), {"status": "ok"})
