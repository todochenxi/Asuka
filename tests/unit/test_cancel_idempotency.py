"""空洞 223：`POST /runs/{id}/cancel` 没有幂等键（M44 / §82）。

--------------------------------------------------------------------------
这个洞的形状：一次**成功**的取消，被重试报成了失败

    t0  用户在控制台点"停止"
    t1  POST /runs/{id}/cancel  → 200，Run 真的停了
    t2  网络超时，客户端重发同一次请求
    t3  → 409 RUN_TERMINAL

409 如实报了（那条 Run 确实终态了），问题在于**报的方式**：
它是一个错误码，于是 UI 只能显示"停止失败"。

更糟的是 409 分不出"是被你叫停的"还是"它自己跑完了" ——
客户端要么把一次成功的取消显示成失败，要么**猜**它是成功。
两者都不可接受：前者骗人，后者把判定权交给了猜。

A-3 说写操作要支持幂等键，要的正是这一件：
**重试拿到第一次的答案**，而不是拿到一个错误码去猜（D-35）。

--------------------------------------------------------------------------
为什么记录里存的是**答案**而不是指针（D-36）

`start_run` 存 `{"run_id": ...}`（指针），回放时重新装载，
装载不回来就喊 `RUN_NOT_RELOADABLE`。

`cancel_run` 反着来。因为取消的答案里最要紧的那一半 ——
"这次叫停被受理了没有"（`cancel_requested`）—— 恰恰**装载不回来**：
跨进程那条路上，这条进程手上既没有 stack 也没有快照，
它能给出的答案只有"我留了一句话"（`status="unknown"`）。

照抄 `start_run` 的写法，那一次成功的跨进程取消会在重试时
因为"装载不回来"被报成 409 —— 把一次已经发生的取消报成失败。

--------------------------------------------------------------------------
这里的世界是**真跑起来的**

`InProcessControlPlane` 是真的、`cancel_run` handler 是真的、
`InMemoryRunCancellationStore` 是真的。手搓一个"返回 200 的假取消"
会同时通过洞存在时和洞闭合后的两组断言 —— 那样的测试没有价值。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from packages.agent_api import start_run
from packages.agent_api.handlers import cancel_run
from packages.agent_api.idempotency import fingerprint
from packages.agent_runtime.cancellation import InMemoryRunCancellationStore

from .test_control_plane_api import ApiTestBase

ROOT = Path(__file__).resolve().parents[2]

BODY = {"reason": "user asked", "by": "alice"}


class CancelTestBase(ApiTestBase):
    """`ApiTestBase` 加上那条持久通道。

    没有 `cancellations`，跨进程那条路根本走不到（会退化成 404），
    于是"跨进程取消的回放"这类断言会**假绿**。
    """

    def setUp(self) -> None:
        super().setUp()
        self.cp.cancellations = InMemoryRunCancellationStore()

    def _cancel(self, run_id: str, *, key: str = "", **over: Any):
        body = dict(BODY)
        body.update(over)
        return cancel_run(self.cp, run_id, body, idempotency_key=key)

    def _send_it_to_another_process(self, run_id: str):
        """把这条 Run 从本进程的内存里摘掉 —— 模拟"它跑在别的进程里"。

        且 `snapshots` 是 None（没有可恢复点），于是 `_try_stack` 装载不回来，
        走的就是跨进程那条路（留意图，而不是 404）。
        """
        return self.cp.runs.pop(run_id)


# ---------------------------------------------------------------- 控制组
class TheHoleTest(CancelTestBase):
    """没有键的时候是什么样子 —— 钉住洞的两侧，免得"治好了"是错觉。"""

    def test_without_a_key_the_retry_gets_a_409(self) -> None:
        created = self._start()
        first = self._cancel(created["run_id"])
        second = self._cancel(created["run_id"])
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 409)
        self.assertEqual(second.body["error"]["code"], "RUN_TERMINAL")

    def test_the_409_is_what_the_client_sees_as_a_failure(self) -> None:
        """洞的代价不是"报错"，是"一次成功的取消被报成了失败"。

        这条断言在洞存在时成立、洞闭合后**仍然**成立（不吃键就走老路）——
        它钉的是"洞是什么"，不是"洞没了"。
        """
        created = self._start()
        self._cancel(created["run_id"])
        second = self._cancel(created["run_id"])
        self.assertNotIn("replayed", second.body)
        self.assertNotEqual(second.body.get("status"), "cancelled")

    def test_without_a_key_the_second_call_really_runs_the_whole_path(self) -> None:
        """第二轮**真的**走到了终态判据 —— 它没有被任何东西短路。"""
        created = self._start()
        self._cancel(created["run_id"])
        # 换个键（= 新的一次写请求）也一样：终态就是终态。
        resp = self._cancel(created["run_id"], key="k-other")
        self.assertEqual(resp.status, 409)


# ---------------------------------------------------------------- D-35
class D35TheRetryGetsTheFirstAnswerTest(CancelTestBase):
    """D-35：同一个键第二次来 ⟹ 第一次的答案，不重新判一次终态。"""

    def test_the_retry_gets_the_first_answer(self) -> None:
        created = self._start()
        first = self._cancel(created["run_id"], key="k1")
        second = self._cancel(created["run_id"], key="k1")
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual(second.status, 200, second.body)
        self.assertTrue(second.body["replayed"])
        self.assertEqual(second.body["status"], "cancelled")
        self.assertEqual(second.body["last_outcome"], "cancelled")

    def test_the_replayed_answer_is_the_same_one_field_by_field(self) -> None:
        """回放的是**那一份**答案，不是"一份长得像的"。"""
        created = self._start()
        first = self._cancel(created["run_id"], key="k1")
        second = self._cancel(created["run_id"], key="k1")
        for field in ("run_id", "agent_id", "status", "last_outcome", "cancel_requested"):
            self.assertEqual(first.body[field], second.body[field], field)

    def test_a_different_key_is_a_different_write_and_still_refuses(self) -> None:
        """键没有掩盖终态判据 —— 换一个键是一次新的写请求，该 409 还是 409。"""
        created = self._start()
        self._cancel(created["run_id"], key="k1")
        resp = self._cancel(created["run_id"], key="k2")
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.body["error"]["code"], "RUN_TERMINAL")

    def test_no_key_means_no_idempotency(self) -> None:
        """A-3 的对面：不吃键就没有幂等，行为与 M44 之前完全一致。"""
        created = self._start()
        self._cancel(created["run_id"])
        self.assertEqual(self._cancel(created["run_id"]).status, 409)

    def test_the_cross_process_answer_is_replayed_too(self) -> None:
        """跨进程那条路最容易踩到：它的答案本来就"装载不回来"。"""
        created = self._start()
        self._send_it_to_another_process(created["run_id"])
        first = self._cancel(created["run_id"], key="k1")
        second = self._cancel(created["run_id"], key="k1")
        self.assertEqual(first.status, 200)
        self.assertTrue(first.body["cancel_requested"])
        self.assertEqual(first.body["status"], "unknown")
        self.assertEqual(second.status, 200, second.body)
        self.assertTrue(second.body["replayed"])
        self.assertTrue(second.body["cancel_requested"])
        self.assertEqual(second.body["status"], "unknown")

    def test_a_failed_cancel_does_not_claim_the_key(self) -> None:
        """失败不占键：一次瞬时故障不该把这个键永久作废。"""
        created = self._start()
        bad = self._cancel(created["run_id"], key="k1", reason="")
        self.assertEqual(bad.status, 400, bad.body)
        good = self._cancel(created["run_id"], key="k1")
        self.assertEqual(good.status, 200, good.body)
        self.assertFalse(good.body["replayed"], "第一次成功的取消不该被标成回放")

    def test_a_404_does_not_claim_the_key_either(self) -> None:
        """对一条不存在的 Run 取消失败（无通道 → 404）之后，同一个键还能用。"""
        self.cp.cancellations = None          # 没有通道：装载不了只能如实 404
        missing = self._cancel("run_nope", key="k1")
        self.assertEqual(missing.status, 404)

        created = self._start()
        self.cp.cancellations = InMemoryRunCancellationStore()
        good = self._cancel(created["run_id"], key="k1")
        self.assertEqual(good.status, 200, good.body)
        self.assertEqual(good.body["status"], "cancelled")


# ---------------------------------------------------------------- D-36
class D36TheRecordHoldsTheAnswerTest(CancelTestBase):
    """D-36：记录里存的是答案，不是一个指向活对象的指针。"""

    def test_the_answer_is_replayed_not_re_derived(self) -> None:
        """回放给的是**当时**那份答案，不是"现在能查到的"那份。

        重新推导会让同一个键在两个时刻给出两个答案 —— 那就不叫幂等了。
        "这条 Run 现在怎么样了"是 `GET /runs/{id}` 的职责。
        """
        created = self._start()
        stack = self._send_it_to_another_process(created["run_id"])
        first = self._cancel(created["run_id"], key="k1")
        self.assertEqual(first.body["status"], "unknown")

        # 它回来了：本进程又能装载它了。
        self.cp.runs[created["run_id"]] = stack
        second = self._cancel(created["run_id"], key="k1")
        self.assertEqual(
            second.body["status"], "unknown",
            "回放不许重新推导 —— 同一个键必须给出同一个答案",
        )

    def test_without_the_key_the_same_call_would_have_cancelled_it(self) -> None:
        """上一条"unknown"之所以有意义，是因为没有键时它**真的**会停掉。

        少了这一条，上一条可能只是因为别的原因（比如压根没走到）而绿。
        """
        created = self._start()
        stack = self._send_it_to_another_process(created["run_id"])
        self._cancel(created["run_id"], key="k1")
        self.cp.runs[created["run_id"]] = stack
        fresh = self._cancel(created["run_id"])          # 没有键 = 新的一次写
        self.assertEqual(fresh.body["status"], "cancelled")

    def test_cancel_does_not_shout_when_it_cannot_reload(self) -> None:
        """跨进程 + 不可装载 ⟹ 照样给答案，不喊 `RUN_NOT_RELOADABLE`。

        喊出来就是把一次已经发生的取消报成失败 —— 正是这一轮要治的病。
        """
        created = self._start()
        self._send_it_to_another_process(created["run_id"])
        self._cancel(created["run_id"], key="k1")
        again = self._cancel(created["run_id"], key="k1")
        self.assertEqual(again.status, 200, again.body)
        self.assertNotIn("error", again.body)

    def test_start_run_by_contrast_shouts_when_it_cannot_reload(self) -> None:
        """对照组：同一个"装载不回来"，`start_run` **必须**喊。

        两个方向相反不是矛盾，是它们保护的东西不同：
        start 的键保护"不要开第二条 Run"（装载不回来 ⟹ 宁可喊）；
        cancel 的键保护"不要把已经发生的取消报成失败"（装载不回来 ⟹ 照样给）。
        """
        first = start_run(
            self.cp, {"agent_id": "agent-api", "user_request": "x"}, idempotency_key="k1"
        )
        self.assertEqual(first.status, 201)
        self.cp.runs.clear()                              # 内存没了，也没有快照

        second = start_run(
            self.cp, {"agent_id": "agent-api", "user_request": "x"}, idempotency_key="k1"
        )
        self.assertEqual(second.status, 409)
        self.assertEqual(second.body["error"]["code"], "RUN_NOT_RELOADABLE")


# ---------------------------------------------------------------- 命名空间
class TheNamespaceSeparatesTheTwoWritesTest(CancelTestBase):
    """`run:` 与 `cancel:` 必须分开 —— 共用的后果是**静默漏掉一次叫停**。"""

    def test_a_key_used_for_start_then_cancel_still_cancels(self) -> None:
        created = self._start(_idem="k1")
        resp = self._cancel(created["run_id"], key="k1")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertFalse(resp.body["replayed"], "cancel 不该命中 start 留下的记录")
        self.assertEqual(resp.body["status"], "cancelled")

    def test_a_key_used_for_cancel_then_start_still_starts(self) -> None:
        created = self._start()
        self._cancel(created["run_id"], key="k1")
        resp = start_run(
            self.cp, {"agent_id": "agent-api", "user_request": "x"}, idempotency_key="k1"
        )
        self.assertEqual(resp.status, 201, resp.body)
        self.assertFalse(resp.body["replayed"])


# ---------------------------------------------------------------- 指纹
class TheFingerprintIsStableTest(unittest.TestCase):
    """指纹必须**跨进程、跨重启稳定** —— 否则重启后合法的重试会被判成复用。"""

    def test_the_same_body_gives_the_same_fingerprint(self) -> None:
        self.assertEqual(
            fingerprint({"run_id": "r1", "reason": "x", "by": "alice"}),
            fingerprint({"run_id": "r1", "reason": "x", "by": "alice"}),
        )

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        """dict 的插入顺序不参与"这两次请求一样不一样"的判断。"""
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))

    def test_it_survives_a_different_hash_seed(self) -> None:
        """⚠️ 这一条是 `hash()` 那个陷阱的守卫。

        Python 对 str 的 `hash()` 带随机盐（`PYTHONHASHSEED`），
        重启之后同一个字符串的 hash **不一样**。用它做指纹，产物是
        一个只在重启后才出现的假 422 —— 单测跑在同一个进程里，全绿。

        所以这里开**两个**哈希种子不同的子进程，比的是它们的指纹。
        换成 `hash()` 的那一刻，它们就不相等了。
        """

        def _fp(seed: str) -> str:
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from packages.agent_api.idempotency import fingerprint;"
                 "print(fingerprint({'run_id':'r1','reason':'x','by':'alice'}))"],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        self.assertEqual(_fp("0"), _fp("1"))
        self.assertEqual(_fp("0"), fingerprint({"run_id": "r1", "reason": "x", "by": "alice"}))


# ---------------------------------------------------------------- 键复用
class TheKeyReuseIsRefusedTest(CancelTestBase):
    """同一个键 + 不同的请求体 ⟹ 点名拒绝，不是静默回放。"""

    def test_a_different_reason_on_the_same_key_is_422(self) -> None:
        created = self._start()
        self._cancel(created["run_id"], key="k1")
        resp = self._cancel(created["run_id"], key="k1", reason="a different reason")
        self.assertEqual(resp.status, 422, resp.body)
        self.assertEqual(resp.body["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_a_different_run_on_the_same_key_is_422(self) -> None:
        first = self._start()
        second = self._start()
        self._cancel(first["run_id"], key="k1")
        resp = self._cancel(second["run_id"], key="k1")
        self.assertEqual(resp.status, 422, resp.body)

    def test_the_same_body_on_the_same_key_is_not_a_reuse(self) -> None:
        created = self._start()
        self._cancel(created["run_id"], key="k1")
        resp = self._cancel(created["run_id"], key="k1")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertTrue(resp.body["replayed"])

    def test_start_run_refuses_a_reused_key_too(self) -> None:
        """M44 顺手补的：`start_run` 原本缺这一条（同一份规矩不许两个实现）。"""
        start_run(
            self.cp, {"agent_id": "agent-api", "user_request": "x"}, idempotency_key="k1"
        )
        resp = start_run(
            self.cp, {"agent_id": "agent-api", "user_request": "y"}, idempotency_key="k1"
        )
        self.assertEqual(resp.status, 422, resp.body)
        self.assertEqual(resp.body["error"]["code"], "IDEMPOTENCY_KEY_REUSED")


# --------------------------------------------------------------------------
# 路由层那一节（"路由有没有读 Idempotency-Key"）M45 已经从本文件**删掉**了。
#
# 它原先是一段 AST 源码扫描，存在的唯一理由是当时没装 fastapi。
# 现在它有了真的替代品：`tests/integration/test_api_real_http.py`
# —— 真 FastAPI、真 TestClient、真 PG，断言的是"带上这个头重试拿到 200
# replayed=True，不带就 409"。同一件事不许有两个定义，
# 而两个里面能挡住"传错了"的只有后者。
# --------------------------------------------------------------------------


# ---------------------------------------------------------------- 线上形状
class TheWireShapeSurvivesTheRoundTripTest(unittest.TestCase):
    """`RunView.from_dict(to_dict())` 必须是恒等 —— 不然回放的答案会丢字段。"""

    def test_from_dict_inverts_to_dict(self) -> None:
        from datetime import datetime, timezone

        from packages.agent_api.dto import ApprovalView, RunView

        view = RunView(
            run_id="r1",
            agent_id="a",
            status="cancelled",
            step_count=3,
            pending_approval=ApprovalView(
                approval_id="ap1",
                run_id="r1",
                status="pending",
                question="ok?",
                requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            last_outcome="cancelled",
            waiting_for=None,
            cancel_requested=True,
        )
        back = RunView.from_dict(view.to_dict())
        self.assertEqual(back.to_dict(), view.to_dict())
        assert back.pending_approval is not None
        self.assertEqual(back.pending_approval.requested_at, view.pending_approval.requested_at)

    def test_an_unreadable_record_is_refused_not_guessed(self) -> None:
        """信封里没有答案（别的版本写的）⟹ 拒绝，不许"当没执行过"再跑一遍。"""
        from packages.agent_api.errors import Unprocessable
        from packages.agent_api.idempotency import answer_of

        with self.assertRaises(Unprocessable):
            answer_of({"v": 1, "fingerprint": "x"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
