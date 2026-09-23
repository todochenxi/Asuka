"""Skill：一段**可复用的做事方法**（基线 §25 / M3）。

Tool 与 Skill 的区别是这条线：

    Tool    一个**函数**        input → execute → output
    Skill   一段**做事方法**    goal → 内部多步 → result

"做事方法"决定的是**怎么拆步、按什么顺序、失败怎么退**，
所以它的形状天然比 Tool 复杂。基线 §25 列了三种形态：

    Prompt Skill     一段提示词（轻，单次模型调用）
    Workflow Skill   一张固定流程（中，Step 序列）
    Agentic Skill    一个子 Agent（重，内部自主决策）

⚠️ 三种形态**都复用同一套 Execution Kernel** —— 它们不是三种执行器，
而是同一个 `SkillSpec.kind` 下的三种**内容形状**。Skill Runtime 的职责是
把"这段方法"变成一条**派生子 Run 的请求**（`ChildRunRequest`），
真正跑起来仍走 `_suspend_for_child()` 那条 M25 的既有路径。

本模块只做**定义与注册表**（spec + registry）—— 它是"技能从哪来"的唯一答案。
派发与执行故意不在这里：那已经是 Loop / Kernel 的事（B-7：一个事实一处定义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class SkillKind(str, Enum):
    """§25 的三种技能形态（闭集，未知值拒绝 —— 同 `PlanNodeKind` / `ActionType`）。"""

    PROMPT = "prompt"          # 一段提示词：单次模型调用
    WORKFLOW = "workflow"      # 一张固定流程：Step 序列
    AGENTIC = "agentic"        # 一个子 Agent：内部自主决策


@dataclass(frozen=True)
class SkillSpec:
    """一个技能的**声明**（它是契约，不是注释）。

    同 `ToolSpec`：`name` / `version` 是身份，缺一不可。
    `kind` 是闭集，判定它的**执行形状**。
    `body` 按 kind 装不同东西：

        PROMPT    一段提示词文本
        WORKFLOW  `{"steps": [...]}` —— 顺序执行的步骤
        AGENTIC   `{"agent_id": "...", "goal": "..."}` —— 子 Agent 的目标

    ⚠️ `body` 刻意是 `Mapping`，不是各写一个字段：三种形态**共享一个字段**，
    于是"这份技能的内容是什么"只有一个地方可查；拆成 `prompt` / `steps` /
    `agent_id` 三个字段的话，`kind=prompt` 的技能会带着两个空字段到处走，
    而"哪个字段才是它的内容"没有答案。
    """

    name: str
    kind: SkillKind = SkillKind.PROMPT
    version: str = "1.0.0"
    description: str = ""
    body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillSpec.name is required")
        if not self.version:
            raise ValueError("SkillSpec.version is required")
        # 闭集：未知 kind 拒绝（不做"兜底成 prompt"—— 那正是要消灭的行为）
        if not isinstance(self.kind, SkillKind):
            try:
                object.__setattr__(self, "kind", SkillKind(self.kind))
            except ValueError as exc:
                known = [k.value for k in SkillKind]
                raise ValueError(
                    f"SkillSpec {self.name!r} has unknown kind {self.kind!r}; "
                    f"expected one of {known}"
                ) from exc
        self._validate_body()

    def _validate_body(self) -> None:
        """每种形态**必须说自己要干什么** —— 空 body 是一个没有内容的声明。"""
        if self.kind is SkillKind.PROMPT:
            if not str(self.body.get("prompt") or "").strip():
                raise ValueError(
                    f"SkillSpec {self.name!r} is a prompt skill but its body has no "
                    f"'prompt'; a prompt skill with no prompt does nothing"
                )
        elif self.kind is SkillKind.WORKFLOW:
            steps = self.body.get("steps")
            if not isinstance(steps, (list, tuple)) or not steps:
                raise ValueError(
                    f"SkillSpec {self.name!r} is a workflow skill but its body has no "
                    f"non-empty 'steps' list"
                )
        elif self.kind is SkillKind.AGENTIC:
            if not str(self.body.get("agent_id") or "").strip():
                raise ValueError(
                    f"SkillSpec {self.name!r} is an agentic skill but its body names no "
                    f"'agent_id'"
                )

    @property
    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"


class SkillNotFoundError(Exception):
    """技能不存在 / 版本不存在。PERMANENT —— 重试一百年也不会有。"""

    def __init__(self, name: str, version: str | None = None) -> None:
        wanted = f"{name}@{version}" if version else name
        super().__init__(f"no such skill: {wanted}")
        self.name = name
        self.version = version


class SkillRegistry:
    """`name@version → SkillSpec`（M3）。

    与 `ToolRegistry` 同形：显式注册、显式默认版本、`has()` / `specs()` 视图。
    区别只有一个 —— 这里存的是**声明**，没有 invoker：技能的"执行器"是
    派生一条子 Run，那由 Loop 接走，不在注册表里。
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], SkillSpec] = {}
        self._defaults: dict[str, str] = {}

    def register(self, spec: SkillSpec, *, make_default: bool = False) -> None:
        key = (spec.name, spec.version)
        if key in self._rows and not make_default:
            raise ValueError(f"skill {spec.qualified_name} already registered")
        self._rows[key] = spec
        if make_default or spec.name not in self._defaults:
            self._defaults[spec.name] = spec.version

    def set_default(self, name: str, version: str) -> None:
        if (name, version) not in self._rows:
            raise SkillNotFoundError(name, version)
        self._defaults[name] = version

    def resolve(self, name: str, version: str | None = None) -> SkillSpec:
        if version is None:
            version = self._defaults.get(name)
            if version is None:
                raise SkillNotFoundError(name)
        row = self._rows.get((name, version))
        if row is None:
            raise SkillNotFoundError(name, version)
        return row

    def has(self, name: str, version: str | None = None) -> bool:
        try:
            self.resolve(name, version)
            return True
        except SkillNotFoundError:
            return False

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(v for (n, v) in self._rows if n == name)

    def specs(self) -> Mapping[str, SkillSpec]:
        return {n: self.resolve(n) for n in sorted({n for n, _ in self._rows})}


__all__ = [
    "SkillKind",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillSpec",
]
