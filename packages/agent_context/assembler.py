"""ContextAssembler（M17，基线 §22）。

```text
ContextAssembler → ContextOptimizer → TokenBudget → Model
```

C-2  Context 是**每次模型调用**的工作台，不是 Run 级状态。
     一次 LLM 调用一份 Context、一份 Snapshot ——
     把它做成 Run 级单例的话，第 5 次调用的 Snapshot 会覆盖第 1 次的，
     于是"它第一次为什么这么答"这个问题永远查不到。

C-12 **排序与取舍是两个独立维度**：
     输出顺序由 `SOURCE_ORDER` 决定（系统指令永远在最前），
     `priority` 只在装不下时决定先丢谁。
     把两者混为一谈（按 priority 排序输出）是最常见的 Context 工程错误。

C-11 **组装归 Runtime，准入归 Harness**：
     把内容拼进 Context 是执行的一部分（Runtime），
     但"这些内容能不能进"（权限 / 敏感信息 / 越权检索）是 Harness 的事。
     §23 里 `AgentHarness ├── ContextManager` 指的是**后者** ——
     不是让 Harness 去拼字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from packages.execution_kernel.ports import Clock

from .budget import ContextPlan, TokenBudget, allocate
from .items import (
    SOURCE_ORDER,
    ContextItem,
    ContextSource,
)
from .snapshot import (
    ContextSnapshot,
    ContextSnapshotStore,
    InMemoryContextSnapshotStore,
    build_snapshot,
)
from .tokens import HeuristicTokenizer, Tokenizer


@dataclass(frozen=True)
class ContextRequest:
    """一次模型调用需要的全部输入。"""

    run_id: str
    execution_id: str = ""
    model_id: str = ""
    system: str = ""
    messages: tuple[tuple[str, str], ...] = ()     # (role, text)
    memory: tuple[ContextItem, ...] = ()
    knowledge: tuple[ContextItem, ...] = ()
    tools: tuple[ContextItem, ...] = ()
    skills: tuple[ContextItem, ...] = ()
    runtime_state: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("ContextRequest.run_id is required")


@dataclass(frozen=True)
class ContextBuild:
    """组装的结果：要喂什么 + 丢了什么 + 审计凭证。"""

    items: tuple[ContextItem, ...]
    plan: ContextPlan
    snapshot: ContextSnapshot

    @property
    def total_tokens(self) -> int:
        return self.plan.total_tokens

    def render(self) -> tuple[str, ...]:
        """按 `SOURCE_ORDER` 渲染成有序文本块（顺序 = 语义，见 C-12）。"""
        buckets: dict[str, list[str]] = {}
        for item in self.items:
            buckets.setdefault(item.source.value, []).append(item.text)
        out: list[str] = []
        for source in SOURCE_ORDER:
            out.extend(buckets.get(source.value, []))
        # 兜底：SOURCE_ORDER 里没有的来源（新增来源时不会静默丢内容）
        known = {s.value for s in SOURCE_ORDER}
        for value, texts in buckets.items():
            if value not in known:
                out.extend(texts)
        return tuple(out)


@dataclass
class ContextAssembler:
    """把各路来源拼成一个受预算约束的 Context。"""

    tokenizer: Tokenizer = field(default_factory=HeuristicTokenizer)
    budget: TokenBudget = field(default_factory=lambda: TokenBudget(total=8192))
    snapshots: ContextSnapshotStore = field(default_factory=InMemoryContextSnapshotStore)
    clock: Clock | None = None
    #: §22 的 `MemoryAssembler`：把 Memory 变成 ContextItem（C-1 的转换口）。
    #: 通常就是 `MemoryManager.recall()` 外面包一层，把 subject 从 request 里取出来。
    memory_provider: Callable[[ContextRequest], Sequence[ContextItem]] | None = None
    #: §22 的 `RetrievalAssembler`：Knowledge → ContextItem。
    #: 通常是 `RetrievalPipeline.run()` + `to_context_items()`（C-9 / C-10 在这里生效）。
    knowledge_provider: Callable[[ContextRequest], Sequence[ContextItem]] | None = None

    def build(self, request: ContextRequest) -> ContextBuild:
        items = self._collect(request)
        plan = allocate(items, self.budget, tokenizer=self.tokenizer)
        ordered = self._order(plan.kept)
        snapshot = build_snapshot(
            run_id=request.run_id,
            plan=ContextPlan(kept=ordered, dropped=plan.dropped, total_tokens=plan.total_tokens),
            clock=self.clock or _SystemClock(),
            model_id=request.model_id,
            execution_id=request.execution_id,
        )
        self.snapshots.save(snapshot)
        return ContextBuild(items=ordered, plan=plan, snapshot=snapshot)

    # ------------------------------------------------------------ 内部
    def _collect(self, request: ContextRequest) -> list[ContextItem]:
        from .items import message, system, tool_contract

        collected: list[ContextItem] = []
        if request.system:
            collected.append(system(request.system))
        for index, (role, text) in enumerate(request.messages):
            collected.append(message(role, text, index=index))
        collected.extend(request.memory)
        if self.memory_provider is not None:
            collected.extend(self.memory_provider(request))
        collected.extend(request.knowledge)
        if self.knowledge_provider is not None:
            collected.extend(self.knowledge_provider(request))
        if request.runtime_state:
            collected.append(
                ContextItem(
                    source=ContextSource.RUNTIME_STATE,
                    key="runtime_state",
                    text=_render_mapping(request.runtime_state),
                    priority=60,
                )
            )
        collected.extend(request.skills)
        collected.extend(request.tools)
        # 工具契约由调用方给；没给的话至少要有一条，否则模型无从发起 Tool Call
        if not request.tools:
            collected.append(tool_contract("__none__", "no tools available"))
        return collected

    def _order(self, kept: Sequence[ContextItem]) -> tuple[ContextItem, ...]:
        rank = {s.value: i for i, s in enumerate(SOURCE_ORDER)}
        return tuple(
            sorted(kept, key=lambda i: (rank.get(i.source.value, len(rank)), i.key))
        )


def _render_mapping(data: Mapping[str, object]) -> str:
    return "\n".join(f"{k}={v}" for k, v in data.items())


class _SystemClock:
    def now(self):
        from datetime import datetime

        return datetime.now()
