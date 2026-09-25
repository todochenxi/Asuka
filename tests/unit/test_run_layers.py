"""M109 · 账本按层摊开（`TraceView.layers`）。

账本是"只增的一串"（事实源），但排障时人问的是"这一层发生了什么"。
`layers_of` 把同一本账再摊成 goal / plan / actions / executions / harness / state，
本文件钉住它**真的摊得出东西**，且缺的层不补空壳。
"""
from __future__ import annotations

import unittest

from examples.demo_stack import build_stack_factory
from packages.agent_api.dto import StartRunRequest
from packages.agent_api.service import InProcessControlPlane, layers_of
from packages.agent_harness.approval import InMemoryApprovalStore


class RunLayersTest(unittest.TestCase):
    def _cp(self) -> InProcessControlPlane:
        return InProcessControlPlane(
            factory=build_stack_factory(), approvals=InMemoryApprovalStore()
        )

    def _finished_trace(self):
        cp = self._cp()
        view = cp.start_run(
            StartRunRequest(agent_id="agent-api", user_request="compute 6*7")
        )
        cp.drive_run(view.run_id)
        return cp.get_trace(view.run_id)

    def test_the_trace_is_still_a_flat_ledger(self) -> None:
        trace = self._finished_trace()
        self.assertTrue(trace.entries, "the flat ledger must not disappear")
        kinds = [e["kind"] for e in trace.entries]
        self.assertIn("task.submitted", kinds)
        self.assertIn("run.finished", kinds)

    def test_layers_are_exposed_alongside_the_ledger(self) -> None:
        payload = self._finished_trace().to_dict()
        layers = payload["layers"]
        self.assertEqual(layers["goal"]["objective"], "compute 6*7")
        self.assertTrue(layers["goal"]["success_criteria"])
        self.assertTrue(layers["plan"]["nodes"])
        self.assertTrue(layers["actions"], "at least one Action must be visible")
        self.assertTrue(layers["executions"], "at least one Execution must be visible")
        self.assertEqual(layers["state"]["run_status"], "completed")

    def test_the_observation_layer_lists_seen_facts(self) -> None:
        """M113：Observation 层回答"Agent 感知到了什么"（只留摘要，不塞 content）。"""
        layers = self._finished_trace().to_dict()["layers"]
        obs = layers["observations"]
        self.assertTrue(obs, "at least one Observation must be visible")
        kinds = [o["kind"] for o in obs]
        self.assertIn("execution_result", kinds)
        self.assertIn("content_keys", obs[0])

    def test_the_harness_layer_lists_context_builds(self) -> None:
        layers = self._finished_trace().to_dict()["layers"]
        # LLM 那一步之前会 context.built
        self.assertTrue(layers["harness"]["context"])
        self.assertIsInstance(layers["harness"]["approval_seqs"], list)

    def test_an_action_carries_the_layer_chain_ids(self) -> None:
        actions = self._finished_trace().to_dict()["layers"]["actions"]
        first = actions[0]
        self.assertTrue(first["task_id"].startswith("task_"))
        self.assertTrue(first["execution_id"].startswith("exec_"))
        self.assertTrue(first["step_id"].startswith("step_"))
        self.assertTrue(first["action_type"])


class LayersOfTest(unittest.TestCase):
    def test_a_missing_goal_is_absent_not_an_empty_shell(self) -> None:
        """缺的层就是缺 —— 不补一个"看起来有、其实空"的壳。"""

        class _NoState:
            state = None
            steps_of_run: tuple = ()
            agent_run = None

        class _Stack:
            loop = _NoState()

        layers = layers_of(_Stack(), [])
        self.assertNotIn("goal", layers)
        self.assertNotIn("plan", layers)
        self.assertIn("actions", layers)          # 这三段是"账本里没有就是空列表"
        self.assertEqual(layers["actions"], [])


if __name__ == "__main__":
    unittest.main()
