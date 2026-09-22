"""AgentOS · agent_context（M17：Context / Memory）

基线 §21（Memory / Knowledge / Context / Artifact）与 §22（Context Engineering）的落点。

```text
Memory（记住的）  ──► MemoryManager.recall() ──┐
Knowledge（外部资料）──► RetrievalPipeline ────┤
                                              ▼
                                       ContextAssembler  （排序 + 取舍 + 预算）
                                              ▼
                                       Context → Model
                                              ▼
                                       ContextSnapshot（审计）
```

**不变量 C-1 ~ C-12**

| # | 内容 |
|---|---|
| C-1 | `Context ≠ Memory ≠ Knowledge ≠ Artifact`：四者是不同类型，转换必须显式（`MemoryManager.recall()` / `RetrievalPipeline.to_context_items()` 都返回 `ContextItem`） |
| C-2 | Context 是**每次模型调用**的工作台，不是 Run 级状态；一次调用一份 Snapshot |
| C-3 | Token Budget 是硬约束：装不下就丢；**pinned 装不下则直接失败**（`ContextBudgetError`），不静默降级 |
| C-4 | **静默截断是 bug**：每一条被丢的内容都要留 `DroppedItem` + 原因；分配结果必须确定 |
| C-5 | `ContextSnapshot ≠ Checkpoint`：前者是"模型当时看到了什么"，后者是"从哪里恢复"；字段互不重叠（可断言） |
| C-6 | Artifact 只以 `reference` 进 Context，不内联 |
| C-7 | Memory 分层（WORKING / EPISODIC / SEMANTIC / PROCEDURAL）；**Qdrant ≠ Truth**，索引是派生的、可重建 |
| C-8 | Memory 写入可溯源；EPISODIC 必须带 `source_run_id` |
| C-9 | Retrieval **必须**过 Permission Filter（`RetrievalPipeline.permission` 构造期必填）；权限不能外包给 Retriever |
| C-10 | Knowledge 进 Context 必须带 Citation（`Chunk.citation` / `ContextItem.reference` 必填） |
| C-11 | **组装归 Runtime，准入归 Harness**：§23 的 `ContextManager` 指后者，不是让 Harness 拼字符串 |
| C-12 | **排序与取舍是两个维度**：顺序由 `SOURCE_ORDER` 决定（语义），priority 只决定先丢谁 |

**边界**：本包不 import `execution_kernel` 的生命周期对象，也不认识 Goal / Decision ——
它只回答"这次喂给模型什么"。
"""
from __future__ import annotations

from .assembler import ContextAssembler, ContextBuild, ContextRequest  # noqa: F401
from .budget import (  # noqa: F401
    ContextBudgetError,
    ContextPlan,
    DroppedItem,
    TokenBudget,
    allocate,
)
from .items import (  # noqa: F401
    SOURCE_ORDER,
    ContextItem,
    ContextSource,
    knowledge_chunk,
    message,
    system,
    tool_contract,
)
from .memory import (  # noqa: F401
    InMemoryMemoryStore,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    MemoryStore,
)
from .retrieval import (  # noqa: F401
    AllowAll,
    Chunk,
    DenyAll,
    PermissionFilter,
    RetrievalPipeline,
    RetrievalQuery,
    RetrievalResult,
    Retriever,
    TenantFilter,
)
from .snapshot import (  # noqa: F401
    ContextSnapshot,
    ContextSnapshotStore,
    InMemoryContextSnapshotStore,
    build_snapshot,
)
from .tokens import HeuristicTokenizer, Tokenizer  # noqa: F401

__all__ = [
    "AllowAll",
    "Chunk",
    "ContextAssembler",
    "ContextBudgetError",
    "ContextBuild",
    "ContextItem",
    "ContextPlan",
    "ContextRequest",
    "ContextSnapshot",
    "ContextSnapshotStore",
    "ContextSource",
    "DenyAll",
    "DroppedItem",
    "HeuristicTokenizer",
    "InMemoryContextSnapshotStore",
    "InMemoryMemoryStore",
    "MemoryLayer",
    "MemoryManager",
    "MemoryRecord",
    "MemoryStore",
    "PermissionFilter",
    "RetrievalPipeline",
    "RetrievalQuery",
    "RetrievalResult",
    "Retriever",
    "SOURCE_ORDER",
    "TenantFilter",
    "TokenBudget",
    "Tokenizer",
    "allocate",
    "build_snapshot",
    "knowledge_chunk",
    "message",
    "system",
    "tool_contract",
]
