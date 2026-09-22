"""Tool Runtime：把一次工具调用走完（基线 §24）。

```text
Tool Call
 ↓
Resolution              ← registry.resolve(name, version)
 ↓
Version Resolution      ← 同上，默认版本必须显式登记（T-5）
 ↓
Input Validation        ← validation.validate_input（T-3：失败即 PERMANENT）
 ↓
✗ Policy / Guardrail    ← **不在这里**，见 T-4
 ↓
Timeout                 ← 取 min(spec / request / default)
 ↓
✗ Retry                 ← **不在这里**，见 T-1
 ↓
Executor                ← invoker.invoke(call)
 ↓
Execution Result        ← ToolResult（记实际版本 + 透传的 key）
```

### T-4：Policy / Guardrail / Rate Limit 不在 Tool Runtime 里

§24 的流程里画了它们，但 §2 把 Harness 钉成 **唯一拦截点**。
执行者应该是 Harness，Tool Runtime 只是被调用方 ——
否则"这个动作能不能做"就有了两个判断处，也就是第二个事实源。

Tool Runtime 保留的是**执行机制**：解析、版本、校验、超时、协议适配。

### T-1：Tool Runtime 不做业务重试

§24 流程里也画了 Retry。但重试已经由 Kernel 的 Attempt 统一管了（带 FailureClass
判定与退避）。Tool Runtime 再重试一层就是双重重试：

```text
Tool Runtime Retry(3) × Kernel Attempt(3) = 9 次工具调用
```

这和 Model Gateway 的 G-1 是**同一个问题的另一种形态**：
单次 Attempt 内部的容错，都不应该产生新的 Attempt / 新的外部调用计数。
协议级的重试（连接池超时等）留在 Invoker 内部，且同样不许上报为"失败次数"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from .protocols import ToolCall
from .registry import ToolNotFoundError, ToolRegistry
from .spec import SideEffect, ToolResult, ToolSpec
from .validation import ToolValidationError, validate_input


class ToolExecutionError(Exception):
    """工具执行失败。`retryable` 决定 Kernel 要不要开新 Attempt。"""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ToolRuntime:
    """一次工具调用的执行入口。

    它**不**判断"该不该调这个工具"（那是 Harness），
    只负责"怎么把它调对"。
    """

    registry: ToolRegistry
    default_timeout: timedelta = timedelta(seconds=30)

    # ------------------------------------------------------------ 调用
    def call(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        idempotency_key: str = "",
        version: str | None = None,
        timeout: timedelta | None = None,
    ) -> ToolResult:
        # 1~2  Resolution + Version Resolution
        try:
            spec = self.registry.resolve(name, version)
        except ToolNotFoundError:
            # 工具不存在是 PERMANENT —— 重试到天亮它也不会出现
            raise

        # 3  Input Validation
        validate_input(spec, args)

        # ✗ 4  Policy / Guardrail / Rate Limit → Harness（T-4）

        # 5  Timeout：取三者最小值，谁更严格听谁的
        effective = self._effective_timeout(spec, timeout)

        # T-2：WRITE 类工具**必须**带去重键。
        # 漏传的后果是重复副作用（重复下单 / 重复扣款），这是最贵的一类 bug，
        # 所以这里是硬拒绝而不是警告。
        if spec.needs_idempotency_key and not idempotency_key:
            raise ToolExecutionError(
                "IDEMPOTENCY_KEY_REQUIRED",
                f"tool {spec.qualified_name} has side_effect="
                f"{spec.side_effect.value}; an idempotency_key is required (§16)",
                retryable=False,
            )

        # ✗ 6  Retry → Kernel（T-1）

        # 7  Executor
        invoker = self.registry.invoker_for(spec)
        call = ToolCall(
            spec=spec,
            args=dict(args),
            idempotency_key=idempotency_key,
            timeout=effective,
        )
        try:
            output = invoker.invoke(call)
        except ToolExecutionError:
            raise
        except Exception as err:                          # noqa: BLE001
            raise ToolExecutionError("TOOL_FAILED", str(err), retryable=True) from err

        # 8  Result
        return ToolResult(
            tool_name=spec.name,
            version=spec.version,                         # 实际执行的版本，不是请求值
            protocol=spec.protocol,
            side_effect=spec.side_effect,
            output=dict(output),
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------ 内部
    def _effective_timeout(
        self, spec: ToolSpec, timeout: timedelta | None
    ) -> timedelta:
        candidates = [self.default_timeout]
        if spec.timeout is not None:
            candidates.append(spec.timeout)
        if timeout is not None:
            candidates.append(timeout)
        return min(candidates)

    # ------------------------------------------------------------ 视图
    def spec_of(self, name: str, version: str | None = None) -> ToolSpec:
        return self.registry.resolve(name, version)

    def requires_idempotency_key(self, name: str, version: str | None = None) -> bool:
        return self.registry.resolve(name, version).needs_idempotency_key


__all__ = [
    "SideEffect",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRuntime",
    "ToolValidationError",
]
