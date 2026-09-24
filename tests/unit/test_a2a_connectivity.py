"""M104 · A2A 客户端 + 远端委派适配（M4 下半）。

补的空洞：M4 的 Multi-Agent 只限**同进程**（`InProcessChildRunSpawner`），
`AgentDelegationExecutor` 的 docstring 一直写着 A2A，全仓却没有一行真的说过它。
这里把 AgentOS 的 `ChildRunSpawner` 协议接到远端 Agent 上，并钉住三条不变量：

    A-1  只有终态才写终态（working / submitted 不许被记成 completed/failed）
    A-2  input-required / auth-required 既非终态亦非失败
    A-3  A2A 状态是闭集，未知字符串拒绝

外加一条实证出来的坑：A2A 的美式 `canceled` 必须归一化成 AgentOS 的
`cancelled`，否则一次远端取消会被唤醒路径读成 `failed`。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.execution.execution import SuspensionReason
from packages.agent_domain.intelligence.action import Action, ActionType
from packages.agent_runtime.connectivity import (
    A2AClient,
    A2AChildRunSpawner,
    A2ATaskState,
    AgentCard,
    InMemoryCardTransport,
    InMemoryTransport,
    JsonRpcClient,
    parse_state,
)
from packages.agent_runtime.delegation import (
    ChildRunKind,
    ChildRunRegistry,
    ChildRunRequest,
    ChildRunUnavailable,
)
from packages.agent_runtime.loop import StepOutcome

from .test_child_run import ChildRunTestBase


def _action() -> Action:
    return Action(
        run_id="run_1",
        action_type=ActionType.AGENT_DELEGATION,
        payload={"agent_id": "researcher"},
    )


def _request(**over) -> ChildRunRequest:
    base = dict(
        parent_run_id="run_1",
        parent_execution_id="exec_1",
        kind=ChildRunKind.AGENT,
        target="researcher",
        action=_action(),
        parent_task_id="task_1",
        instruction="go find out",
    )
    base.update(over)
    return ChildRunRequest(**base)


def _client(responses) -> A2AClient:
    return A2AClient(JsonRpcClient(InMemoryTransport(responses)))


def _wrap(transport: InMemoryTransport) -> A2AClient:
    """包一个**已存在**的 transport —— 需要断言 `sent` 时用它。"""
    return A2AClient(JsonRpcClient(transport))


def _task(task_id: str, state: str, **over):
    task = {"id": task_id, "contextId": "run_1", "status": {"state": state}}
    task.update(over)
    return task


class AgentCardTest(unittest.TestCase):
    def test_a_card_is_parsed(self) -> None:
        card = AgentCard.from_mapping(
            {"name": "researcher", "url": "http://r", "version": "1.2", "skills": [{"id": "s"}]}
        )
        self.assertEqual(card.name, "researcher")
        self.assertEqual(card.version, "1.2")
        self.assertEqual(len(card.skills), 1)

    def test_a_card_without_a_name_is_refused(self) -> None:
        with self.assertRaises(InvariantViolation):
            AgentCard.from_mapping({"url": "http://r"})

    def test_the_client_fetches_the_card_over_the_card_transport(self) -> None:
        client = A2AClient(
            JsonRpcClient(InMemoryTransport({})),
            base_url="http://r",
            card_transport=InMemoryCardTransport(
                {"http://r/.well-known/agent-card.json": {"name": "researcher"}}
            ),
        )
        self.assertEqual(client.fetch_agent_card().name, "researcher")


class StateTest(unittest.TestCase):
    def test_the_state_is_a_closed_set(self) -> None:
        self.assertIs(parse_state("working"), A2ATaskState.WORKING)
        with self.assertRaises(InvariantViolation) as ctx:
            parse_state("frobnicating")
        self.assertIn("A-3", str(ctx.exception))


class ClientTest(unittest.TestCase):
    def test_send_message_builds_an_a2a_message(self) -> None:
        transport = InMemoryTransport({"message/send": _task("t1", "working")})
        _wrap(transport).send_message("hello", context_id="run_1", data={"k": 1})
        message = transport.sent[0]["params"]["message"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["contextId"], "run_1")
        self.assertEqual(message["parts"][0], {"kind": "text", "text": "hello"})
        self.assertEqual(message["parts"][1], {"kind": "data", "data": {"k": 1}})
        self.assertIn("messageId", message)

    def test_get_and_cancel_use_the_task_id(self) -> None:
        transport = InMemoryTransport(
            {"tasks/get": _task("t1", "working"), "tasks/cancel": _task("t1", "canceled")}
        )
        client = _wrap(transport)
        client.get_task("t1")
        client.cancel_task("t1")
        self.assertEqual(transport.sent[0]["params"], {"id": "t1"})
        self.assertEqual(transport.sent[1]["params"], {"id": "t1"})


class SpawnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ChildRunRegistry()

    def _spawner(self, responses, **kw) -> A2AChildRunSpawner:
        return A2AChildRunSpawner(
            clients={"researcher": _client(responses)},
            registry=self.registry,
            **kw,
        )

    def test_spawn_binds_the_remote_task_id(self) -> None:
        transport = InMemoryTransport({"message/send": _task("t1", "working")})
        spawner = A2AChildRunSpawner(
            clients={"researcher": _wrap(transport)}, registry=self.registry
        )
        handle = spawner.spawn(_request())
        self.assertEqual(handle.child_run_id, "t1")
        self.assertFalse(handle.is_finished)
        # contextId 让远端能把这次委派关联回父 Run
        self.assertEqual(transport.sent[0]["params"]["message"]["contextId"], "run_1")

    def test_d1_a_retry_does_not_open_a_second_remote_task(self) -> None:
        transport = InMemoryTransport({"message/send": _task("t1", "working")})
        spawner = A2AChildRunSpawner(
            clients={"researcher": _wrap(transport)}, registry=self.registry
        )
        first = spawner.spawn(_request())
        second = spawner.spawn(_request())
        self.assertEqual(first.child_run_id, second.child_run_id)
        self.assertEqual(len(transport.sent), 1, "D-1：重试不许再开一条远端任务")

    def test_a_synchronously_finished_task_is_recorded_but_not_returned_finished(self) -> None:
        """同步完成的远端：结果要落登记处，但**返回未终态的 handle** ——
        否则 Loop 会拿着它撞 D-11（挂起去等一个不会再变的答案）。"""
        spawner = self._spawner(
            {"message/send": _task("t1", "completed", artifacts=[{"parts": [{"kind": "text", "text": "42"}]}])}
        )
        handle = spawner.spawn(_request())
        self.assertFalse(handle.is_finished)
        stored = self.registry.for_child("t1")
        assert stored is not None
        self.assertTrue(stored.is_finished)
        self.assertEqual(stored.status, "completed")
        self.assertIn("42", stored.result["text"])

    def test_poll_records_a_terminal_state(self) -> None:
        spawner = self._spawner(
            {
                "message/send": _task("t1", "working"),
                "tasks/get": _task("t1", "completed", artifacts=[{"parts": [{"kind": "text", "text": "done"}]}]),
            }
        )
        spawner.spawn(_request())
        handle = spawner.poll("t1")
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "completed")
        self.assertEqual(handle.result["state"], "completed")

    def test_a1_a_working_poll_does_not_finish_the_child(self) -> None:
        spawner = self._spawner({"message/send": _task("t1", "working"), "tasks/get": _task("t1", "working")})
        spawner.spawn(_request())
        handle = spawner.poll("t1")
        self.assertFalse(handle.is_finished)

    def test_a2_input_required_is_neither_terminal_nor_failure(self) -> None:
        spawner = self._spawner(
            {"message/send": _task("t1", "working"), "tasks/get": _task("t1", "input-required")}
        )
        spawner.spawn(_request())
        handle = spawner.poll("t1")
        self.assertFalse(handle.is_finished)
        self.assertNotEqual(handle.status, "failed")

    def test_a3_an_unknown_state_is_refused_not_treated_as_working(self) -> None:
        spawner = self._spawner(
            {"message/send": _task("t1", "working"), "tasks/get": _task("t1", "frobnicating")}
        )
        spawner.spawn(_request())
        with self.assertRaises(InvariantViolation):
            spawner.poll("t1")

    def test_remote_cancel_normalizes_to_agentos_cancelled(self) -> None:
        """美式 `canceled` → 英式 `cancelled`，否则取消被读成失败（D-10）。"""
        spawner = self._spawner(
            {"message/send": _task("t1", "working"), "tasks/cancel": _task("t1", "canceled")}
        )
        spawner.spawn(_request())
        handle = spawner.cancel_child("t1", reason="user asked")
        self.assertTrue(handle.is_finished)
        self.assertEqual(handle.status, "cancelled")

    def test_a_rejected_task_is_a_failure(self) -> None:
        spawner = self._spawner(
            {"message/send": _task("t1", "working"), "tasks/get": _task("t1", "rejected")}
        )
        spawner.spawn(_request())
        handle = spawner.poll("t1")
        self.assertEqual(handle.status, "failed")
        self.assertEqual(handle.result["state"], "rejected")

    def test_an_unknown_target_says_so(self) -> None:
        spawner = self._spawner({"message/send": _task("t1", "working")})
        with self.assertRaises(ChildRunUnavailable):
            spawner.spawn(_request(target="ghost"))

    def test_polling_an_unregistered_child_is_refused(self) -> None:
        spawner = self._spawner({})
        with self.assertRaises(InvariantViolation):
            spawner.poll("nobody")


class LoopRemoteDelegationTest(ChildRunTestBase):
    """端到端：委托给远端 Agent → 挂起 → 轮询到终态 → 唤醒父 Run。"""

    def test_a_remote_agent_result_wakes_the_parent(self) -> None:
        spawner = A2AChildRunSpawner(
            clients={
                "researcher": _client(
                    {
                        "message/send": _task("remote_1", "working"),
                        "tasks/get": _task(
                            "remote_1",
                            "completed",
                            artifacts=[{"parts": [{"kind": "text", "text": "42"}]}],
                        ),
                    }
                )
            }
        )
        loop = self._loop(self._delegation(), spawner=spawner)
        loop.start("delegate it")

        outcome = loop.step()
        self.assertIs(outcome, StepOutcome.WAITING_CHILD)
        assert loop.pending_child is not None
        self.assertEqual(loop.pending_child.child_run_id, "remote_1")
        self.assertEqual(
            self._suspended()[0].suspension.reason, SuspensionReason.CHILD_AGENT
        )

        handle = spawner.poll("remote_1")
        self.assertTrue(handle.is_finished)

        step = loop.child_completed("remote_1", handle.result)
        self.assertIs(step, StepOutcome.EXECUTED)
        self.assertIsNone(loop.pending_child)


if __name__ == "__main__":
    unittest.main()
