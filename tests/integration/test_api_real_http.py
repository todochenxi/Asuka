"""真 HTTP 层：`apps/api` 的冒烟固化（M45 / 空洞 211）。

--------------------------------------------------------------------------
这一层为什么必须存在

到 M44 为止，"路由有没有把 `Idempotency-Key` 传给 handler"这件事
是**扫源码**验的（`tests/unit/test_cancel_idempotency.py` 里那段 AST 扫描）。
§82.7 当时就把话写死了：

    它挡得住删漏，挡不住"传错了"。真跑一遍要等 211。

那不是谦虚，是陈述事实。扫源码能证明的是"这句参数声明在文件里"，
它证明不了的是 **FastAPI 真的把它从 HTTP 头上取下来了** ——
两者之间隔着一层框架绑定，而那层绑定从来没被执行过一次。

同一个毛病在 M29 已经踩过一次（`pg_connection` 的 docstring 里记着）：
`row_factory=dict_row` 缺了会让生产路径 500，而**单元测试一直是绿的**，
因为替身（`sqlite_shim`）设了 `sqlite3.Row`、集成层的 `real_pg()` 自己设了
`dict_row`。结论是那句：**替身比生产更好用，而生产那条路径从来没跑过。**

--------------------------------------------------------------------------
这一层刻意不做什么

它**不替换** `tests/unit/test_http_api.py`。那一层走真的 handler、
不经过 fastapi（A-7 / PR-22）—— 领域不许知道框架。
这一层走**真的 fastapi**。两层都在，因为它们验的是两件事：

    unit    业务语义：给一个 body，handler 给出什么
    本层    传输语义：HTTP 头 / 状态码 / 框架绑定 / 事务边界

--------------------------------------------------------------------------
连接必须是生产那一个

`build_api(config)` 不注入 `conn` 时自己调 `pg_connection(config)`，
那是 **autocommit=False + dict_row**（M29 起）。这里刻意**照抄**它，
而不是把 `real_pg()` 那条 autocommit 连接递进去：

    递 autocommit 连接 ⟹ 写不用 commit 也落库 ⟹
    事务中间件就算整个不存在，这一层也全绿。

那是把 X-3 / PR-31 在替身里判成通过（PR-28）。
所以下面有一条专门盯它：请求结束后，**另一个连接**能看见那一行。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from apps._bootstrap import RuntimeConfig, build_api, pg_connection

from ._pg import RealPostgresCase, dsn, real_pg

ROOT = Path(__file__).resolve().parents[2]

#: 带审批的演示栈：第 2 步会被治理层拦下（`policy[risk-gate]`）。
#: 页面那条"人在环"的路只有它能走到（I-9）。
STACK_PROVIDER = "examples.demo_stack:build_approval_demo_stack_factory"

BODY = {"reason": "user asked", "by": "alice"}


def _frozen_baseline_version() -> str:
    """冻结基线文档里那个版本号 —— 与 `test_http_api` 那条同源（B-7）。"""
    docs = sorted(p.name for p in ROOT.glob("AgentOS_*_最终冻结版.md"))
    assert len(docs) == 1, f"应当只有一份冻结基线，现在是：{docs}"
    found = re.search(r"v(\d+\.\d+\.\d+)", docs[0])
    assert found is not None, docs[0]
    return found.group(1)


class RealHttpCase(RealPostgresCase):
    """真 FastAPI app + TestClient + 生产同款连接。"""

    def setUp(self) -> None:
        super().setUp()
        self.config = RuntimeConfig(pg_dsn=dsn(), stack_provider=STACK_PROVIDER)
        # ⚠️ 不是 self.conn —— 见本模块 docstring 最后一节。
        self.api_conn = pg_connection(self.config)
        self.addCleanup(self.api_conn.close)
        self.app = build_api(self.config, conn=self.api_conn)
        self.client = _client(self.app)
        self.addCleanup(self.client.close)

    # -- 快捷方式 ------------------------------------------------------
    def _start(self, *, key: str = "", request: str = "compute 6*7") -> dict:
        headers = {"Idempotency-Key": key} if key else {}
        r = self.client.post(
            "/agents/agent-it/runs",
            json={"user_request": request},
            headers=headers,
        )
        assert r.status_code in (200, 201), (r.status_code, r.text)
        return dict(r.json())

    def _cancel(self, run_id: str, *, key: str = "", **over) -> httpx.Response:
        body = dict(BODY)
        body.update(over)
        headers = {"Idempotency-Key": key} if key else {}
        return self.client.post(f"/runs/{run_id}/cancel", json=body, headers=headers)


def _client(app: object) -> object:
    from fastapi.testclient import TestClient

    return TestClient(app)


class TheProcessServesTest(RealHttpCase):
    """它起得来，而且那三个"不用业务也能问"的端点答得对。"""

    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_the_console_page_is_served(self) -> None:
        """`GET /console` 是控制台本身，`GET /` 是聊天页（M93）。

        目录不存在时 `_mount_console` 是**静默跳过**的（服务可用性不依赖
        演示页面）—— 于是"页面没了"在别处一个红灯都不会有。
        这一条就是那个缺失的红灯。

        ⚠️ 标题**和**控件一起验：只验标题的话，一个写着同名标题的占位页
        也能混过去 —— 而"页面在、但里面的东西没了"正是没人会报警的那种坏。
        """
        console = self.client.get("/console")
        self.assertEqual(console.status_code, 200, console.text[:200])
        self.assertIn("text/html", console.headers["content-type"])
        self.assertIn("<h1>AgentOS 控制台</h1>", console.text)
        self.assertIn('id="btn-cancel"', console.text)
        # M109：账本页的分层记录
        self.assertIn("分层记录", console.text)

        chat = self.client.get("/")
        self.assertEqual(chat.status_code, 200, chat.text[:200])
        self.assertIn("text/html", chat.headers["content-type"])
        self.assertIn("<h1>AgentOS 对话</h1>", chat.text)
        self.assertIn('id="send"', chat.text)

    def test_the_architecture_page_is_served(self) -> None:
        """`GET /architecture` 是架构与进度页（M100 §03/§04，M101 回写）。

        同 `/console` 那条：页面没了在别处一个红灯都不会有。
        这里除了标题，还钉住 §03/§04 两个锚点 —— 只验标题的话，
        一个只有标题的空页也能混过去。
        """
        r = self.client.get("/architecture")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("<h1>AgentOS · 架构与进度</h1>", r.text)
        self.assertIn("M 层路线图", r.text)
        self.assertIn("未落地的可做工项", r.text)

    def test_a_chat_run_is_listed_by_get_runs(self) -> None:
        """M110：聊天页开的 Run 必须能**被列出来** —— 否则它在控制台没有入口。"""
        chat = self.client.post(
            "/chat", json={"agent_id": "agent-chat", "message": "compute 6*7"}
        )
        self.assertEqual(chat.status_code, 200, chat.text)
        run_id = chat.json()["run_id"]

        runs = self.client.get("/runs")
        self.assertEqual(runs.status_code, 200, runs.text)
        ids = [r["run_id"] for r in runs.json()["items"]]
        self.assertIn(run_id, ids, "the chat run must appear in GET /runs")

    def test_the_metrics_endpoint_reports_queue_depth(self) -> None:
        """M107：`GET /metrics` 吐出 Prometheus 文本，含队列深度（KEDA 要读它）。"""
        r = self.client.get("/metrics")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("text/plain", r.headers["content-type"])
        self.assertIn("agentos_pending_executions", r.text)
        self.assertIn("# TYPE agentos_pending_executions gauge", r.text)

    def test_the_chat_endpoint_answers(self) -> None:
        """`POST /chat` 真的跑完一条 Run，并把模型回答带出来（M93）。

        用的是带审批的演示栈，但 `agent-chat` 被显式排除在闸门之外 ——
        所以它必须一步答完，而不是挂起等批准。
        """
        r = self.client.post(
            "/chat", json={"agent_id": "agent-chat", "message": "compute 6*7"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["answer"])
        self.assertIn("demo-1 received", body["answer"])

        # 回答是只读的派生事实，`GET /runs/{id}/answer` 必须给出同一份。
        again = self.client.get(f"/runs/{body['run_id']}/answer")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["answer"], body["answer"])

    def test_the_version_it_serves_is_the_frozen_baseline(self) -> None:
        """对外报的版本号 —— 这次是**服务真的答的**，不是源码里扫到的。

        上层那条（`test_http_api`）断言的是 `app.py` 里有这个字面量；
        这一条断言的是 `/openapi.json` 把它发出去了。
        中间隔的是 `fastapi.FastAPI(version=...)` 那一次绑定 ——
        绑定写错了（比如传给了 `title`），上层照样绿。
        """
        r = self.client.get("/openapi.json")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["info"]["version"], _frozen_baseline_version())


class TheHeaderIsReallyReadTest(RealHttpCase):
    """`Idempotency-Key` 是**框架**从 HTTP 头上取下来的 —— M45 的核心。

    这三条一起才成立：

        带键重试 ⟹ 200 replayed=True
        不带键重试 ⟹ 409
        带键换体 ⟹ 422

    少了中间那条控制组，第一条证明不了任何事 ——
    它可能只是"取消本来就返回 200"。
    """

    def test_the_cancel_key_replays_the_first_answer(self) -> None:
        run_id = self._start()["run_id"]
        first = self._cancel(run_id, key="k1")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])

        second = self._cancel(run_id, key="k1")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["status"], "cancelled")

    def test_the_control_without_the_header_it_is_a_409(self) -> None:
        """控制组：不带这个头，同一件事就是一次失败的取消（223 的原状）。

        它证明上一条里的 200 是**这个头**换来的，不是"取消本来就成功"。
        """
        run_id = self._start()["run_id"]
        self.assertEqual(self._cancel(run_id, key="k1").status_code, 200)
        retry = self._cancel(run_id)
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(retry.json()["error"]["code"], "RUN_TERMINAL")

    def test_a_reused_cancel_key_is_422(self) -> None:
        run_id = self._start()["run_id"]
        self._cancel(run_id, key="k1")
        reused = self._cancel(run_id, key="k1", reason="a different reason")
        self.assertEqual(reused.status_code, 422, reused.text)
        self.assertEqual(reused.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_the_start_key_replays_the_same_run(self) -> None:
        """同一个键 + **同一个请求体** ⟹ 同一条 Run，不是第二条（A-3）。

        注意"同一个请求体"这五个字：换了体就是复用，那是下面那一条的 422，
        不是回放。把这两件事分成两条断言，是因为它们曾经是同一件事 ——
        M44 之前换体也静默拿回旧 Run。
        """
        first = self._start(key="s1")
        second = self._start(key="s1")
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertTrue(second["replayed"])

    def test_a_reused_start_key_is_422(self) -> None:
        self._start(key="s1")
        reused = self.client.post(
            "/agents/agent-it/runs",
            json={"user_request": "another"},
            headers={"Idempotency-Key": "s1"},
        )
        # ⚠️ 注意：建 Run 的键复用在 M44 之前是**静默**的 ——
        # 它拿回旧 Run 而不是报错。这里要的是 422 那个新行为。
        self.assertEqual(reused.status_code, 422, reused.text)


class TheWriteReachesPostgresTest(RealHttpCase):
    """请求结束之后，写**真的**在库里（PR-31：一个请求 = 一个事务）。

    这一条是事务中间件的守卫，而且它只有在本层用 **autocommit=False**
    的连接时才成立 —— 用 autocommit 连接跑它，中间件删掉了也全绿。
    """

    def test_the_idempotency_row_survives_the_request(self) -> None:
        run_id = self._start()["run_id"]
        self.assertEqual(self._cancel(run_id, key="k1").status_code, 200)

        # 另一个连接、另一个事务 —— 只有真的 commit 了才看得见。
        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        with other.cursor() as cur:
            cur.execute(
                "SELECT durable FROM idempotency_keys WHERE key = %s", ("cancel:k1",)
            )
            row = cur.fetchone()
        self.assertIsNotNone(row, "请求提交了，可库里没有这一行 —— 事务边界没接上")
        self.assertTrue(row["durable"])

    def test_the_step_survives_the_request(self) -> None:
        """走一步产生的那批事件，请求结束之后**另一个连接**看得见。

        ⚠️ 这里查的是 `aggregate_type = 'execution'`，不是 run_id ——
        因为 Run 侧目前**唯一**落 outbox 的事件是"子 Run 完成"（X-15 那轮
        就记着这件事）。走一步落的是 execution / lease / attempt 那几条。

        这不是将就：这一条守的是"这一步的写在请求结束之后还在不在"，
        而不是"事件挂在哪一个聚合上"。后者是另一件事（它正是空洞 225
        至今没有答案的原因 —— 库里没有"这条 Run 存不存在"这一行）。
        """
        run_id = self._start()["run_id"]
        self.assertEqual(self.client.post(f"/runs/{run_id}/step").status_code, 200)

        other = real_pg(fresh=False)
        self.addCleanup(other.close)
        with other.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM outbox_events "
                "WHERE aggregate_type = 'execution'"
            )
            n = cur.fetchone()["n"]
        self.assertGreater(
            n, 0, "走了一步，outbox 里却一条事件都没有 —— 写没有真的提交"
        )


class ThePageFlowOverHttpTest(RealHttpCase):
    """页面那条完整流程，全部走 HTTP：建 → 走 → 被拦 → 批准 → 跑完。

    页面能点出这条流程，不等于这条流程**在 HTTP 上**走得通 ——
    它此前只在 unit 层（直接调 handler）被走过一次。
    """

    def test_the_whole_flow(self) -> None:
        created = self._start()
        run_id = created["run_id"]
        self.assertEqual(created["status"], "created")

        first = self.client.post(f"/runs/{run_id}/step")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "running")

        second = self.client.post(f"/runs/{run_id}/step")
        self.assertEqual(second.status_code, 200, second.text)
        # 治理层拦下的，不是智能体主动请示（I-9）
        self.assertEqual(second.json()["status"], "suspended")
        gate = second.json()["pending_approval"]
        self.assertIsNotNone(gate)
        self.assertIn("risk-gate", gate["question"])

        listed = self.client.get(f"/runs/{run_id}/approvals")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 1)

        decided = self.client.post(
            f"/runs/{run_id}/approvals/{gate['approval_id']}/decision",
            json={"decision": "approve", "by": "alice"},
        )
        self.assertEqual(decided.status_code, 200, decided.text)

        finished = self.client.post(f"/runs/{run_id}/run")
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["status"], "completed")

        trace = self.client.get(f"/runs/{run_id}/trace")
        self.assertEqual(trace.status_code, 200)
        body = trace.json()
        self.assertGreater(len(body["entries"]), 0)

        # M109：同一本账也**按层摊开**，页面的"分层记录"读的就是它。
        layers = body["layers"]
        self.assertIn("goal", layers)
        self.assertTrue(layers["actions"], "the layer view must show the Actions")
        self.assertEqual(layers["state"]["run_status"], "completed")

    def test_a_terminal_run_refuses_to_be_cancelled(self) -> None:
        """B-10 在 HTTP 上的样子：409，不是 200 加一句"好吧"。"""
        run_id = self._start()["run_id"]
        self.assertEqual(self._cancel(run_id).status_code, 200)
        again = self._cancel(run_id)
        self.assertEqual(again.status_code, 409, again.text)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TheRealProcessTest(RealPostgresCase):
    """**真进程**：`python -m apps.api` 起一个 uvicorn，走 TCP 打它。

    上面所有用例都还在**同一个进程里**（TestClient 直接调 ASGI）。
    那是真的框架绑定，但它不是真的进程：

        · 没有 uvicorn，没有 socket，没有端口
        · 没有 `apps/api/__main__.py` 那条装载路径
        · 没有"配置从环境变量来"那一次解析（PR-16）

    空洞 211 写的是"真进程冒烟"，所以得有一个用例真的把进程起起来。
    """

    def test_a_real_uvicorn_serves_the_console_and_the_api(self) -> None:
        port = _free_port()
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "AGENTOS_PG_DSN": dsn(),
            "AGENTOS_STACK_PROVIDER": STACK_PROVIDER,
            "AGENTOS_API_HOST": "127.0.0.1",
            "AGENTOS_API_PORT": str(port),
        }
        # 落在系统临时目录而不是仓库根：仓库里多一个文件，
        # 下一次 `git status` 就会有人来问它是什么（PR-19 的另一种用法）。
        log = Path(tempfile.gettempdir()) / f"agentos_http_smoke_{port}.log"
        # ⚠️ `unlink` 必须在 `with` **外面**：Windows 上父进程还握着这个句柄时
        # 删它会 `PermissionError`，而那时 finally 已经跑完了 ——
        # 一个与被测代码毫无关系的红灯（PR-19 反过来用：红灯要指向真凶）。
        with log.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                [sys.executable, "-m", "apps.api"],
                cwd=str(ROOT),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                self._wait_for_health(base, proc, log)

                # ⚠️ `trust_env=False`：httpx 默认**信任环境变量**，
                # 于是机器上只要设了 `HTTP_PROXY`（企业内网极其常见），
                # 这个用例就会去绕代理 —— 而代理发回来的请求行是绝对 URI，
                # 服务端按 RFC 认不出路径，一律 404。
                #
                # 那样红起来，红的跟被测代码**毫无关系**（PR-19 反过来用）。
                # 实测：`trust_env=True` → [200, 404, 404, 404]；
                #       `trust_env=False` → [200, 200, 200, 200]。
                # 打 127.0.0.1 本来就不该过任何代理。
                with httpx.Client(base_url=base, timeout=10.0, trust_env=False) as client:
                    console = client.get("/console")
                    self.assertEqual(console.status_code, 200)
                    self.assertIn("AgentOS 控制台", console.text)

                    chat = client.get("/")
                    self.assertEqual(chat.status_code, 200)
                    self.assertIn("AgentOS 对话", chat.text)

                    started = client.post(
                        "/agents/agent-it/runs",
                        json={"user_request": "compute 6*7"},
                        headers={"Idempotency-Key": "real-process-1"},
                    )
                    self.assertEqual(started.status_code, 201, started.text)
                    run_id = started.json()["run_id"]

                    cancelled = client.post(
                        f"/runs/{run_id}/cancel",
                        json=dict(BODY),
                        headers={"Idempotency-Key": "real-process-2"},
                    )
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    replayed = client.post(
                        f"/runs/{run_id}/cancel",
                        json=dict(BODY),
                        headers={"Idempotency-Key": "real-process-2"},
                    )
                    self.assertTrue(replayed.json()["replayed"])
            finally:
                # ⚠️ `terminate()` 只是**发信号**，它立刻返回、**不等进程退出**。
                #
                # 这个子进程连着真 PG。不等它退出就往下走，它会残留一个
                # 不确定的窗口（毫秒到数秒），而紧随其后的用例（或紧接的
                # 下一次运行）要 `DROP DATABASE` —— PG 在**有其它连接时
                # 会拒绝 DROP**。
                #
                # 于是红一条与被测代码毫无关系的灯，而且只在"跑完这个用例
                # 又紧接着跑别的"时才出现，单独跑往往撞不上（PR-19 反过来用：
                # 红灯要指向真凶）。
                #
                # 所以：发完信号必须等，等不来就 kill。
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:      # pragma: no cover
                    proc.kill()
                    proc.wait(timeout=5)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
        try:
            log.unlink(missing_ok=True)
        except PermissionError:  # pragma: no cover - Windows 上子进程句柄释放有延迟
            pass

    def _wait_for_health(self, base: str, proc: subprocess.Popen, log: Path) -> None:
        """等它起来；起不来就把**启动日志**甩出来（PR-19）。

        轮询而不是 `sleep 3`：慢机器上睡不够就假红，
        而假红比真红贵得多 —— 它会把人引向一个不存在的 bug。
        """
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"进程自己退出了（exit={proc.returncode}）：\n"
                    + log.read_text(encoding="utf-8")
                )
            try:
                if httpx.get(f"{base}/health", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.25)
        raise AssertionError(  # pragma: no cover
            f"60 秒没起来：\n{log.read_text(encoding='utf-8')}"
        )


class IamAuthTest(RealHttpCase):
    """M105 / IAM：认证器接上之后，写路由要凭据，actor 来自身份（I-1/I-2/I-3）。"""

    def _provider(self):
        from packages.agent_api.identity import (
            SCOPE_RUNS_WRITE,
            Identity,
            InMemoryIdentityProvider,
        )

        return InMemoryIdentityProvider(
            {
                "writer": Identity(
                    subject="alice", tenant_id="acme", scopes=frozenset({SCOPE_RUNS_WRITE})
                ),
                "reader": Identity(subject="bob", scopes=frozenset({"runs:read"})),
            }
        )

    def _authed_client(self):
        app = build_api(self.config, conn=self.api_conn, identity_provider=self._provider())
        return _client(app)

    def test_i1_a_write_without_a_token_is_401(self) -> None:
        client = self._authed_client()
        r = client.post("/agents/agent-it/runs", json={"user_request": "x"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["error"]["code"], "UNAUTHENTICATED")

    def test_i1_a_bad_token_is_401(self) -> None:
        client = self._authed_client()
        r = client.post(
            "/agents/agent-it/runs",
            json={"user_request": "x"},
            headers={"Authorization": "Bearer nope"},
        )
        self.assertEqual(r.status_code, 401)

    def test_i2_a_token_without_the_scope_is_403(self) -> None:
        client = self._authed_client()
        r = client.post(
            "/agents/agent-it/runs",
            json={"user_request": "x"},
            headers={"Authorization": "Bearer reader"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "FORBIDDEN")

    def test_a_valid_token_creates_a_run(self) -> None:
        client = self._authed_client()
        r = client.post(
            "/agents/agent-it/runs",
            json={"user_request": "compute 6*7"},
            headers={"Authorization": "Bearer writer"},
        )
        self.assertIn(r.status_code, (200, 201), r.text)

    def test_i3_the_actor_comes_from_the_identity_not_the_body(self) -> None:
        """body 里**没有** `by` —— 没有 I-3 的话取消会以 400（by is required）失败。"""
        client = self._authed_client()
        started = client.post(
            "/agents/agent-it/runs",
            json={"user_request": "compute 6*7"},
            headers={"Authorization": "Bearer writer"},
        )
        run_id = started.json()["run_id"]
        r = client.post(
            f"/runs/{run_id}/cancel",
            json={"reason": "stop"},
            headers={"Authorization": "Bearer writer"},
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
