"""M110 · `GET /runs` —— 本进程装载过的 Run。

补的空洞：控制台的"追踪的 Run"是**浏览器本地**列表，聊天页把会话存另一个 key，
于是"在聊天页问过一句"在控制台里**一个入口都没有** —— 数据在后台，只是没人列。
`list_runs` 把后台这一侧能列的列出来（查询语义：空集不是错误）。
"""
from __future__ import annotations

import unittest

from examples.demo_stack import build_stack_factory
from packages.agent_api.dto import StartRunRequest
from packages.agent_api.handlers import list_runs
from packages.agent_api.service import InProcessControlPlane
from packages.agent_harness.approval import InMemoryApprovalStore


class ListRunsTest(unittest.TestCase):
    def _cp(self) -> InProcessControlPlane:
        return InProcessControlPlane(
            factory=build_stack_factory(), approvals=InMemoryApprovalStore()
        )

    def test_empty_is_not_an_error(self) -> None:
        self.assertEqual(list(self._cp().list_runs()), [])

    def test_it_lists_a_started_run(self) -> None:
        cp = self._cp()
        view = cp.start_run(
            StartRunRequest(agent_id="agent-api", user_request="compute 6*7")
        )
        runs = list(cp.list_runs())
        self.assertEqual([r.run_id for r in runs], [view.run_id])
        self.assertEqual(runs[0].agent_id, "agent-api")

    def test_it_lists_more_than_one_run(self) -> None:
        cp = self._cp()
        first = cp.start_run(StartRunRequest(agent_id="agent-api", user_request="a"))
        second = cp.start_run(StartRunRequest(agent_id="agent-api", user_request="b"))
        ids = {r.run_id for r in cp.list_runs()}
        self.assertEqual(ids, {first.run_id, second.run_id})

    def test_the_handler_wraps_items(self) -> None:
        cp = self._cp()
        cp.start_run(StartRunRequest(agent_id="agent-api", user_request="compute 6*7"))
        resp = list_runs(cp)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body["items"]), 1)
        self.assertIn("run_id", resp.body["items"][0])


if __name__ == "__main__":
    unittest.main()
