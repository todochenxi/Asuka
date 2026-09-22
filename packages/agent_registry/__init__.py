"""Agent 注册表（M61 / M11 Registry；闭合空洞 231）。

--------------------------------------------------------------------------
它解决什么

空洞 231 登记的是：`POST /agents/{id}/runs` **不校验 agent 是否存在** ——
传任何字符串都 201。当时判为"抽象缺口，等需要时再治"，
因为 `agent_id` 那时只是个透传标签、不参与分派。

注册表让它**参与分派**：登记了哪些 agent、每个用哪套栈。
于是"这个 id 存不存在"第一次有了答案。

--------------------------------------------------------------------------
为什么"没配注册表就维持现状"是必须的（不是偷懒）

仓库里各测试用了 7 种即席的 agent id（`agent-it` / `agent-api` /
`agent-math` / `agent-1` / `agent-saga` / `agent-rec` / `agent-ctx`），
它们都不是"登记过的 agent"，而是一次性起的名字。

若强制校验，这 7 类用例会全部红。而它们红的理由**不是**有 bug，
只是没写注册表 —— 那会把"没登记"和"写错了"混成一种错。

所以：

    没配注册表  → 维持现状（agent_id 透传，任何 id 都收）
    配了注册表  → 只收登记过的 id，其余 404 AGENT_NOT_FOUND

这不是回避，是把"要不要强校验"留给部署决定 ——
而一旦决定要，校验就真的生效（不再是登记里那句"等需要时"）。

--------------------------------------------------------------------------
为什么是 TOML

与 manifest 同：`tomllib` 是标准库，零新增依赖。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Any, Mapping


class RegistryError(ValueError):
    """注册表写错了 —— 与 manifest 同理，启动前就该死。"""


@dataclass(frozen=True)
class Agent:
    """一个登记过的 agent。"""

    agent_id: str
    description: str = ""
    #: 这个 agent 用哪套栈。**可选** —— 不写就用全局的
    #: `AGENTOS_STACK_PROVIDER`（大多数部署只有一个栈，不必每个 agent 都写一遍）。
    stack: str = ""
    model: str = ""
    tool: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "description": self.description,
            "stack": self.stack,
            "model": self.model,
            "tool": self.tool,
        }


class Registry:
    """登记了哪些 agent。"""

    def __init__(self, agents: Mapping[str, Agent] | None = None) -> None:
        self._agents: dict[str, Agent] = dict(agents or {})

    def knows(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))

    def all(self) -> tuple[Agent, ...]:
        return tuple(self._agents[k] for k in self.ids())

    def __len__(self) -> int:
        return len(self._agents)


def parse(text: str) -> Registry:
    """读一份注册表文本。

    形状：

        [agents.agent-it]
        description = "集成测试栈"
        stack = "examples.demo_stack:build_approval_demo_stack_factory"

        [agents.agent-math]
        description = "只做算术"

    `stack` / `model` / `tool` 都可选 —— 不写就跟随全局配置。
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f"REGISTRY_NOT_TOML: {e}") from None

    section = raw.get("agents")
    if section is None:
        raise RegistryError("REGISTRY_NO_AGENTS: expected an [agents] section")
    if not isinstance(section, Mapping):
        raise RegistryError("REGISTRY_BAD_SHAPE: [agents] must be a table")

    agents: dict[str, Agent] = {}
    for agent_id, body in section.items():
        if not isinstance(body, Mapping):
            raise RegistryError(
                f"REGISTRY_BAD_AGENT: agents.{agent_id} must be a table"
            )
        unknown = sorted(
            k for k in body
            if k not in ("description", "stack", "model", "tool")
        )
        if unknown:
            raise RegistryError(
                f"REGISTRY_UNKNOWN_KEY: agents.{agent_id} has "
                + ", ".join(unknown)
                + " (allowed: description, stack, model, tool)"
            )
        for key, value in body.items():
            if not isinstance(value, str):
                raise RegistryError(
                    f"REGISTRY_BAD_TYPE: agents.{agent_id}.{key} must be a string"
                )
        agents[str(agent_id)] = Agent(
            agent_id=str(agent_id),
            description=str(body.get("description", "")),
            stack=str(body.get("stack", "")),
            model=str(body.get("model", "")),
            tool=str(body.get("tool", "")),
        )

    if not agents:
        raise RegistryError("REGISTRY_EMPTY: no agents declared")

    return Registry(agents)


def load(path: str) -> Registry:
    with open(path, "rb") as fh:
        return parse(fh.read().decode("utf-8"))
