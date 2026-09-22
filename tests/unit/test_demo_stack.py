"""`examples/demo_stack.py` 必须**真的跑得通**，不只是装得上。

--------------------------------------------------------------------------
为什么要有这个文件

它是 `AGENTOS_STACK_PROVIDER` 的默认实现 —— 也就是"照着文档起一个系统"
时第一个被指向的东西。而 M28 的探针发现它**从来没跑通过一次模型调用**：

    Attempt #1 FAILED  GATEWAY_FAILED
    NO_MODEL_REQUESTED: CompletionRequest.model_id is empty
                        and gateway.default_model_id is not set

`build_model_gateway()` 建了 Gateway 却没设 `default_model_id`，
于是 `LLMCallExecutor` 拿着空 `model_id` 去调，第一步就 PERMANENT 失败。

**测试一直是绿的**，因为没人测"跑得通"，只测了"装得上"：
`test_http_api.py` 里那条 `test_the_real_provider_loads_through_importlib`
断言的是 `callable(factory)`。装载成功 ≠ 跑得通。

这是 `CHILD_AGENT`（D-4）与 `compensation` 序列化（PR-27）的同一种病：
**一个字段/概念被冻结在文档里，没有任何一条测试要求它被设置。**

--------------------------------------------------------------------------
每条不变量配一个控制组。
"""
from __future__ import annotations

import unittest
from typing import Any

from packages.agent_domain.business.run import AgentRunStatus
from packages.agent_runtime.assembly import RuntimeStack
from packages.agent_runtime.model_gateway import CompletionRequest, ModelGateway

from examples.demo_stack import (
    build_model_gateway,
    build_stack_factory,
    build_tool_runtime,
)


class GatewayIsUsableTest(unittest.TestCase):
    """G-8：缺省模型由 Gateway 决定 —— 它必须真的被决定过。"""

    def test_g8_the_gateway_has_a_default_model(self) -> None:
        gateway = build_model_gateway()
        self.assertTrue(
            gateway.default_model_id,
            "示例栈的 Gateway 没设 default_model_id —— "
            "调用方拿空 model_id 来调会直接 NO_MODEL_REQUESTED",
        )

    def test_a_request_without_a_model_id_still_completes(self) -> None:
        """G-8 的落点：不指定模型也调得通。

        M28 之前这条是红的（`NO_MODEL_REQUESTED`，PERMANENT）。
        """
        gateway = build_model_gateway()
        call = gateway.complete(CompletionRequest(prompt="hello"))
        self.assertTrue(call.attempts)
        self.assertTrue(
            all(a.ok for a in call.attempts),
            f"模型调用失败：{[a.error_code for a in call.attempts]}",
        )
        self.assertTrue(call.response.text)

    def test_the_control_a_bad_model_id_is_still_refused(self) -> None:
        """控制组：写错模型名照样报错 —— 上一条不是因为"凡是请求都放行"。

        注意它抛的是 `GatewayError`（路由选不出来），不是返回一个"失败的结果"：
        换多少次 Attempt 也没用，所以不该给 Kernel 留下"再试一次"的余地。
        """
        from packages.agent_runtime.model_gateway.gateway import GatewayError

        gateway = build_model_gateway()
        with self.assertRaises(GatewayError) as cm:
            gateway.complete(CompletionRequest(prompt="hello", model_id="nope"))
        self.assertIn("nope", str(cm.exception))


class DemoStackRunsTest(unittest.TestCase):
    """整个示例栈必须能把一条 Run **跑到完成**，不是只 start 一下。"""

    def _stack(self) -> RuntimeStack:
        return build_stack_factory(None)("demo", None)

    def test_one_step_does_not_fail(self) -> None:
        """M28 之前这一步就是 `StepOutcome.FAILED`。"""
        stack = self._stack()
        stack.start("帮我算一下 1+1")
        outcome = stack.loop.step()
        self.assertNotEqual(
            getattr(outcome, "value", outcome),
            "failed",
            "第一步就失败了 —— 示例栈根本跑不通",
        )

    def test_the_run_reaches_a_terminal_state(self) -> None:
        """`run()` 跑到停，Run 必须进终态（B-7）。"""
        stack = self._stack()
        stack.start("帮我算一下 1+1")
        stack.run()
        assert stack.loop.agent_run is not None
        self.assertTrue(
            stack.loop.agent_run.status.is_terminal
            if hasattr(stack.loop.agent_run.status, "is_terminal")
            else stack.loop.agent_run.status
            in (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED),
            f"跑完了却不是终态：{stack.loop.agent_run.status}",
        )

    def test_the_trace_records_a_model_call(self) -> None:
        """账本里真的有一步模型调用 —— 上一条不是因为"什么都没做就结束了"。"""
        stack = self._stack()
        stack.start("帮我算一下 1+1")
        stack.run()
        kinds = [getattr(e, "kind", "") for e in stack.loop.trace]
        self.assertTrue(
            any("llm" in str(k).lower() or "task" in str(k).lower() for k in kinds),
            f"账本里没有模型调用的痕迹：{kinds}",
        )

    def test_the_control_two_runs_do_not_share_decision_state(self) -> None:
        """控制组：第二个 Run 也跑得完 —— DecisionEngine 是有状态的，别跨 Run 共享。"""
        factory = build_stack_factory(None)
        first = factory("demo", None)
        first.start("第一件事")
        first.run()
        second = factory("demo", None)
        second.start("第二件事")
        second.run()
        assert second.loop.agent_run is not None
        self.assertNotEqual(second.loop.agent_run.status, AgentRunStatus.CREATED)


class ToolRuntimeIsUsableTest(unittest.TestCase):
    def test_a_write_tool_without_an_idempotency_key_is_refused(self) -> None:
        """T-2：WRITE 工具必须带去重键。示例栈靠这条让它在最浅的路上被看见。

        它抛的是 `ToolExecutionError`（调用前就被拒），
        不是"返回结果里带个错误" —— 没有去重键的写操作不许发出去。
        """
        from packages.agent_runtime.tool_runtime.runtime import ToolExecutionError

        runtime = build_tool_runtime()
        with self.assertRaises(ToolExecutionError) as cm:
            runtime.call("note.write", {"text": "hi"})
        self.assertIn("idempotency_key", str(cm.exception))

    def test_the_control_a_write_tool_with_a_key_succeeds(self) -> None:
        """控制组：给了去重键就成功，且键**出现在结果里**（审计要能追溯）。

        上一条不是因为工具根本用不了。
        """
        runtime = build_tool_runtime()
        result = runtime.call(
            "note.write", {"text": "hi"}, idempotency_key="k1"
        )
        self.assertEqual(result.idempotency_key, "k1")
        self.assertEqual(result.output.get("stored"), "hi")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
