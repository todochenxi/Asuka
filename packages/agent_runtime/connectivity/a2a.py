"""A2A（Agent2Agent）客户端与**远端委派**适配（基线 §26 / M4）。

### 它补的是什么

M4 的 Multi-Agent 早就落地了 —— 但**只限同进程**：`AgentLoop._suspend_for_child`
派生一条本地的子 Run（`InProcessChildRunSpawner`）。`AgentDelegationExecutor`
的 docstring 一直写着"A2A / §26"，可全仓没有一行真的说过 A2A 这门协议。
于是"把一条子任务交给**远端** Agent"此前只能靠手写一个 spawner 去假装。

这一层给三段：

    AgentCard          远端 Agent 的自述（名字 / 地址 / 能力 / 技能）
    A2AClient          `message/send` / `tasks/get` / `tasks/cancel`（复用 JSON-RPC 核心）
    A2AChildRunSpawner 把 AgentOS 的 `ChildRunSpawner` 协议接到远端 Agent 上

### 三条不变量

| # | 内容 |
|---|---|
| A-1 | **只有终态才写终态**。`working` / `submitted` 的轮询结果不许被记成 completed/failed —— 那是替一个还在跑的对象作证 |
| A-2 | `input-required` / `auth-required` **既不是终态也不是失败**。把它们当成 failed，会让"需要人补一句话"变成一次静默的委派失败（D-10 的同款） |
| A-3 | A2A 状态是**闭集**，未知字符串拒绝（不做"兜底成 working"—— 那正好会把一个真终态读成还在跑，父 Run 永远挂着） |

### 拼写：A2A 的 `canceled` → AgentOS 的 `cancelled`

A2A 用美式 `canceled`，AgentOS 的唤醒路径认的是英式 `cancelled`
（`child_wake` 的 `handle.status == "cancelled"`）。不做归一化的话，
一次远端取消会被读成 `failed` —— 于是**取消被当成失败**（D-10 那个空洞）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.ids import new_id

from ..delegation import (
    ChildRunHandle,
    ChildRunRegistry,
    ChildRunRegistryPort,
    ChildRunRequest,
    ChildRunUnavailable,
)
from .jsonrpc import JsonRpcClient
from .transport import HttpTransport

#: Agent Card 的相对路径（A2A 约定）。
AGENT_CARD_PATH = "/.well-known/agent-card.json"


class A2ATaskState(str, Enum):
    """A2A 的任务状态（闭集）。"""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"          # ⚠️ 美式拼写；归一化见模块头
    FAILED = "failed"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"
    UNKNOWN = "unknown"


#: 终态：只有落在这里的状态才允许写进登记处（A-1）。
TERMINAL_A2A_STATES = frozenset(
    {
        A2ATaskState.COMPLETED,
        A2ATaskState.CANCELED,
        A2ATaskState.FAILED,
        A2ATaskState.REJECTED,
    }
)

#: A2A 状态 → AgentOS 子 Run 状态。
#: `rejected` 归到 `failed`：唤醒路径只认 completed / cancelled / 其余当失败，
#: 把 rejected 单列一个新状态只会让 `_reason` 说"failed"，而状态字段说"rejected"——
#: 同一个问题两个答案。精确的 A2A 状态留在 `result["state"]` 里。
_A2A_TO_AGENTOS_STATUS: Mapping[A2ATaskState, str] = {
    A2ATaskState.COMPLETED: "completed",
    A2ATaskState.CANCELED: "cancelled",
    A2ATaskState.FAILED: "failed",
    A2ATaskState.REJECTED: "failed",
}


def parse_state(raw: Any) -> A2ATaskState:
    """把服务端给的字符串变成闭集成员（A-3）。未知即拒。"""
    try:
        return A2ATaskState(raw)
    except ValueError as exc:
        known = [s.value for s in A2ATaskState]
        raise InvariantViolation(
            f"A-3: unknown A2A task state {raw!r}; expected one of {known}"
        ) from exc


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCard:
    """远端 Agent 的自述。只抽我们真的用得到的几个字段。"""

    name: str
    url: str = ""
    description: str = ""
    version: str = ""
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    skills: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise InvariantViolation("an A2A agent card must name the agent")
        object.__setattr__(self, "capabilities", dict(self.capabilities))
        object.__setattr__(self, "skills", tuple(self.skills))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentCard":
        if not isinstance(data, Mapping) or not data.get("name"):
            raise InvariantViolation(
                f"not an A2A agent card (no 'name'): {data!r}"
            )
        raw_skills = data.get("skills")
        skills = tuple(raw_skills) if isinstance(raw_skills, (list, tuple)) else ()
        caps = data.get("capabilities")
        return cls(
            name=str(data["name"]),
            url=str(data.get("url") or ""),
            description=str(data.get("description") or ""),
            version=str(data.get("version") or ""),
            capabilities=dict(caps) if isinstance(caps, Mapping) else {},
            skills=skills,
        )


class AgentCardTransport(Protocol):
    """取一张 Agent Card。A2A 用普通 HTTP GET（不是 JSON-RPC）。"""

    def fetch(self, url: str) -> Mapping[str, Any]: ...


@dataclass
class InMemoryCardTransport:
    """`url → card` 的脚本化对端（测试用）。"""

    cards: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def fetch(self, url: str) -> Mapping[str, Any]:
        if url not in self.cards:
            raise KeyError(f"no agent card scripted for {url!r}")
        return dict(self.cards[url])


class HttpCardTransport:
    """真 HTTP GET（stdlib `urllib`，零第三方）。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def fetch(self, url: str) -> Mapping[str, Any]:
        import json
        import urllib.request

        with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
        parsed = json.loads(body)
        if not isinstance(parsed, Mapping):
            raise InvariantViolation(f"agent card at {url!r} is not a JSON object")
        return parsed


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class A2AClient:
    """一个远端 Agent 的会话。JSON-RPC 走 `rpc`，Agent Card 走 `card_transport`。"""

    rpc: JsonRpcClient
    base_url: str = ""
    card_transport: AgentCardTransport | None = None

    @classmethod
    def from_http(
        cls,
        url: str,
        *,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> "A2AClient":
        """用 HTTP JSON-RPC 连一个远端 Agent（M108）。"""
        return cls(
            rpc=JsonRpcClient(HttpTransport(url, headers=headers or {}, timeout=timeout)),
            base_url=base_url,
            card_transport=HttpCardTransport(timeout=timeout) if base_url else None,
        )

    def fetch_agent_card(self) -> AgentCard:
        if self.card_transport is None or not self.base_url:
            raise ChildRunUnavailable(
                "this A2AClient has no card transport / base_url; an agent card is "
                "an HTTP GET, which is a different transport from JSON-RPC"
            )
        return AgentCard.from_mapping(
            self.card_transport.fetch(self.base_url.rstrip("/") + AGENT_CARD_PATH)
        )

    # ------------------------------------------------------------ JSON-RPC
    def send_message(
        self,
        text: str,
        *,
        task_id: str = "",
        context_id: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """`message/send`。返回一个 Task 或一个 Message。"""
        parts: list[dict[str, Any]] = [{"kind": "text", "text": text}]
        if data:
            parts.append({"kind": "data", "data": dict(data)})
        message: dict[str, Any] = {
            "role": "user",
            "parts": parts,
            "messageId": new_id("a2amsg"),
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        result = self.rpc.call("message/send", {"message": message})
        if not isinstance(result, Mapping):
            raise InvariantViolation(f"message/send returned a non-object: {result!r}")
        return result

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        return _as_mapping(self.rpc.call("tasks/get", {"id": task_id}), "tasks/get")

    def cancel_task(self, task_id: str) -> Mapping[str, Any]:
        return _as_mapping(self.rpc.call("tasks/cancel", {"id": task_id}), "tasks/cancel")


# ---------------------------------------------------------------------------
# Remote delegation
# ---------------------------------------------------------------------------


@dataclass
class A2AChildRunSpawner:
    """把 `ChildRunSpawner` 接到远端 Agent（复用 Loop 那条既有委派路径）。

    - `spawn` 发 `message/send`，把远端 Task 的 id 当成 `child_run_id` 登记。
    - `poll` 是**远端到本地的结算通道**：`tasks/get` 到终态才写 `mark_finished`
      （A-1）。本地侧由既有的唤醒扫（`ChildRunWaker.sweep`）把结果交回父 Run。
    - `cancel_child` 发 `tasks/cancel`，同样只在远端**自己**报终态时才落终态
      （D-14：父不替一条它看不见的 Run 作证）。
    """

    clients: Mapping[str, A2AClient]
    registry: ChildRunRegistryPort = field(default_factory=ChildRunRegistry)

    # ------------------------------------------------------------ spawn
    def spawn(self, request: ChildRunRequest) -> ChildRunHandle:
        # D-1：先查再派。顺序反了，每次重试都多开一条远端任务。
        existing = self.registry.for_execution(request.parent_execution_id)
        if existing is not None:
            return existing

        client = self._client_for(request.target)
        response = client.send_message(
            request.instruction or request.target,
            context_id=request.parent_run_id,
            data=dict(request.payload),
        )
        task = _as_task_or_message(response)
        child_run_id = _task_id(task) or new_id("a2a")

        handle = ChildRunHandle(
            child_run_id=child_run_id,
            kind=request.kind,
            parent_run_id=request.parent_run_id,
            parent_execution_id=request.parent_execution_id,
            target=request.target,
            action=request.action,
            parent_task_id=request.parent_task_id,
            status="created",
            wait_timeout=request.wait_timeout,
        )
        bound = self.registry.bind(handle)

        # 远端如果**当场**就给了终态（同步 Agent），照样记 —— 但交给登记处，
        # 不写进返回值：Loop 拿着一个"已终态"的 handle 会撞 D-11（挂起去等一个
        # 不会再变的答案）。这里返回 `bind()` 的原始快照，唤醒扫照样看得到结果。
        if _is_task(task) and _state_of(task) in TERMINAL_A2A_STATES:
            self._record(bound.child_run_id, task)
        return bound

    # ------------------------------------------------------------ settle
    def poll(self, child_run_id: str) -> ChildRunHandle:
        """取一次远端状态。终态才落终态（A-1），否则原样返回。"""
        handle = self._must(child_run_id)
        if handle.is_finished:
            return handle
        task = self._client_for(handle.target).get_task(child_run_id)
        state = _state_of(task)
        if state in TERMINAL_A2A_STATES:
            return self._record(child_run_id, task)
        return handle

    # ------------------------------------------------------------ cancel
    def cancel_child(
        self, child_run_id: str, *, reason: str, by: str = "system"
    ) -> ChildRunHandle:
        """B-9：叫停一条我派出去的远端子任务。

        远端**自己**报终态才落终态（D-14）。`tasks/cancel` 只回一个
        "还在处理取消"的 working 时，这里不写终态 —— 后面的 `poll` 会接住。
        """
        if not reason:
            raise InvariantViolation("B-8: cancel_child requires a non-empty reason")
        handle = self._must(child_run_id)
        if handle.is_finished:
            return handle
        task = self._client_for(handle.target).cancel_task(child_run_id)
        state = _state_of(task)
        if state in TERMINAL_A2A_STATES:
            return self._record(child_run_id, task)
        return handle

    # ------------------------------------------------------------ 内部
    def _client_for(self, target: str) -> A2AClient:
        client = self.clients.get(target)
        if client is None:
            raise ChildRunUnavailable(
                f"no A2A client for target {target!r}; configured targets: "
                f"{sorted(self.clients)}"
            )
        return client

    def _must(self, child_run_id: str) -> ChildRunHandle:
        handle = self.registry.for_child(child_run_id)
        if handle is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered; "
                f"a result can only be recorded for a child run that was bound"
            )
        return handle

    def _record(self, child_run_id: str, task: Mapping[str, Any]) -> ChildRunHandle:
        state = _state_of(task)
        status = _A2A_TO_AGENTOS_STATUS[state]
        result = {
            "state": state.value,
            "text": _parts_text(task),
            "artifacts": list(task.get("artifacts") or ()),
            "reason": _status_message(task),
        }
        return self.registry.mark_finished(child_run_id, status, result)


# ---------------------------------------------------------------------------
# 形状判定
# ---------------------------------------------------------------------------


def _as_mapping(payload: Any, method: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvariantViolation(f"A2A {method} returned a non-object: {payload!r}")
    return payload


def _is_task(response: Mapping[str, Any]) -> bool:
    """A2A 的 `message/send` 要么回 Task，要么回 Message。"""
    return "status" in response


def _as_task_or_message(response: Mapping[str, Any]) -> Mapping[str, Any]:
    if _is_task(response) or "parts" in response:
        return response
    raise InvariantViolation(
        f"A2A message/send returned neither a Task (no 'status') nor a Message "
        f"(no 'parts'): {sorted(response)}"
    )


def _task_id(response: Mapping[str, Any]) -> str:
    return str(response.get("id") or "") if _is_task(response) else ""


def _state_of(task: Mapping[str, Any]) -> A2ATaskState:
    if not _is_task(task):
        # 一个 Message 就是一次"已经答完"的同步回应。
        return A2ATaskState.COMPLETED
    status = task.get("status")
    if not isinstance(status, Mapping):
        raise InvariantViolation(f"A2A task has no status object: {task!r}")
    return parse_state(status.get("state"))


def _status_message(task: Mapping[str, Any]) -> str:
    status = task.get("status")
    if not isinstance(status, Mapping):
        return ""
    message = status.get("message")
    if isinstance(message, Mapping):
        text = _parts_text(message)
        if text:
            return text
    return str(status.get("state") or "")


def _parts_text(obj: Mapping[str, Any]) -> str:
    chunks: list[str] = []

    def _walk_parts(parts: Any) -> None:
        if not isinstance(parts, (list, tuple)):
            return
        for part in parts:
            if isinstance(part, Mapping) and part.get("kind") == "text":
                chunks.append(str(part.get("text") or ""))

    _walk_parts(obj.get("parts"))
    for artifact in obj.get("artifacts") or ():
        if isinstance(artifact, Mapping):
            _walk_parts(artifact.get("parts"))
    return "\n".join(c for c in chunks if c)


__all__ = [
    "AGENT_CARD_PATH",
    "TERMINAL_A2A_STATES",
    "A2AClient",
    "A2AChildRunSpawner",
    "A2ATaskState",
    "AgentCard",
    "AgentCardTransport",
    "HttpCardTransport",
    "InMemoryCardTransport",
    "parse_state",
]
