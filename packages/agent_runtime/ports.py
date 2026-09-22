"""Runtime 层的端口（阶段 8）。

Runtime **驱动**循环，但它不替 Intelligence 做决定，也不替 Kernel 管生命周期：

    Intelligence  决定（Planner / DecisionEngine）
    Runtime       驱动（AgentLoop：拿决定 → 造 Task → 交棒 → 收 Observation）
    Kernel        执行（Execution / Attempt / Lease）

这里只放 Runtime 需要而别处没有的接口。**不重复定义** Kernel 或 Domain 已有的东西。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from packages.agent_domain.intelligence.decision import Decision
from packages.agent_domain.intelligence.plan import Plan
from packages.agent_domain.intelligence.state import State


class ProgressBearing(Protocol):
    """R-7：**有状态的 Intelligence 实现必须能自述进度**。

    ------------------------------------------------------------------
    为什么需要这个协议

    一个 Planner / DecisionEngine 可以完全从 `state` 推出下一步（无状态），
    也可以自己记着"我走到第几步了"（有状态）。两种写法都合法 ——
    但只有**前者**天然扛得住一次恢复。

    Runtime 侧 `restore()` 只恢复**它自己的**内存状态
    （`steps` / `consecutive_denials` / `steps_of_run` / `spent` / `trace`）。
    它注入的那个引擎若自己记着进度，那份进度**不在快照里**，
    于是恢复后引擎从"第 1 次"重新开始 —— 同一个 Run、同一份 State，
    两个引擎给出两个答案，而系统没有任何东西会发现这件事。

    探针（probe86.py）实测最朴素的后果：

        正常路径    第 1 次 → LLM_CALL   第 2 次 → FINISH
        恢复之后    新引擎第 1 次 → LLM_CALL   ← 又调了一次模型

    调模型是要花钱、要在外部世界留痕的。**静默重来一次不等于没发生。**

    ------------------------------------------------------------------
    怎么用

    有状态的实现实现 `progress()`，返回一个能反映"我走到哪了"的值
    （字符串 / 数字 / 元组都行，只要**同进度 ⟹ 同值**）。
    无状态的实现**什么都不用做** —— 本协议是可选的，探测用
    `getattr(engine, "progress", None)`（与 `cancel_child` / `attempts`
    同一套风格）。

    恢复时 Runtime 拿快照里存的那个值跟当前引擎自述的值比。
    对不上时它做**两件事**，顺序不能反：

        1. 先问引擎："你能把自己接到这个进度上吗？"（`resume()`）
           能 ⟹ 接上，恢复照常走 —— **这是常态**，不是例外。
           不能 / 没实现 `resume` ⟹ 走进第 2 步。
        2. 点名拒绝恢复。

    ⚠️ **为什么必须先试 `resume()` 而不是直接拒绝**

    "挂起 → 进程重启 → 恢复" 是企业级平台里最常规的一条路径：
    一条 Run 挂起等人审批（可能要等几小时），期间 Pod 被调度、被重启、
    被滚动更新 —— 它回来时必须能接上。

    直接拒绝会把这条常规路径变成不可用，那是**用一个正确的判据
    弄坏一个正常的系统**。判据要挡的是"静默重来"，不是"恢复"本身。

    所以协议的正解是成对的：

        progress()  我在哪   ← 捕获时问
        resume(p)   接到这   ← 恢复时问

    只实现 `progress()` 而不实现 `resume()`，含义是
    "我能自述进度，但接不回去" —— 那么一旦对不上就只能拒绝。
    那是一个合法的选择（比如进度里含不可重建的东西），
    但它必须在**恢复的那一刻**才生效，而不是让整个恢复路径不可用。
    """

    def progress(self) -> object: ...

    def resume(self, progress: object) -> None: ...


class Planner(Protocol):
    """I-2：产出 Plan（计划的**节点**是静态的；运行时的 Step 是它的实例）。

    有状态的实现可另实现 `ProgressBearing`（见上）—— 那不是必需的。
    """

    def plan(self, state: State) -> Plan: ...


class DecisionEngine(Protocol):
    """产出 Decision。

    ⚠️ Decision **不可执行**（I-4）：它只有 `selected_action` 和 `confidence_signal`，
    没有 `execute()` —— 副作用必须经过 ActionResolver → TaskFactory → Kernel。

    有状态的实现可另实现 `ProgressBearing`（见上）—— 那不是必需的。
    """

    def decide(self, state: State) -> Decision: ...


class LLMClient(Protocol):
    """最小 LLM 接口（真实实现接 OpenAI / Anthropic / 内部网关）。"""

    def complete(self, prompt: str, **kwargs: Any) -> Mapping[str, Any]: ...


class Tool(Protocol):
    """一个可被调用的工具。"""

    name: str

    def call(self, args: Mapping[str, Any]) -> Mapping[str, Any]: ...
