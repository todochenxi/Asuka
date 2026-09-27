"""M115 · `GET /tools` —— 本进程能调的工具（连接视图的数据源）。

⚠️ 同 `GET /runs`：这是**这一进程**的工具表，从它在手的某个 stack 的
`tool_runtime` 读，不是全局清单。没装载过 Run 时 `source="none"` +
空 items —— 如实说"没读到"，不假装"一个工具都没有"。
"""
from __future__ import annotations

import unittest

from examples.demo_stack import build_stack_factory
from packages.agent_api.dto import StartRunRequest
from packages.agent_api.handlers import list_tools
from packages.agent_api.service import InProcessControlPlane
from packages.agent_harness.approval import InMemoryApprovalStore


class ToolsEndpointTest(unittest.TestCase):
    def _cp(self) -> InProcessControlPlane:
        return InProcessControlPlane(
            factory=build_stack_factory(), approvals=InMemoryApprovalStore()
        )

    def test_no_run_loaded_says_none_not_empty(self) -> None:
        resp = list_tools(self._cp())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["source"], "none")
        self.assertEqual(resp.body["items"], [])

    def test_a_loaded_run_reveals_its_tools(self) -> None:
        cp = self._cp()
        cp.start_run(StartRunRequest(agent_id="agent-api", user_request="compute 6*7"))
        resp = list_tools(cp)
        self.assertEqual(resp.body["source"], "tool_runtime")
        names = {t["name"] for t in resp.body["items"]}
        self.assertIn("echo", names)
        for tool in resp.body["items"]:
            self.assertIn("protocol", tool)
            self.assertIn("side_effect", tool)

    def test_side_effect_is_reported(self) -> None:
        cp = self._cp()
        cp.start_run(StartRunRequest(agent_id="agent-api", user_request="x"))
        by_name = {t["name"]: t for t in list_tools(cp).body["items"]}
        self.assertEqual(by_name["echo"]["side_effect"], "read")
        self.assertEqual(by_name["note.write"]["side_effect"], "write")


if __name__ == "__main__":
    unittest.main()
