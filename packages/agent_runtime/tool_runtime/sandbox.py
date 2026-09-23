"""Sandbox：一个**声明得出、也真的守得住**的执行边界（基线 §24 / M9）。

`ToolProtocol.SANDBOX` 从 M15 起就冻结在枚举里，但直到现在全仓 **0 处**
真的跑在沙箱里 —— 它只是一个"声明了却没人管"的协议值
（同 `ActionType` 在 M90 之前的处境）。

### 先说清楚这个沙箱**能**和**不能**做什么

进程内 Python **无法**关住任意代码：一个子进程照样能 `open("/etc/passwd")`
或连外网。任何声称"进程内 Python 沙箱能隔离文件系统/网络"的实现都是在编
（M88：声明与能力之间的差必须由系统说出来）。所以这里**只**提供能真正强制的东西：

    ✅ 硬超时          —— 到点 `kill` 进程（`subprocess` 能杀）
    ✅ 输出上限        —— 超了拒绝，而不是悄悄截断
    ✅ 环境变量白名单  —— 子进程只拿到显式列出的变量（父进程的密钥不外泄）
    ✅ 禁止 shell 字符串 —— 只收 argv 列表，杜绝注入
    ✅ 工作目录        —— 每次调用一个干净目录（默认）
    ✅ 声明路径校验    —— 工具自己说会碰哪些路径，越界即拒（**尽力而为的输入校验，
                          不是文件系统隔离**）

    ❌ 文件系统隔离    —— 需要容器 / namespace，本实现不声称
    ❌ 网络出站隔离    —— 同上

**因此这里刻意没有 `network_hosts` 这个字段**：提供一个管不住的旋钮，
比不提供更糟 —— 它会让读代码的人以为网络被挡住了。

### 契约（M101 收窄 T-6）

T-6 说"换个协议只换 Invoker"。本模块不推翻它，而是补一句：
`protocol=SANDBOX` 的工具，它的 Invoker **必须**是 `SandboxedInvoker`
（见 `registry.register`）。一个声明了沙箱、却跑在裸函数里的工具，
其声明是句谎话 —— 注册期就拒。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from .spec import ToolSpec


class SandboxViolation(Exception):
    """越过沙箱边界。`code` 点名是哪一条边界。

    与 `ToolExecutionError` 分开定义（避免 `runtime` ↔ `sandbox` 循环 import），
    由 `ToolRuntime.call` 翻成带 code 的 `ToolExecutionError`。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_within(path: str, root: str) -> bool:
    """`path` 是否在 `root` 之内（含 root 自身）。

    用 `commonpath` 而不是字符串 `startswith` —— 后者会把 `/data-evil`
    误判成在 `/data` 里。不同盘符时 `commonpath` 抛 `ValueError`，判否。
    """
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:
        return False


@dataclass(frozen=True)
class SandboxProfile:
    """一次沙箱执行被允许触碰的全部东西（闭集）。

    `read_roots` / `write_roots` 为空 = **一个路径都不许碰**（fail-closed）。
    这不是"不限制"，而是"没有授权" —— 与"没有规则就是放行"相反。
    """

    timeout: timedelta = timedelta(seconds=10)
    max_output_bytes: int = 64 * 1024
    env_allowlist: tuple[str, ...] = ()
    #: None = 每次调用开一个干净临时目录；给了就用它（不清理）
    workdir: str | None = None
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout <= timedelta(0):
            raise ValueError("SandboxProfile.timeout must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("SandboxProfile.max_output_bytes must be positive")
        object.__setattr__(self, "env_allowlist", tuple(self.env_allowlist))
        object.__setattr__(self, "read_roots", tuple(self.read_roots))
        object.__setattr__(self, "write_roots", tuple(self.write_roots))

    # ------------------------------------------------------------ 路径
    def check_path(self, path: str, *, write: bool = False) -> str:
        """工具声明的路径是否被授权。越界抛 `SANDBOX_PATH_DENIED`。

        写授权天然包含读授权（能改的路径当然能读）。
        """
        roots = (*self.write_roots, *self.read_roots) if not write else self.write_roots
        if not any(_is_within(path, root) for root in roots):
            mode = "write" if write else "read"
            raise SandboxViolation(
                "SANDBOX_PATH_DENIED",
                f"{mode} path {path!r} is outside the sandbox profile's allowed roots",
            )
        return path

    # ------------------------------------------------------------ 环境
    def child_env(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        """子进程能看到的变量 —— **只有**白名单里的，其余一律不带过去。"""
        source = os.environ if source is None else source
        return {k: source[k] for k in self.env_allowlist if k in source}

    # ------------------------------------------------------------ 超时
    def effective_timeout(self, requested: timedelta | None) -> timedelta:
        """调用请求的超时只能比 profile **更严**，不能更宽。"""
        if requested is None:
            return self.timeout
        return min(self.timeout, requested)


class SandboxedInvoker:
    """标记基类：这个 Invoker **真的**把调用关进了沙箱。

    远程沙箱服务（比如把命令发给一个隔离容器）也可以继承它 ——
    契约问的是"你有没有真的沙箱化"，不是"你是不是本地的 `subprocess` 版"。
    """

    profile: SandboxProfile


@dataclass
class SandboxedCommandInvoker(SandboxedInvoker):
    """跑一条命令，并把沙箱 profile 真正**执行**出来。

    `call.args`：

        command  : list[str]        必填。**只收 argv 列表** —— 字符串（shell 形式）拒绝
        paths    : list[str | {path, write}]  选填。工具自述会碰的路径，越界即拒
        cwd      : str              选填。必须在 `write_roots` 里（要写就得授权）

    返回值：`{"exit_code", "stdout", "stderr"}`。
    """

    profile: SandboxProfile
    #: 非 0 退出是否算失败。默认算 —— 一个"跑挂了"的命令不该被读成成功。
    fail_on_nonzero: bool = True

    def invoke(self, call) -> Mapping[str, Any]:
        argv = self._argv(call)
        self._check_declared_paths(call)

        timeout = self.profile.effective_timeout(call.timeout)
        env = self.profile.child_env()
        workdir, cleanup = self._workdir()
        try:
            proc = subprocess.run(  # noqa: S603 - argv 列表 + shell=False，无注入面
                argv,
                cwd=workdir,
                env=env,
                timeout=timeout.total_seconds(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as err:
            raise SandboxViolation(
                "SANDBOX_TIMEOUT",
                f"command exceeded the sandbox timeout of {timeout.total_seconds()}s "
                f"and was killed: {argv!r}",
            ) from err
        except FileNotFoundError as err:
            raise SandboxViolation(
                "SANDBOX_COMMAND_NOT_FOUND", f"cannot execute {argv[0]!r}: {err}"
            ) from err
        finally:
            if cleanup:
                shutil.rmtree(workdir, ignore_errors=True)

        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
        total = len(stdout) + len(stderr)
        if total > self.profile.max_output_bytes:
            # 拒绝而不是截断 —— 静默截断会把"输出被砍过"这件事藏起来
            raise SandboxViolation(
                "SANDBOX_OUTPUT_LIMIT",
                f"command produced {total} bytes of output, over the sandbox limit "
                f"of {self.profile.max_output_bytes}",
            )
        if self.fail_on_nonzero and proc.returncode != 0:
            raise SandboxViolation(
                "SANDBOX_COMMAND_FAILED",
                f"command exited with {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()[:400]}",
            )
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    # ------------------------------------------------------------ 内部
    def _argv(self, call) -> list[str]:
        raw = call.args.get("command")
        if isinstance(raw, str):
            raise SandboxViolation(
                "SANDBOX_SHELL_REFUSED",
                "command must be an argv list, not a shell string; "
                "a shell string would defeat the no-shell guarantee",
            )
        if not isinstance(raw, (list, tuple)) or not raw:
            raise SandboxViolation(
                "SANDBOX_BAD_COMMAND",
                "command must be a non-empty argv list",
            )
        if not all(isinstance(a, str) for a in raw):
            raise SandboxViolation(
                "SANDBOX_BAD_COMMAND", "every argv element must be a string"
            )
        return list(raw)

    def _check_declared_paths(self, call) -> None:
        paths = call.args.get("paths")
        if paths is None:
            return
        if not isinstance(paths, (list, tuple)):
            raise SandboxViolation(
                "SANDBOX_BAD_PATH_ENTRY", "'paths' must be a list"
            )
        for entry in paths:
            if isinstance(entry, str):
                self.profile.check_path(entry, write=False)
            elif isinstance(entry, Mapping) and "path" in entry:
                self.profile.check_path(
                    str(entry["path"]), write=bool(entry.get("write", False))
                )
            else:
                raise SandboxViolation(
                    "SANDBOX_BAD_PATH_ENTRY",
                    f"each declared path must be a string or {{'path', 'write'}}; "
                    f"got {entry!r}",
                )

    def _workdir(self) -> tuple[str, bool]:
        if self.profile.workdir is not None:
            self.profile.check_path(self.profile.workdir, write=True)
            return self.profile.workdir, False
        return tempfile.mkdtemp(prefix="agentos-sbx-"), True


def spec_requires_sandbox(spec: ToolSpec) -> bool:
    """这个工具是否声明了沙箱协议（注册契约的判据，只此一处）。"""
    from .spec import ToolProtocol

    return spec.protocol is ToolProtocol.SANDBOX


__all__ = [
    "SandboxProfile",
    "SandboxViolation",
    "SandboxedCommandInvoker",
    "SandboxedInvoker",
    "spec_requires_sandbox",
]
