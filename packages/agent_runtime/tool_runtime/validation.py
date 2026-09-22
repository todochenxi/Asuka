"""Input Validation（§24 流程的第三环）。

**T-3：校验失败是 PERMANENT，不是 TRANSIENT。**

参数少了一个必填字段，重试一百次也是少那个字段。
把它判成可重试的后果是：白白占用 retry budget、拖慢 P99，
而且最后抛的还是同一个错 —— 纯粹浪费。

### 为什么自己写一个极简校验而不引入 jsonschema

`packages/` 至今零第三方依赖，这是能 60ms 跑完 276 个测试的前提之一。
而且工具入参通常只有几个字段，`required` + `type` 两件事就够用了。
真要完整 JSON-Schema 时，替换这一个函数即可，接口不变。
"""
from __future__ import annotations

from typing import Any, Mapping

from .spec import ToolSpec

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


class ToolValidationError(Exception):
    """入参不合法。调用方应把它翻译成 `FailureClass.PERMANENT`（T-3）。"""

    def __init__(self, tool: str, problems: list[str]) -> None:
        super().__init__(f"invalid input for tool {tool!r}: " + "; ".join(problems))
        self.tool = tool
        self.problems = tuple(problems)


def validate_input(spec: ToolSpec, args: Mapping[str, Any]) -> None:
    """按 `spec.input_schema` 校验入参。schema 为空则只查 args 是不是 Mapping。"""
    if not isinstance(args, Mapping):
        raise ToolValidationError(spec.name, ["args must be a mapping"])

    schema = spec.input_schema
    if not schema:
        return

    problems: list[str] = []

    for key in schema.get("required", ()):
        if key not in args:
            problems.append(f"missing required field: {key}")

    properties: Mapping[str, Any] = schema.get("properties", {}) or {}
    for key, rule in properties.items():
        if key not in args:
            continue
        expected = rule.get("type") if isinstance(rule, Mapping) else None
        if not expected:
            continue
        allowed = _TYPE_CHECKS.get(expected)
        if allowed is None:
            continue
        value = args[key]
        # bool 是 int 的子类，但 JSON 里它们是不同类型 —— 别让 True 通过 integer 校验
        if expected == "integer" and isinstance(value, bool):
            problems.append(f"{key}: expected integer, got boolean")
            continue
        if not isinstance(value, allowed):
            problems.append(f"{key}: expected {expected}, got {type(value).__name__}")

    if problems:
        raise ToolValidationError(spec.name, problems)
