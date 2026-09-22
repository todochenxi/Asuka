"""M65 / 空洞 234：给 Execution 的取消归因一个**出口**。

--------------------------------------------------------------------------
为什么需要这一层

M48 给 `executions` 加了 `cancellation_reason` / `cancellation_by`，
**并且真的写入了**（真库上验过）。但没有任何端点或命令读它们 ——
那两列是死数据。

按"交付的判据是被用上，不是被写出来"（M64 冻过这条，本项目栽过三次），
光写进库不算落地。这里补上出口。

--------------------------------------------------------------------------
两条边界

1. **没配查询器返回空列表，不是 404** —— "这条 Run 手上有哪些 Execution"
   是查询语义，空集不是错误（与 `list_approvals` 同一条判据）。
2. 查询器由**组合根**注入，service 层不 import 任何数据库客户端。
"""
from __future__ import annotations

import unittest

from packages.agent_api.handlers import list_run_executions


class _Plane:
    def __init__(self, executions=None):
        self.executions = executions


class TestAttributionHasAnExit(unittest.TestCase):
    def test_it_returns_what_the_lookup_found(self):
        """主断言：写进库的归因，端点读得出来。"""
        rows = [
            {
                "execution_id": "exec_1",
                "status": "CANCELLED",
                "cancellation_requested": True,
                "cancellation_reason": "who stopped me",
                "cancellation_by": "alice",
            }
        ]
        resp = list_run_executions(_Plane(executions=lambda run_id: rows), "run_1")
        self.assertEqual(resp.status, 200)
        items = resp.body["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cancellation_reason"], "who stopped me")
        self.assertEqual(items[0]["cancellation_by"], "alice")
        self.assertTrue(items[0]["cancellation_requested"])

    def test_run_id_is_echoed_so_the_caller_can_check_it(self):
        resp = list_run_executions(_Plane(executions=lambda r: []), "run_xyz")
        self.assertEqual(resp.body["run_id"], "run_xyz")

    def test_it_passes_the_run_id_through_to_the_lookup(self):
        """别把 run_id 弄丢 —— 那会返回别的 Run 的归因。"""
        seen = []

        def lookup(run_id):
            seen.append(run_id)
            return []

        list_run_executions(_Plane(executions=lookup), "run_42")
        self.assertEqual(seen, ["run_42"])


class TestNoLookupConfigured(unittest.TestCase):
    """没配查询器：空列表，不是 404。"""

    def test_empty_result_not_found(self):
        resp = list_run_executions(_Plane(executions=None), "run_1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["items"], [])

    def test_a_plane_without_the_attribute_is_also_empty(self):
        """`executions` 字段不存在时也不该炸（老的组合根没注入这个字段）。"""

        class Bare:
            pass

        resp = list_run_executions(Bare(), "run_1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["items"], [])


if __name__ == "__main__":
    unittest.main()
