"""M93：聊天页的后端 —— `POST /chat` 与 `GET /runs/{id}/answer`。

聊天页要的是"问一句、拿一句回答"，而回答**不在** `RunView`、也不在 trace 里
（trace 的 `execution.observed` 只有 `status` / `outcome`）。它存在 State 的
Observation 的 `content["result"]["response"]["text"]`。

这一层验三件事：

    1. `chat` 真的开 Run、推到停下来、把回答带出来；
    2. 停在治理闸门上的 Run **没有**回答 —— 状态说真话，回答留空；
    3. 聊天 agent 被排除在闸门之外（否则每问一句都要先去点一次批准）。
"""
from __future__ import annotations

import unittest

from examples.demo_stack import (
    CHAT_AGENT_ID,
    build_approval_demo_stack_factory,
    build_stack_factory,
)
from packages.agent_api.dto import RunView
from packages.agent_api.handlers import chat, get_answer, _conversation_prompt
from packages.agent_api.service import InProcessControlPlane, last_llm_answer
from packages.agent_harness.approval import InMemoryApprovalStore


def _control_plane(factory) -> InProcessControlPlane:
    return InProcessControlPlane(factory=factory, approvals=InMemoryApprovalStore())


class _Obs:
    def __init__(self, content):
        self.content = content


class _State:
    def __init__(self, observations):
        self.observations = observations


class ChatHandlerTest(unittest.TestCase):
    def test_the_chat_handler_answers(self) -> None:
        """开一条 Run、推到完成，回答与状态都带出来。"""
        cp = _control_plane(build_stack_factory())
        resp = chat(cp, {"agent_id": CHAT_AGENT_ID, "message": "compute 6*7"})

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["status"], "completed")
        self.assertTrue(resp.body["answer"])
        self.assertIn("demo-1 received", resp.body["answer"])

    def test_the_approval_demo_stack_lets_the_chat_agent_through(self) -> None:
        """带审批的栈里，`agent-chat` 也**不**挂闸门 —— 否则聊天没法用。"""
        cp = _control_plane(build_approval_demo_stack_factory())
        resp = chat(cp, {"agent_id": CHAT_AGENT_ID, "message": "hello"})

        self.assertEqual(resp.body["status"], "completed")
        self.assertTrue(resp.body["answer"])

    def test_a_run_stopped_at_the_gate_reports_suspended(self) -> None:
        """停在闸门上的 Run：状态是 `suspended` 且带出待审批 —— 状态说真话。"""
        cp = _control_plane(build_stack_factory(approval_at_step=2))
        resp = chat(cp, {"agent_id": "agent-it", "message": "do something risky"})

        self.assertEqual(resp.body["status"], "suspended")
        self.assertIsNotNone(resp.body["pending_approval"])
        self.assertIsInstance(resp.body["answer"], str)

    def test_a_missing_message_is_a_400(self) -> None:
        cp = _control_plane(build_stack_factory())
        resp = chat(cp, {"agent_id": CHAT_AGENT_ID})
        self.assertEqual(resp.status, 400)

    def test_the_answer_endpoint_replays_the_same_text(self) -> None:
        """`GET /runs/{id}/answer` 与 `chat` 带回来的回答**逐字相同**。"""
        cp = _control_plane(build_stack_factory())
        started = chat(cp, {"agent_id": CHAT_AGENT_ID, "message": "hi"})
        run_id = started.body["run_id"]

        resp = get_answer(cp, run_id)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["answer"], started.body["answer"])


class ConversationPromptTest(unittest.TestCase):
    def test_no_history_returns_the_message_unchanged(self) -> None:
        """单轮请求的 prompt 与 M93 之前**逐字相同** —— 加多轮不改单轮行为。"""
        self.assertEqual(_conversation_prompt(None, "hi"), "hi")
        self.assertEqual(_conversation_prompt([], "hi"), "hi")
        self.assertEqual(_conversation_prompt("not-a-list", "hi"), "hi")

    def test_history_is_folded_in_before_the_message(self) -> None:
        text = _conversation_prompt(
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
            "第二问",
        )
        self.assertIn("第一问", text)
        self.assertIn("第一答", text)
        self.assertTrue(text.rstrip().endswith("用户：第二问"))

    def test_malformed_entries_are_skipped(self) -> None:
        text = _conversation_prompt(
            [{"role": "user"}, {"content": "no role"}, 42, {"role": "assistant", "content": "ok"}],
            "now",
        )
        self.assertIn("ok", text)
        self.assertTrue(text.rstrip().endswith("用户：now"))


class _RecordingCP:
    """记录 `start_run` 收到的 `user_request`，其余返回一个完成的视图。"""

    def __init__(self) -> None:
        self.request = None

    def start_run(self, request):
        self.request = request
        return RunView(run_id="run_rec", agent_id=request.agent_id, status="completed")

    def drive_run(self, run_id):
        return RunView(run_id=run_id, agent_id=CHAT_AGENT_ID, status="completed")

    def answer(self, run_id):
        return "ok"


class ChatHistoryTest(unittest.TestCase):
    def test_history_reaches_the_run_as_a_user_request(self) -> None:
        cp = _RecordingCP()
        chat(
            cp,
            {
                "agent_id": CHAT_AGENT_ID,
                "message": "第二问",
                "history": [
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                ],
            },
        )
        self.assertIsNotNone(cp.request)
        self.assertIn("第一问", cp.request.user_request)
        self.assertIn("第一答", cp.request.user_request)
        self.assertTrue(cp.request.user_request.rstrip().endswith("用户：第二问"))

    def test_without_history_the_request_is_just_the_message(self) -> None:
        cp = _RecordingCP()
        chat(cp, {"agent_id": CHAT_AGENT_ID, "message": "hello"})
        self.assertEqual(cp.request.user_request, "hello")


class LastLlmAnswerTest(unittest.TestCase):
    def test_it_reads_the_llm_output(self) -> None:
        state = _State([_Obs({"result": {"response": {"text": "the answer"}}})])
        self.assertEqual(last_llm_answer(state), "the answer")

    def test_it_ignores_a_tool_result(self) -> None:
        """工具调用的输出不是回答 —— 不能拿它冒充。"""
        state = _State([_Obs({"result": {"tool": "add", "result": {"sum": 42}}})])
        self.assertEqual(last_llm_answer(state), "")

    def test_it_takes_the_most_recent_llm_output(self) -> None:
        state = _State(
            [
                _Obs({"result": {"response": {"text": "first"}}}),
                _Obs({"result": {"tool": "add", "result": {}}}),
                _Obs({"result": {"response": {"text": "second"}}}),
            ]
        )
        self.assertEqual(last_llm_answer(state), "second")

    def test_no_observations_is_an_empty_string(self) -> None:
        self.assertEqual(last_llm_answer(_State([])), "")
        self.assertEqual(last_llm_answer(None), "")


if __name__ == "__main__":
    unittest.main()
