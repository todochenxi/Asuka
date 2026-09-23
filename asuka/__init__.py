"""Asuka —— 面向技术文档知识库的 Agent Evaluation & Runtime（MVP）。

    K8s / Redis / Python / FastAPI / Linux / PyTorch 官方文档
        ↓ 文档处理（抓取 → 归一化 → 结构化 → 切分）
    Knowledge Base（带 Citation）
        ↓ 生成
    Task Dataset（question / reference_answer / source_document / difficulty）
        ↓
    Agent（Retrieval + Answer）
        ↓
    Runtime → Trace
        ↓
    Evaluation（retrieval / generation / agent 三层指标）
        ↓
    Evaluation Report

--------------------------------------------------------------------------
它和 AgentOS 的关系

**不重复造 Runtime。** AgentOS 已经有一整套可靠 / 可观测 / 可审计的运行时，
其中两块正好是 RAG 评测框架的空缺：

    · `packages/agent_context`     —— Chunk / Citation（C-10）/ RetrievalPipeline / 权限过滤
    · `packages/agent_evaluation`  —— 行为轨迹评估（RAGAS / TruLens / DeepEval **都不做这个**）

社区那三个 RAG 评测框架（RAGAS / TruLens / DeepEval）**全都只评"检索 + 生成"**，
没有一个做 Agent 轨迹与工具调用评估；而 AgentOS 的评估**只评轨迹、不评答案**
（`agent_evaluation/__init__.py` 原话："账本里没有答案文本"）。

⇒ 两边各缺一半。Asuka 补的是**它们都缺的那两块**：技术文档语料与任务集、
以及答案级指标（faithfulness / answer_relevancy / context_precision / context_recall）。

--------------------------------------------------------------------------
依赖纪律（**分层，不是一刀切**）

**核心层零第三方**（与 `packages/` 同一纪律，只用标准库）：

    textutil.py      文本原语（原子块 / 保护层 / 装箱）
    corpus.py        抓取（urllib）→ 归一化 → 结构化 → 切分 → manifest
    embedding.py     embedder 协议 + 身份自述；API 走 urllib
    vectorstore.py   Qdrant 的一层薄封装（qdrant_client **惰性 import**）

**加速层是可选的，且每一层都有兜底**（没有它就退化，不是崩）：

    切分    langchain-text-splitters（社区标准）  →  兜底：`splitters._split_stdlib`
    embedding  本地 bge-m3（sentence-transformers）  →  兜底：无（**不许静默降级**，
                                                          见 `embedding.build_embedder`）
    向量库  Qdrant（qdrant-client）              →  兜底：自实现的 BM25 词法检索

**为什么 BM25 词法检索不是"妥协"而是"对照组"**

这个项目的目的是**比较**：同一份语料、同一个任务集，换检索器 / 换模型会怎样。
没有词法基线，"向量检索得了 0.72" 这个数字**无法解释** ——
它比随机好多少？比一个三十年前就成熟的算法好多少？
⇒ 词法基线是**评测平台必须有的那个分母**，不是省事的替代品。

--------------------------------------------------------------------------
两条硬规矩（都来自实证，不是洁癖）

1. **citation 必须说真话。** redis.io 有 14/20 份文档带页面模板样板块
   `## Code Examples Legend`，它把文档**无标题的概述**拖进自己的标题路径，
   于是 citation 变成 `Redis · EXPIRE · Code Examples Legend`，
   而内容是 "Set a timeout on `key`..."。Citation 指标就是拿 citation 判分的 ——
   citation 说谎，指标就废了。⇒ 归一化必须在**切分之前**，且移除量要记账。

2. **embedding 不许静默换人。** 向量库里躺着 1024 维的 bge-m3 向量，
   拿另一个 1024 维模型来查 —— 维度一样、语义不同、结果全是垃圾、**一声不响**。
   ⇒ 每个 embedder 自述 `name@dim`，向量库把它写进 manifest，查询时对不上就**拒绝**。

⚠️ 上面两条的"实证"都在 `tests/unit/test_asuka_corpus_contract.py` 里钉着。
"""
from __future__ import annotations

__all__ = [
    "answers",
    "compare",
    "context",
    "corpus",
    "dataset",
    "embedding",
    "evaluate",
    "index",
    "kb",
    "regression",
    "splitters",
    "textutil",
    "trace",
    "vectorstore",
]
