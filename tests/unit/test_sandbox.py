"""M101：Sandbox —— `ToolProtocol.SANDBOX` 从"声明"变成"真跑在沙箱里"（基线 §24 / M9）。

此前 `SANDBOX` 只是枚举里的一个值（全仓 0 处真的隔离）。这里给它一个
**只声称它守得住的东西**的实现：

    ✅ 硬超时 / 输出上限 / 环境白名单 / 禁 shell / 干净工作目录 / 声明路径校验
    ❌ 文件系统隔离、网络出站隔离（需要 OS 级 namespace，本实现**不**提供旋钮）

判据的重点是**边界真的会拦**，以及**声明了沙箱的协议必须配真的沙箱**。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta

from packages.agent_runtime.tool_runtime import (
    SandboxedCommandInvoker,
    SandboxProfile,
    SandboxViolation,
    SideEffect,
    ToolExecutionError,
    ToolProtocol,
    ToolRegistry,
    ToolRuntime,
    ToolSpec,
)


def _runtime(profile: SandboxProfile, *, fail_on_nonzero: bool = True) -> ToolRuntime:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="run", protocol=ToolProtocol.SANDBOX, side_effect=SideEffect.READ),
        SandboxedCommandInvoker(profile=profile, fail_on_nonzero=fail_on_nonzero),
    )
    return ToolRuntime(reg)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class ProfileTest(unittest.TestCase):
    def test_positive_limits_are_required(self) -> None:
        with self.assertRaises(ValueError):
            SandboxProfile(timeout=timedelta(0))
        with self.assertRaises(ValueError):
            SandboxProfile(max_output_bytes=0)

    def test_an_empty_allowlist_means_no_path_at_all(self) -> None:
        """fail-closed：没有授权 ≠ 不限制。"""
        profile = SandboxProfile()
        with self.assertRaises(SandboxViolation) as ctx:
            profile.check_path("/tmp/whatever")
        self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")

    def test_write_implies_read_but_not_the_reverse(self) -> None:
        profile = SandboxProfile(read_roots=("/data",), write_roots=("/data/out",))
        profile.check_path("/data/out/report.txt", write=True)   # 写授权内
        profile.check_path("/data/out/report.txt")               # 写⇒读
        profile.check_path("/data/in.txt")                       # 只读区
        with self.assertRaises(SandboxViolation):
            profile.check_path("/data/in.txt", write=True)       # 只读区不许写

    def test_a_sibling_prefix_is_not_inside_the_root(self) -> None:
        """`/data-evil` 不在 `/data` 里 —— 字符串 startswith 会判错。"""
        profile = SandboxProfile(read_roots=("/data",))
        with self.assertRaises(SandboxViolation):
            profile.check_path("/data-evil/secret")

    def test_child_env_keeps_only_the_allowlist(self) -> None:
        profile = SandboxProfile(env_allowlist=("KEEP",))
        env = profile.child_env({"KEEP": "1", "SECRET": "leak"})
        self.assertEqual(env, {"KEEP": "1"})

    def test_a_requested_timeout_can_only_be_stricter(self) -> None:
        profile = SandboxProfile(timeout=timedelta(seconds=10))
        self.assertEqual(profile.effective_timeout(None), timedelta(seconds=10))
        self.assertEqual(profile.effective_timeout(timedelta(seconds=3)), timedelta(seconds=3))
        self.assertEqual(profile.effective_timeout(timedelta(seconds=99)), timedelta(seconds=10))


class CommandBoundaryTest(unittest.TestCase):
    def test_a_command_runs_and_its_output_is_returned(self) -> None:
        result = _runtime(SandboxProfile()).call("run", {"command": _py("print('hi')")})
        self.assertEqual(result.output["exit_code"], 0)
        self.assertIn("hi", result.output["stdout"])

    def test_a_shell_string_is_refused(self) -> None:
        """只收 argv 列表 —— shell 字符串会毁掉"无 shell"这条保证。"""
        with self.assertRaises(ToolExecutionError) as ctx:
            _runtime(SandboxProfile()).call("run", {"command": "echo hi"})
        self.assertEqual(ctx.exception.code, "SANDBOX_SHELL_REFUSED")

    def test_an_empty_argv_is_refused(self) -> None:
        with self.assertRaises(ToolExecutionError) as ctx:
            _runtime(SandboxProfile()).call("run", {"command": []})
        self.assertEqual(ctx.exception.code, "SANDBOX_BAD_COMMAND")

    def test_a_nonzero_exit_is_a_failure(self) -> None:
        with self.assertRaises(ToolExecutionError) as ctx:
            _runtime(SandboxProfile()).call("run", {"command": _py("import sys; sys.exit(3)")})
        self.assertEqual(ctx.exception.code, "SANDBOX_COMMAND_FAILED")

    def test_nonzero_can_be_allowed_explicitly(self) -> None:
        result = _runtime(SandboxProfile(), fail_on_nonzero=False).call(
            "run", {"command": _py("import sys; sys.exit(3)")}
        )
        self.assertEqual(result.output["exit_code"], 3)

    def test_output_over_the_limit_is_refused_not_truncated(self) -> None:
        profile = SandboxProfile(max_output_bytes=64)
        with self.assertRaises(ToolExecutionError) as ctx:
            _runtime(profile).call("run", {"command": _py("print('x' * 100000)")})
        self.assertEqual(ctx.exception.code, "SANDBOX_OUTPUT_LIMIT")

    def test_a_hung_command_is_killed(self) -> None:
        profile = SandboxProfile(timeout=timedelta(seconds=0.4))
        with self.assertRaises(ToolExecutionError) as ctx:
            _runtime(profile).call("run", {"command": _py("import time; time.sleep(30)")})
        self.assertEqual(ctx.exception.code, "SANDBOX_TIMEOUT")

    def test_the_parent_secrets_do_not_reach_the_child(self) -> None:
        os.environ["AGENTOS_SBX_SECRET"] = "leak"
        try:
            result = _runtime(SandboxProfile()).call(
                "run",
                {"command": _py("import os; print(os.environ.get('AGENTOS_SBX_SECRET', '<none>'))")},
            )
        finally:
            os.environ.pop("AGENTOS_SBX_SECRET", None)
        self.assertIn("<none>", result.output["stdout"])

    def test_a_declared_path_outside_the_roots_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            profile = SandboxProfile(read_roots=(root,))
            with self.assertRaises(ToolExecutionError) as ctx:
                _runtime(profile).call(
                    "run",
                    {"command": _py("print('x')"), "paths": [os.path.join(root, "..", "escape")]},
                )
            self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")

    def test_the_workdir_is_honoured_and_must_be_authorised(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            profile = SandboxProfile(workdir=root, write_roots=(root,))
            result = _runtime(profile).call(
                "run", {"command": _py("import os; print(os.getcwd())")}
            )
            self.assertEqual(
                os.path.normcase(os.path.realpath(result.output["stdout"].strip())),
                os.path.normcase(os.path.realpath(root)),
            )

        # 没授权的 workdir → 拒绝
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ToolExecutionError) as ctx:
                _runtime(SandboxProfile(workdir=root)).call("run", {"command": _py("print(1)")})
            self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")


class ContractTest(unittest.TestCase):
    def test_a_sandbox_protocol_without_a_sandbox_invoker_is_refused(self) -> None:
        from packages.agent_runtime.tool_runtime import FunctionInvoker

        reg = ToolRegistry()
        with self.assertRaises(ValueError) as ctx:
            reg.register(
                ToolSpec(name="t", protocol=ToolProtocol.SANDBOX),
                FunctionInvoker(lambda args: {"ok": True}),
            )
        self.assertIn("sandbox", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
