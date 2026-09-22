# AgentOS — 架构评审纪要 & 版本变更记录（v2.1，非事实源）

> ⚠️ **本文档已降级为「评审纪要 + 变更记录」，不再作为架构事实源。**
>
> **架构基线 = `AgentOS_企业级Agent平台架构_v2.1.16_最终冻结版.md`（v2.1.16）**
> 概念、边界、状态集合、契约一律以基线为准；本文只在冲突时提供「为什么这么改」的来龙去脉。
>
> 配套：`M15-核心领域模型.md`（代码级领域模型与不变量，已同步基线 v2.1.16）。

> 整理自《项目拆解计划群.md》对话记录 · 经三轮架构评审完成**收敛**
> 项目定位：**Enterprise Agent Harness & Runtime Platform**
> 状态：**架构冻结**。不再增加 M16+ 概念，下一步直接进入 M15 Kernel Implementation。
> 评分轨迹：v1 → v2.0（收敛边界）→ v2.1（闭合概念）→ v2.1.1（补定义）→ v2.1.2（M15 落地回写）→ v2.1.3（Business Domain 回写：钉死「谁有权说一个 Run 结束了」）→ v2.1.4（§44 闭环回写：钉死「一个 Run 凭什么算跑完了」）→ v2.1.5（M17 回写：钉死「模型的每个回答凭什么这么答」）→ v2.1.6（M18 回写：钉死「系统怎么被唤起，人怎么把话递回去」）→ v2.1.7（M19 回写：钉死「人递回去的那句话，凭什么第二天还作数」）→ v2.1.8（M20 回写：钉死「第二天，谁来接这句话」）→ v2.1.9（M10 回写：钉死「它在这个世界上动过的东西，怎么收回去」）→ v2.1.10（M21 回写：钉死「谁来收」—— 库不会自己跑起来）→ v2.1.11（M22 回写：钉死「谁来干」—— 进程类仍然不是进程）→ v2.1.12（M23 回写：钉死「干的到底是不是该干的那一件」—— 分派有两个维度，系统只表达了一个）→ v2.1.13（M24 回写：钉死「请求从哪儿进来，以及它凭什么只进来一次」—— 门上写着不许用 Redis 钥匙，而钥匙挂了六轮）→ v2.1.14（M25 回写：钉死「谁有权把活派出去，以及派出去的那一份凭什么只有一份」—— 两个洞其实是一格，而 CHILD_AGENT 冻结了六个版本没人设置过）→ v2.1.15（M26 回写：钉死「派出去的那一份，第二天还认不认得」—— 唯一键在这里是安慰剂，而撤销声明从来没有序列化通道）→ **v2.1.16（M27 回写：钉死「凭什么说它被验过」—— 629 条全绿里没有一条跑在真 PostgreSQL 上）**

---

## 0 · 冻结声明与评分定位

评审判断：架构思想 8.5 / 领域模型 8 / 工程边界 7.5 / 直接开写代码成熟度 6.5。

核心诊断：**不是"少了什么技术"，而是 Kernel 的几个核心定义还没钉死。**

因此 v2.0 只做一件事 —— **收敛**。冻结后不再往上堆概念。

### 一句话总纲（不变）

> 我的 Agent 平台核心不是某个 LLM，而是一个**统一的 Execution Kernel**。
> Harness 控制 Agent，Runtime 驱动 Agent，Kernel 执行 Task。
> Workflow、Skill、Tool、Multi-Agent、Evaluation 全部复用这一套 Kernel。

原则：**先有执行模型，再选择基础设施**（Architecture with Responsibility）。

---

## 1 · 顶层收敛架构（最终版）

```
                            AgentOS
                               │
              ┌────────────────┼────────────────┐
              ↓                ↓                ↓
       Intelligence        Harness          Governance
              │                │                │
            Goal            Context           Policy
            State           Memory             IAM
            Decision        Cost               OPA
            Action          HITL               Vault
            Plan            Guardrail
              │                │
              └────────────────┘
                               ↓
                        Agent Runtime
                               │
                        Agent Loop
                    （Decision / Planning）
                               │
                            Action
                               │
                             Task
                               ↓
                    ┌────────────────────┐
                    │  Execution Kernel  │
                    │                    │
                    │  State Machine     │
                    │  Scheduler         │
                    │  Lease             │
                    │  Attempt           │
                    │  Checkpoint        │
                    │  Retry             │
                    │  Recovery          │
                    │  Cancellation      │
                    │  Idempotency       │
                    └─────────┬──────────┘
                              ↓
                        Observation
                              ↓
                            State
                              ↓
                          Decision  ──→ 回到 Runtime
```

### 三层职责一句话钉死

| 层 | 动词 | 职责 |
| --- | --- | --- |
| **Agent Harness** | **控制** Agent | Context / Policy / Memory / Guardrail / Cost / HITL / Evaluation |
| **Agent Runtime** | **驱动** Agent | Agent Loop / Planner / Decision Engine / Action Resolver / Task Factory / Observation Processor |
| **Execution Kernel** | **执行** Task | State Machine / Scheduler / Lease / Attempt / Checkpoint / Retry / Recovery / Cancellation / Idempotency |

> ⚠️ **关键修正（v1 → v2）**：
> - `Scheduler` / `State Machine` / `Retry` / `Cancellation` **从 Agent Runtime 移除**，它们属于 Kernel
> - **State Machine 是 Kernel 能力，不是 Agent 能力** —— Workflow / Agent / Tool / Evaluation 都可能需要它
> - Runtime 只负责"把 Agent 的想法变成 Task"，不负责"怎么可靠地把 Task 跑完"

---

## 2 · 执行对象模型（P0，已钉死）

### 2.1 五层关系

```
AgentRun
    │
    │ contains
    ↓
   Step
    │
    │ produces  (一对多)
    ↓
   Task ── Task ── Task
    │
    │ scheduled as
    ↓
 Execution
    │
    │ retried as
    ↓
  Attempt
```

### 2.2 逐层定义

| 对象 | 定义 | 例子 |
| --- | --- | --- |
| **AgentRun** | 业务级 Agent 执行实例 —— 用户让某个 Agent 完成一次任务 | Run #10086："帮我分析 Q3 财务风险" |
| **Step** | Agent / Workflow 的**逻辑执行节点**，属于 Execution Graph | Step：`research_company` |
| **Task** | **可调度的工作单元**。**Scheduler 只认识 Task** | `search_official_site` / `search_news` / `query_db` / `read_kb` |
| **Execution** | Task 在 Execution Kernel 中被管理的**生命周期实体**，负责状态 / Lease / Checkpoint / Cancellation / Retry / Recovery | Execution #100 |
| **Attempt** | Execution 的**一次具体尝试** | Attempt #1 → timeout；Attempt #2 → success |

尖锐回答「**Step 和 Task 有什么区别？**」：
> **Step 是逻辑节点（做什么），Task 是调度单元（谁去跑）。**
> Step 描述"这一步要调研这家公司"，Runtime 为它生成**一个或多个** Task；Task 进入 Kernel 才获得 Execution 生命周期。
> Step 属于业务语义层，Task / Execution / Attempt 属于 Kernel 层。

### 2.3 Step → Task 是**一对多**（P1 修正）

> ❌ v2.0 写"一个 Step produces 一个可调度 Task" —— 对第一版好理解，但**把架构限制死了**。

一个 Step 可以产出多个 Task：

```
Step: research_company
   ↓
Planner / Fan-out
   ↓
Task 1: search_official_site
Task 2: search_news
Task 3: query_database
Task 4: read_knowledge_base
   ↓
Fan-in → 汇总 → 下一个 Step
```

这样才能自然支持：**Parallel / Planner / Multi-Agent / Workflow / Fan-out / Fan-in**。

> 准确表述：**Step 是逻辑执行节点，Task 是由 Runtime 为该节点生成的一个或多个可调度工作单元。**

### 2.4 Execution Domain 全景（Business vs Kernel）

```
Business                              Kernel
─────────────────────────────────    ──────────────────────────
AgentRun                              Task
WorkflowRun                             │
SkillRun                                ↓
EvaluationRun                        Execution
     │                                  └── Attempt
     │ 内部产生可执行工作
     └──────────────────────────────→ Checkpoint / Lease / Cancellation
```

> ⚠️ **P0 修正（v2.0 → v2.1）**：**删除 `TaskExecution` / `ToolExecution` 作为独立领域对象。**
> v2.0 里"Kernel Execution = TaskExecution / ToolExecution"会让面试官追问
> "Execution 到底是抽象概念还是具体实体？""ToolExecution 是 Task 还是 Execution？"——**这是新的套娃**。

**唯一正确的 Kernel 执行链**：

```
ToolCall
   ↓
Action
   ↓
Task (task_type = TOOL)
   ↓
Execution
   ↓
Attempt
```

> 一个 Tool Call **产生 Task**，不直接产生 ToolExecution。Kernel 里只有 `Task / Execution / Attempt` 三个执行对象。

**AgentRun 与 Kernel Execution 的关系（P0 明确）**：

```
AgentRun                      ← 业务语义上的 Run
   │
   ├── Step
   │    └── Task
   │         └── Execution
   │              └── Attempt
   │
   └── ...
```

> **AgentRun 是业务语义上的 Run，不等价于 Kernel Execution；
> 但 AgentRun 内部产生的可执行工作（Task）由 Kernel 管理。**
>
> 否则将来做 WorkflowRun / SkillRun / EvaluationRun 时，会再次出现"到底谁才是 Execution"的问题。

### 2.5 TaskFactory：Runtime 与 Kernel 的交接点

```
Action  ──(Action Resolver)──→  Action 实例
Action  ──(Task Factory)─────→  Task / Tasks   ← Runtime 的最后一步（可一对多）
Task    ──(提交)──────────────→  Execution Kernel
```

Runtime 产出 Task 即交棒，后续生命周期全归 Kernel。

---

## 3 · Agent Runtime（收敛后）

```
Agent Runtime
├── Agent Loop              主循环：观察 → 决策 → 行动
├── Planner                 规划（Plan = Execution Graph）
├── Decision Engine         决策：产生 Decision（不直接执行）
├── Action Resolver         把 Decision 解析成具体 Action
├── Task Factory            把 Action 转成可调度 Task
└── Observation Processor   把执行结果加工成 Observation，更新 State
```

**不再包含**：~~Scheduler~~ ~~State Machine~~ ~~Retry~~ ~~Cancellation~~ ~~Run Manager~~

---

## 4 · Execution Kernel

### 4.1 九大能力（附边界一句话）

面试时这六个最容易被混成一个"失败重试系统"，必须能一句话说清：

| 能力 | 一句话边界 | 补充说明 |
| --- | --- | --- |
| **Attempt** | **我试了一次** | 失败开新 Attempt，保留失败证据。Retry ≠ 状态回退 |
| **Retry** | **我要不要再试** | Exponential Backoff + RetryPolicy + Error Classification（可重试 / 不可重试） |
| **Recovery** | **系统怎么从异常状态恢复** | 见 4.3，与 Agent Recovery 严格区分 |
| **Checkpoint** | **从哪里继续** | 崩溃从最近点恢复。见 4.4 恢复边界 |
| **Lease** | **谁现在拥有执行权** | 租约 + Heartbeat；挂掉则重新可见。禁止 RUNNING → QUEUED |
| **Idempotency** | **重试后会不会造成重复副作用** | At-Least-Once + 幂等键。**Retry ≠ Safe** |
| **State Machine** | — | 通用机制，见 4.2 |
| **Scheduler** | — | 只认识 Task；Atomic Claim；Runnable Queue；Priority + Aging 防饥饿；Tenant 隔离 |
| **Cancellation** | — | 见 4.5 |

补充：跨服务用 **Saga / Compensation**；写侧走 Kernel、读侧构建 **Read Model**（CQRS）。

### 4.2 StateMachine 是**通用机制**，不是 AgentRun 专属（P1 修正）

> v2.0 说"State Machine 是 Kernel 能力，不是 Agent 能力"思想对，
> 但后面展开的全是 AgentRun 状态机 —— 会被追问"到底是 AgentRun 的状态机还是通用状态机？"

```
StateMachine（通用 Kernel Mechanism）
├── AgentRunStateMachine
├── TaskStateMachine
├── ExecutionStateMachine
├── AttemptStateMachine
└── WorkflowRunStateMachine（未来）
```

概念上必须区分；**第一版只需实现 `ExecutionStateMachine` + `AttemptStateMachine`**，AgentRun 状态由它们派生。

### 4.3 Recovery 分两层（P1 修正，消除 Harness/Kernel 撞车）

| | **Execution Recovery**（Kernel） | **Agent Recovery**（Harness） |
| --- | --- | --- |
| 解决什么 | Worker crash / Lease expired / Attempt failed / Execution stale / Checkpoint resume | Tool failed / Need replan / Change model / Ask human / Change strategy |
| 一句话 | **怎么把 Execution 救回来** | **Agent 失败以后下一步怎么办** |
| 层级 | 基础设施级 | 业务策略级 |
| 命名 | `Recovery`（Kernel） | 建议改名 **`AgentRecoveryStrategy`**，避免两个 RecoveryManager 撞车 |

### 4.4 Checkpoint 的恢复边界（明确，防止退化成大 JSON）

> Checkpoint ≠ ContextSnapshot 已经明确，但还需明确 **Checkpoint 到底装什么**，
> 否则实现时很容易写成 `checkpoint = messages`，又变回"大 Context 对象"。

```
Checkpoint
├── current_step          执行到哪一步
├── completed_tasks       已完成的任务
├── execution_state       执行状态
├── context_snapshot_id   ← 只存 ID 引用，不内嵌
├── variables             变量
├── artifacts             产物引用
└── recovery_metadata     恢复所需元信息
```

- **ContextSnapshot** = 模型上下文是什么
- **Checkpoint** = 执行到哪里了 + 恢复需要什么（**引用** ContextSnapshot，不内嵌）

### 4.5 Cancellation 归属（P1 明确）

```
User / Policy / System
        ↓
Cancellation Request          ← Harness 可以发起请求
        ↓
Kernel Cancellation           ← 生命周期管理属于 Kernel
        ↓
Worker CancellationToken
        ↓
Execution terminated
```

> **Harness 可以发起取消请求，但 Cancellation 的生命周期管理属于 Kernel。**
> 不存在"Harness 的 Cancellation 和 Kernel 的 Cancellation 是两个东西"的问题。

### 4.6 SUSPENDED 的 Wake-up 机制（P1 补齐，否则闭环不完整）

v2.0 只写了 `RUNNING ⇄ SUSPENDED`，但没回答"**谁负责唤醒**"。补齐：

```
Suspended Execution
        ↓
Wait Condition  (HUMAN_APPROVAL / CHILD_AGENT / TIMER / EXTERNAL_EVENT)
        ↓
Event / Timer 到达
        ↓
Wake-up                    ← Kernel 的 Resume 机制
        ↓
Runnable Task              ← 重新变成可调度单元
        ↓
Scheduler
        ↓
继续执行（用 Checkpoint 恢复）
```

> Wake-up 属于 **Kernel**，不是 Harness。这样 Actor + Mailbox、Event 唤醒 Resume 才真正落到执行模型上。

### 4.7 两种执行模型（Actor 降级）

```
Execution Kernel
├── Task Execution        无状态 / 短任务：claim → run → done
└── Stateful Execution    长任务：Actor + Mailbox
      └── Actor + Mailbox
```

> **Actor + Mailbox 不进入第一版核心。** 它只是 Stateful Execution 的一种实现模型，
> 不要把整个 AgentOS 做成 Actor Framework。
> 适用场景：Long-running Agent / Human Approval / External Event / Child Agent / Timer。
>
> 注意：Kernel 里**不再有** `TaskExecution` / `ToolExecution` 领域对象；
> 上图"Task Execution"是**执行模式**的描述，不是领域实体。领域实体只有 `Task / Execution / Attempt`。

Mailbox 消息类型：`UserMessage / ToolResult / ApprovalResult / ChildAgentResult / TimerEvent / ExternalEvent / Cancellation`

---

## 5 · 状态机（AgentRun 视角）

> 本章展示的是 **AgentRunStateMachine**。StateMachine 本身是通用机制（见 4.2），
> 第一版真正实现的是 `ExecutionStateMachine` + `AttemptStateMachine`，AgentRun 状态由它们派生。

### 5.1 七态 + Suspension Reason

```
CREATED ──► QUEUED ──► RUNNING ⇄ SUSPENDED ──► COMPLETED
                          │                  ──► FAILED
                          │                  ──► CANCELLED
                          └──► (retry → 新 Attempt)
```

| 状态 | 含义 |
| --- | --- |
| `CREATED` | 已创建，未入队 |
| `QUEUED` | 已入队，等待调度 |
| `RUNNING` | 正在执行 |
| `SUSPENDED` | 挂起（有原因） |
| `COMPLETED` | 成功完成 |
| `FAILED` | 失败（含 timeout —— timeout 是 Attempt 层的失败原因，映射到 FAILED） |
| `CANCELLED` | 被取消 |

### 5.2 为什么用 SUSPENDED + reason

`WAITING_HUMAN` **不是生命周期状态**，它描述的是"为什么当前 Execution 没有继续执行"—— 属于 **SuspensionReason**。

```json
{ "status": "SUSPENDED", "suspension_reason": "HUMAN_APPROVAL" }
```

```json
{
  "status": "SUSPENDED",
  "suspension_reason": "CHILD_AGENT",
  "suspension_detail": { "child_run_id": "run_10087" }
}
```

| suspension_reason | 场景 |
| --- | --- |
| `HUMAN_APPROVAL` | 人工审批（HITL）—— Checkpoint 后 **释放 Worker**，三小时后 Event 唤醒 Resume |
| `CHILD_AGENT` | 等待子 Agent 返回结果 |
| `TIMER` | 定时唤醒 / 延迟执行 |
| `EXTERNAL_EVENT` | 等待外部系统回调 |

以后新增等待类型**无需增加顶层状态**。

---

## 6 · AgentOS Core Domain Contracts（P1，重命名）

> ⚠️ v1 称"五大核心协议"不严谨：Goal / State / Decision / Action / Observation 是**领域契约**，
> 真正的 Protocol 应是 Execution Protocol / Event Protocol / Tool Protocol / Agent Protocol / Model Protocol。

### 三组 Contract

#### ① Intelligence Contracts

| Contract | 定义 | 字段 / 取值 |
| --- | --- | --- |
| **Goal** | 系统可执行的任务定义（≠ UserRequest，需 Goal Interpreter 转换） | objective / constraints / success_criteria / priority / deadline / budget |
| **State** | Agent 当前知道什么 + 执行到哪里 | goal / current_plan / active_tasks / completed_tasks / observations / variables / constraints / runtime_status |
| **Decision** | Agent 对下一步的判断，**不直接执行**（属 Intelligence，Execution 属 Runtime，必须隔离） | 选出的 Action + Evidence + **`confidence_signal`**（见下） |
| **Action** | 一个具体的意图 | `ToolCall` / `SkillCall` / `AgentDelegation` / `HumanApproval` / `AskUser` / `Finish` |
| **Observation** | Agent 感知世界的统一接口 | 执行结果 → 更新 State |
| **Plan** | 执行图（DAG），非线性的 | nodes / edges / validator 结果 |

#### ② Execution Contracts

`Task` / `Execution` / `Attempt` / `Checkpoint` / `Lease` / `Cancellation`

#### ③ Integration Contracts

`Tool` / `Skill` / `Agent` / `Model` / `Event`

### 核心循环

```
G → D → A → T → E → O → S → D
Goal → Decision → Action → Task → Execution → Observation → State → Decision
```

### ⚠️ Decision 的 Confidence 不要用成概率（重要）

`{"action": "send_email", "confidence": 0.97}` **并不意味着这个决策 97% 正确** —— LLM 给出的 confidence 不是 calibrated probability。

| 建议 | 说明 |
| --- | --- |
| 字段命名 | 用 **`confidence_signal`** 或归入 **`decision_metadata`**，不要叫 `confidence` |
| 禁止用法 | ❌ `confidence > 0.9 → 自动执行` —— 这是危险设计 |
| 正确用法 | 作为**排序 / 人工复核优先级**的信号；是否自动执行由 **Policy / Guardrail / 风险分级** 决定，不由 confidence 决定 |

---

## 7 · Agent Harness（控制 Agent）

```
AgentHarness
├── ContextManager          ContextEngine（Assembler / Optimizer / TokenBudget）
├── MemoryManager           Working / Semantic / Episodic / Procedural
├── PolicyEngine            OPA，Policy as Code
├── GuardrailEngine         输入 / 输出 / Tool 三层护栏
├── CostManager             Token / Model / Run / Tenant 成本与预算
├── HumanLoop               HITL → 发起 Cancellation/Suspend 请求 → SUSPENDED(HUMAN_APPROVAL)
├── AgentRecoveryStrategy   Tool failed / Replan / Change model / Ask human（见 4.3）
└── Checkpoint/Recovery integration   调用 Kernel Checkpoint，不自己实现
```

> **P1 修正（v2.0 → v2.1）**
> - **Evaluator 从 Harness 移除** —— Evaluation 已是独立 Platform Domain（见 §7.5），Harness 只**调用**它
> - **RecoveryManager 改名 `AgentRecoveryStrategy`** —— 避免与 Kernel 的 `Recovery` 撞车（见 4.3）

### Context Engineering

```
ContextEngine
├── ContextAssembler      每种信息一个独立 Assembler
│     ├── SystemInstructions
│     ├── Conversation
│     ├── Memory
│     ├── Retrieval (RAG)
│     ├── Tools
│     ├── Skills
│     ├── RuntimeState
│     └── Metadata
├── ContextOptimizer      压缩 / 摘要 / 丢弃
└── TokenBudget           按 Priority 排序，不是简单拼接
```

> 不要写 `context = system + messages + memory + tools` —— 很快变屎山。
> **Context 不是拼接，而是排序。**

### 7.5 Evaluation Platform（P1 提升为独立 Domain）

> v2.0 把 Evaluator 放进 Harness 不准确。Evaluation 自己有
> `EvaluationRun / Experiment / Dataset / Evaluator / Metrics`，已是**独立的 Platform Domain**。

```
Evaluation Platform
├── Evaluator           Deterministic / LLM-as-a-Judge / Trajectory / Outcome
├── Dataset             独立管理，版本化
├── Experiment          对比不同 AgentVersion
├── Metrics             Task Success / Tool / RAG / Generation / Cost / Latency
└── EvaluationRun       本身就是一个 Business Run（走 Execution Kernel）
```

- **Harness 可以调用 Evaluation，但 Evaluation 不是 Harness 的组成部分**
- Evaluation Run 复用了同一套 Kernel：`EvaluationRun(Business) → Task → Execution → Attempt`

**Evaluation 四层**：`Deterministic → LLM-as-a-Judge → Trajectory → Outcome`
（Regression Evaluation 是独立机制，不属于这四层）

先做 **Failure Attribution（归因）** + **Failure Taxonomy（分类体系）**，再谈优化。
由 **Evolution Controller** 统一调度五个方向：
`Prompt / Tool Selection / Model Routing / Skill / Memory Policy`

> Evolution 不一定是在改 Prompt。最后才轮到 SFT / DPO / RL —— **标为 Future Extension**（见 §11.6）。

---

## 8 · Memory / Knowledge / Context / Artifact 存储边界（P2）

### 8.1 通用原则（贯穿全系统）

| 存储 | 定位 |
| --- | --- |
| **PostgreSQL** | **durable truth** |
| **Redis** | **runtime** |
| **Qdrant** | **retrieval index**（不是事实存储） |
| **Kafka** | **durable event log** |
| **S3 / MinIO** | **large object** |

### 8.2 Memory（Qdrant 只是索引）

```
Memory
 ├── Metadata / Lifecycle  → PostgreSQL     (durable truth)
 ├── Hot / Working         → Redis          (runtime)
 └── Semantic Index        → Qdrant         (retrieval index)
```

> ⚠️ **不要让 Qdrant 成为 Memory 的最终事实存储。** 它只是语义索引，可重建。

四类型：Working / Semantic / Episodic / Procedural
Scope：Run 级 / Agent 级 / User 级 / Tenant 级 / Global

写链路：`Experience → MemoryCandidate → MemoryPolicy → Importance → 去重(精确+语义) → 冲突解决 → 版本化 → Store`
**读写必须分开**；Memory 不做简单 DELETE，做版本化 + 失效标记。

### 8.3 Knowledge（三层，不要让 Qdrant 成为唯一存储）

```
Object Storage    →  Original Document（原始文件）
       ↓
PostgreSQL        →  Document Metadata / Version（durable truth）
       ↓
Qdrant            →  Semantic Index（retrieval index）
```

RAG 链路：
`Document Pipeline → Chunk → Embedding → Hybrid Search(Vector+BM25) → Fusion → Rerank → Context Assembly → Citation`

- **Memory ≠ Knowledge**：Memory 是经验（主观、动态）；Knowledge 是资料（客观、版本化）
- **权限必须进入 Retrieval**（Permission-aware Retrieval，不是检索后再过滤）
- RetrievalResult 必须带 **Citation**

### 8.4 Artifact（新增，企业级 Agent 必需）

生成的 PDF / Excel / 图片 / 代码仓库 / 数据集 / 模型输出 / 分析报告 —— **不能塞进 Observation**。

```
Execution ──→ Artifact ──→ S3 / Object Storage
```

Observation 只保存引用：

```json
{
  "artifact_id": "artifact_123",
  "type": "pdf",
  "uri": "s3://agentos/artifacts/artifact_123.pdf",
  "metadata": {}
}
```

### 8.5 四者区分

| | 是什么 | 生命周期 | 存储 |
| --- | --- | --- | --- |
| **Memory** | Agent 记住的经验 | 跨 Run | PG + Redis + Qdrant |
| **Knowledge** | 外部资料 | 版本化 | S3 + PG + Qdrant |
| **Context** | 每次送给模型的工作台 | 单次调用内 | 运行时组装，落 ContextSnapshot |
| **Artifact** | 产出的文件 | 长期 | S3 + PG 元数据 |

---

## 9 · Event / Observation / State / Trace 彻底区分

| 概念 | 定义 | 例子 |
| --- | --- | --- |
| **Observation** | **Agent 感知到的东西** | Tool 返回的结果 |
| **Event** | **系统发生过的事情** | `ToolExecutionStarted` / `ToolExecutionCompleted` |
| **State** | 系统当前状态 | `run.status = RUNNING` |
| **Trace** | 执行链路（观测视图） | OTel Span Tree |

一次执行的时间线：

```
Event → Event → Event → Observation → State Change
```

即：`ToolExecutionStarted` → `LLMCalled` → `ToolExecutionCompleted` → **Observation(结果)** → **State 更新**

> Observation 是给 Agent 看的；Event 是给系统 / 审计 / 评估看的。**不要混。**

### 9.1 Observation **不直接**更新 State（需要 State Reducer）

> v2.0 写"Observation：执行结果 → 更新 State"稍粗。Observation 是事实/感知，State 是系统解释后的当前状态，
> 中间必须有一层转换，否则两者又会混起来。

```
Execution Result
      ↓
Observation Processor
      ↓
Observation                ← 事实："balance = 100"
      ↓
State Transition (Reducer) ← 解释：写入 variables / 更新 completed_tasks
      ↓
New State                  ← "account_balance = 100"
```

| | 性质 | 例子 |
| --- | --- | --- |
| **Observation** | 事实 / 感知（append-only，不可变） | Tool 返回 `balance = 100` |
| **State** | 系统解释后的当前状态（可变，带 version） | `variables["account_balance"] = 100` |

`State.apply(observation)` 内部就是 Reducer —— 唯一的合法状态变更入口。

---

## 10 · 数据底座（P0 表述修正）

| 技术 | **正确定位** | 说明 |
| --- | --- | --- |
| **PostgreSQL** | **Current Durable State / Business Source of Truth** | 记录"现在是什么状态" |
| **Kafka** | **Durable Event Log / Event Backbone** | 记录"发生过什么" |
| **Redis** | Low-latency Runtime State | 热状态 / 锁 / 取消 / 幂等 / 限流 |
| **Qdrant** | Retrieval Index | 语义检索 |
| **S3 / MinIO** | Large Object Storage | Artifact / Dataset / Checkpoint 大对象 |

> ❌ v1 错误表述：**"Kafka = Execution Truth"**
> ✅ v2 正确表述：**Kafka 的 Event Log 并不自动等于完整的业务 Truth。**
>
> 既然已声明**不采用纯 Event Sourcing**，就不能又说 Kafka 是 Truth —— 会被面试官抓住。

**一致性如何保证**：

```
PostgreSQL（状态变更）+ Transactional Outbox  ──→  Kafka
                                                    ↓
                                          Idempotent Consumer
```

> **PG + Kafka 通过 Transactional Outbox + Idempotent Consumer 保证状态变化和事件传播的一致性。**

### 为什么不纯 Event Sourcing

> Agent Runtime 对实时状态查询非常敏感，纯 Event Sourcing 会增加状态重建成本。
> 因此采用 **CQRS + Event-driven + Snapshot**：PG 提供低复杂度当前状态查询，Kafka 提供事件流与审计来源。

---

## 11 · 其余模块（保持 v1 结论）

### Tool / Skill / MCP / A2A / Multi-Agent（M3 / M4）

| | 本质 | 形态 |
| --- | --- | --- |
| **Tool** | 一个函数 / 能力 | `input → execute → output` |
| **Skill** | 一段可复用的"做事方法" | Prompt Skill / Workflow Skill / Agentic Skill |
| **Agent** | 能自主决策的执行体 | `goal → decide → act → observe → result` |

**Tool Runtime 8 步安全边界**：
`Resolution → Input Validation → Policy → Guardrail → Rate Limit → Timeout → Retry → Execute`

- Tool Runtime 不关心具体协议（Native / MCP / HTTP 统一）
- Everything Versioned（Tool 也要 Version）—— 保证 Reproducibility
- **Tool ≠ Credential**（凭证由 Vault 隔离注入）
- **MCP** = Tool Connectivity；**A2A** = Agent Connectivity（请求携带 Context Boundary）
- **Multi-Agent = Structured Delegation**，不是"多个 Agent 随便聊天"
- Child Agent 不是普通 Tool，Delegation 创建 **Child AgentRun**，走同一套 Kernel

### Model Gateway（M6）

```
Model Registry / Model Gateway / Provider Adapter / Model Router
Router: Capability → Cost → Latency → Load-aware (+ Policy)
Reliability: Retry / Timeout / Fallback / Circuit Breaker
Cost: Token / Model / Run / Tenant / Budget
```

- **Model ≠ Deployment**：AgentVersion 绑 ModelConfig（能力需求），Router 决定具体 Deployment
- Adapter 必须转成统一 `ModelResponse`，不能原样返回厂商结构

### Evolve（M8）→ 见 §7.5 Evaluation Platform

> Evaluation 已提升为**独立 Platform Domain**，不再放在 Harness 内。
> 演化闭环：`Run → Trace → Evaluate → Failure Attribution → Optimize → New Version → Production`
> 五个方向：`Prompt / Tool Selection / Model Routing / Skill / Memory Policy`，由 **Evolution Controller** 统一调度。

### Production（M7）

```
Client → API Gateway / Ingress → API Pods (FastAPI)
   → PostgreSQL → Outbox → Kafka → Scheduler
   → [ Default Worker | GPU Worker ]
   → Agent Harness → Agent Runtime → Execution Kernel
   → Model Gateway → [ OpenAI | vLLM | Qwen ] → GPU Cluster
```

- **Control Plane / Execution Plane 分离部署**：API 是 IO 密集，Worker 是计算密集
- **HPA 不能只看 CPU** → 业务指标驱动（Queue Depth / Run Latency）
- **AgentOS Scheduler ≠ Kubernetes Scheduler**：前者调度业务 Task，后者调度 Pod
- Rolling Update / Canary / Rollback；AgentVersion 是独立 Deployment Unit

### Developer Platform（M11）

SDK（Agent / Tool / Skill / Workflow）· REST / Async / Streaming API · Agent Manifest + Package ·
Agent Registry · CLI · Console（Debug Playground / Replay / Eval Playground）· CI/CD + GitOps

- Replay：Live Replay（重跑）vs Deterministic Replay（确定性复现）
- Test Pyramid：Unit → Integration → **Agent Evaluation** → E2E

### Cognitive Runtime（M12 / M13）

```
User Goal → Goal Interpreter → Cognitive Router
        ┌──────────────┬──────────────┐
    Fast Path       Planner      Deep Reasoning
        └──────────────┼──────────────┘
            Candidate Plan → Plan Validator → Action Selector
            → Policy/Guardrail → Task → Execute → Observe → Verify
                  ┌───────┴───────┐
               Success          Failure
                  ↓               ↓
                Finish      Reflect → Replan → Decision
```

`Task Complexity: SIMPLE / TOOL / MULTI_STEP / REASONING / RESEARCH / MULTI_AGENT / HIGH_RISK`
第一原则：**不要让所有任务都走 Deep Reasoning** —— 本质是在做计算资源分配。

### Enterprise（M9）

Multi-Tenant / Tenant 隔离（API / Data / Vector / Object / Runtime）/ IAM + RBAC + ABAC / OPA /
Vault / Sandbox（Firecracker / gVisor）/ API Gateway / DLQ + Event Replay / Workflow Engine /
Agent Marketplace + Agent GitOps

### 11.6 ⚠️ Future Extension（架构设计内，但不在实现范围）

> 文档里出现的技术名词很多，若声称全部实现，项目会巨大到失控，
> 且容易被面试官问穿："这个人真的实现了 Agent + RAG + MCP + A2A + K8s + RL 全套？"

以下内容**属于架构演进方向，明确标为 Future Extension，不作为 M15 交付范围**：

| 类别 | 内容 | 定位 |
| --- | --- | --- |
| **模型训练** | SFT / DPO / RL | Future Extension。Evolution 优先做 Prompt / Tool Selection / Model Routing / Skill / Memory Policy 五个方向 |
| **深度推理** | Tree Search / Self-Consistency / Deep Reasoning | Future Extension，成本高按需启用 |
| **高级执行** | Actor + Mailbox / Saga / Workflow Engine | 设计已闭环，第一版不实现（Actor 仅作 Stateful Execution 的实现模型） |
| **平台化** | Agent Marketplace / GitOps / Multi-Tenant 完整隔离 | 架构已定义，按需演进 |

**必须坚持的原则**：每个技术都要有明确的
**数据模型 + 一致性要求 + 延迟要求 + 故障模型 + 使用边界**。答不上来就不要放进去。

---

## 12 · 五大闭环

```
① Intelligence   Goal → Decision → Action → Observation → Decision
② Execution      Task → Schedule → Execution → Attempt → Checkpoint → Recovery
③ Knowledge      Query → Retrieve → Context → Answer → Evaluation
④ Development    Develop → Test → Evaluate → Deploy → Observe
⑤ Evolution      Run → Trace → Evaluate → Failure Analysis → Optimize → New Version
```

---

## 13 · 架构不变式清单（面试杀手锏）

这些比"我用了 LangChain / Redis / Kafka / RAG"高级得多：

```
Agent ≠ LLM
Agent ≠ Tool
Agent ≠ Workflow

Runtime ≠ Harness ≠ Kernel

Memory ≠ Knowledge ≠ Context ≠ Artifact

Decision ≠ Action ≠ Task ≠ Execution ≠ Attempt
Step    ≠ Task

Event ≠ State ≠ Trace ≠ Observation

AgentOS Scheduler ≠ Kubernetes Scheduler

Model ≠ Deployment
Tool  ≠ Credential
Qdrant ≠ Truth（只是 retrieval index）
Kafka  ≠ Truth（只是 durable event log）

AgentRun ≠ Kernel Execution（业务 Run vs Kernel 执行实体）
Execution Recovery ≠ Agent Recovery（救回 Execution vs 失败后怎么办）
TaskExecution / ToolExecution ≠ 领域对象（Kernel 只有 Task/Execution/Attempt）
Confidence ≠ Probability（只是 signal，不是校准概率）
Observation ≠ State（事实 vs 解释后的状态，中间有 Reducer）
```

---

## 14 · Milestone（冻结到 M15）

| Milestone | 主题 | 交付 |
| --- | --- | --- |
| M0 | Platform Foundation | Monorepo / Docker Compose / PG / Redis / Kafka / OTel / FastAPI / Worker |
| M1 | Agent Domain | Agent / AgentVersion / AgentRun / Step / Event / State Machine |
| M1.5 | 可靠性 | Outbox / Retry / Idempotency / Lease / Checkpoint / Recovery / Cancellation |
| M2 | Agent Harness | Context Engineering / Memory / Policy / Guardrails / HITL / Cost |
| M3 | Runtime | Agent Loop / Tool Runtime / Skill Runtime / Scheduler / Worker |
| M4 | Agent Connectivity | MCP / A2A / Multi-Agent / Agent Team |
| M5 | Knowledge | RAG / Hybrid Search / Rerank / Knowledge Version |
| M6 | Model Platform | Model Gateway / vLLM / TRT-LLM / Routing / Fallback / Cost |
| M7 | Production | K8s / HPA / Rolling Update / Canary / Rollback / GitLab CI-CD |
| M8 | Agent Intelligence | Evaluation / Trajectory / Evolve / SFT / DPO / RL |
| M9 | Enterprise | Multi-Tenant / IAM / OPA / Vault / Sandbox / Workflow Engine |
| M10 | Distributed Kernel | 统一 Execution / Lease / Checkpoint / Idempotency / Saga |
| M11 | Developer Platform | SDK / Manifest / Package / Registry / CLI / Console / GitOps |
| M12 | Agent Intelligence Layer | Cognitive Architecture / Decision Engine / Planner / Reflection |
| M13 | Cognitive Runtime | Cognitive Router / Fast Path / Deep Reasoning / Verifier |
| M14 | Kernel Protocol | Core Domain Contracts 三组统一 |
| **M15** | **Kernel Implementation** | **见下** |

> ⛔ **架构到此冻结。M16+ 不再新增概念。**

---

## 15 · M15：Kernel Implementation（下一步）

### 15.1 第一件事不是 FastAPI，也不是 Kafka

先把 **8 个核心对象**的领域模型和不变量定义出来：

```
Goal / State / Decision / Action / Observation      ← Intelligence Contracts
Task / Execution / Attempt                          ← Execution Contracts
```

外加：`Checkpoint` / `Lease` / `Cancellation` / `Event`

> 这一步做好，后面的 PG 表、API、Worker、Kafka 都会**自然长出来**。

### 15.2 实施顺序

> **P1 修正**：v2.0 的顺序是 `Domain → PostgreSQL → Kernel`，这**违反了自己的原则**
> —— "先定义执行模型，再选择基础设施"。PG Schema 应由 Domain + Kernel 的生命周期**定义出来**，而不是反过来决定 Domain。

```
阶段 1  Domain Model        8 个核心对象 + 不变量（纯 Python，零依赖）
阶段 2  State Machine       ExecutionStateMachine / AttemptStateMachine + 转换表
阶段 3  Execution Kernel    Scheduler / Lease / Attempt / Checkpoint / Retry /
                            Recovery / Cancellation / Idempotency（In-Memory 实现）
阶段 4  Repository Interfaces   ← 抽象接口，不绑定任何数据库
阶段 5  PostgreSQL          表结构由上面的生命周期定义出来 + Outbox
阶段 6  Redis               Lease / Cancellation / Idempotency
阶段 7  Kafka + Worker      Event 驱动
阶段 8  最小 Agent Loop     接一个 LLM + 一个 Tool
```

即：**Domain → StateMachine → Kernel → Repository Interfaces → PostgreSQL → Redis/Kafka**

### 15.3 验收：跑通最小闭环

```
POST /agents/{agent_id}/runs
        ↓
    AgentRun
        ↓
      Task
        ↓
    Execution
        ↓
     Worker
        ↓
       LLM
        ↓
     Decision
        ↓
       Tool
        ↓
    Observation
        ↓
      State
        ↓
      Final
```

---

## 16 · 工程规范

### Monorepo

```
agentos/
├── apps/
│   ├── api/           FastAPI
│   ├── scheduler/
│   └── worker/
├── packages/
│   ├── agent_domain/        ← M15 从这里开始
│   ├── agent_harness/       context / memory / policy / guardrails /
│   │                        agent_recovery_strategy / human_loop / cost
│   ├── agent_runtime/       loop / planner / decision / action / task_factory
│   ├── evaluation/          evaluator / dataset / experiment / metrics（独立 Domain）
│   ├── execution_kernel/    state_machine / scheduler / lease / attempt /
│   │                        checkpoint / retry / recovery / cancellation / idempotency
│   ├── event_bus/
│   ├── database/
│   ├── cache/
│   └── observability/
├── infrastructure/          docker / kafka / postgres / redis / k8s
└── tests/
```

> 注意：`agent_runtime/` 不再包含 scheduler / state；新增独立的 `execution_kernel/`。

### 设计纪律 6 问

```
1. 为什么需要它？   2. 它负责什么？     3. 它不负责什么？
4. 接口是什么？     5. 数据怎么流？     6. 出故障怎么办？
```

答完才允许 AI Coding。

### 技术选型纪律

反对"技术栈堆砌"。每个技术都要有明确的 **数据模型 + 一致性要求 + 延迟要求 + 故障模型 + 使用边界**。
追求 Google 级架构思维，而不是 README 上技术名词最多。

---

## 17 · 附：收敛变更清单

### 17.0 v2.1 → v2.1.1（第三轮评审，已并入基线）

> 本轮评审对象为外部基线文档 `AgentOS_企业级Agent平台架构_v2.1_最终冻结版.md`。
> 评审结论：方向 9/10，边界 8.5/10，**可直接开写代码 7/10**。
> 缺陷不是"写错"，而是"**有名字、没定义**"——概念层干净，但有 3 处会在 M15 第三天卡住。
> 全部补丁已写回基线（详见基线 §49），本文不再重复概念定义。

| # | 优先级 | 问题 | 补丁 |
| --- | --- | --- | --- |
| 1 | **P0** | Task:Execution 基数未声明，Lease 归属自相矛盾（Scheduler 只认 Task，Lease 却在 Execution） | 钉死 `Task:Execution = 1:1`；**Lease 挂 Execution**，Scheduler 选 Task、Claim Execution；加 fencing_token |
| 2 | **P0** | Policy / Guardrail 运行期拦截点未定义，Harness 若不反向调用 Kernel 就无法拦截 | 钉死 hook point：`Agent Loop → Action → Harness.Policy/Guardrail → Task`；Harness 永远是被调用方 |
| 3 | **P0** | Step 不在任何 Domain 列表；且"Agent = 动态决策图"与"Step = 静态执行图节点"冲突 | Step 归入 **Business Domain**，定义为 **Plan Node 的运行实例**（Runtime 动态产生） |
| 4 | **P1** | Task / Execution / Attempt 状态机只有名字没有状态集合；STALE、CANCEL_REQUESTED 游离 | 补齐 Execution（PENDING/RUNNING/STALE/SUSPENDED/COMPLETED/FAILED/CANCELLED）与 Attempt 状态集合及转换表 |
| 5 | **P1** | 状态机机制与策略未分离（AgentRun 是 Business 对象却归 Kernel 状态机） | Kernel 提供机制（能不能转），Runtime / Harness 决定策略（要不要转） |
| 6 | **P1** | Kernel 无 Retry 判据（§30 Failure Taxonomy 只服务 Evolution） | 新增 **RetryPolicy** + **Kernel Failure Class**；EXTERNAL_UNKNOWN 禁止盲重试 |
| 7 | **P1** | Idempotency Key 作用域未定义；缺 fencing token | Key = `{execution_id}`，跨 Attempt 稳定；Lease / Idempotency / Fencing Token 三者分离 |
| 8 | **P1** | Checkpoint 混入业务语义（current_step / completed_tasks） | 拆 **Kernel Checkpoint** / **Run Checkpoint**，并定义写入时机 |
| 9 | **P1** | Gateway Fallback 与 Kernel Retry 叠加造成重试放大 | Fallback 属单次 Attempt 内部容错，**不产生新 Attempt** |
| 10 | **P1** | Suspension 所有权未定义（Cancellation 有三段式，Suspension 没有） | 四段划分：Harness 请求 / Kernel 生命周期 / Wake-up Controller 检测 / Scheduler 重调度 |
| 11 | P2 | Roadmap 中 Kernel 出现在 M1.5 / M10 / M15 三处 | M1.5 = 最小补丁，M15 = 成型，M10 = 建立在 M15 之上的高级能力 |
| 12 | P2 | Governance 只在图中出现一次；Policy 同时挂 Harness 与 OPA | Governance 定位为**横切能力**，非第五边界；运行时决策执行点在 Harness |
| 13 | P2 | Monorepo 缺包与部署单元 | 补 outbox_publisher / recovery_controller / wakeup_controller 与 knowledge / memory / governance / sandbox |
| 14 | P2 | 并行 Task 同时 reduce 同一 State 无策略 | State version + per-run 有序队列 + 冲突重放；Kernel OCC 与 State version 两层都要有 |
| 15 | P2 | Replanning 无边界 | 加 max_replan_count / replan_budget / no-progress 检测 / escalation |

### 17.1 v2.0 → v2.1（第二轮评审）

| # | 优先级 | 问题 | v2.0 | v2.1 |
| --- | --- | --- | --- | --- |
| 1 | **P0** | TaskExecution / ToolExecution 造成新套娃 | Kernel Execution = TaskExecution / ToolExecution | **删除这两个领域对象**。Kernel 只有 `Task / Execution / Attempt`；ToolCall → Action → **Task(type=TOOL)** → Execution → Attempt |
| 2 | **P0** | AgentRun 与 Kernel Execution 边界不清 | 只说"业务级实例" | 明确 **AgentRun 是业务语义 Run，不等价于 Kernel Execution；其内部产生的 Task 由 Kernel 管理** |
| 3 | **P1** | Step → Task 写成一对一 | "一个 Step produces 一个 Task" | **一对多**，支持 Parallel / Planner / Multi-Agent / Fan-out / Fan-in |
| 4 | **P1** | StateMachine 实际只展开 AgentRun | 说是通用能力，展示却是 AgentRun | 明确 **StateMachine 是通用 Mechanism**；分 AgentRun/Task/Execution/Attempt 状态机；第一版实现 Execution + Attempt |
| 5 | **P1** | 两个 RecoveryManager 撞车 | Harness 有 RecoveryManager，Kernel 有 Recovery | 拆 **Execution Recovery（Kernel，基础设施级）/ Agent Recovery（Harness，改名 `AgentRecoveryStrategy`，策略级）** |
| 6 | **P1** | Evaluation 塞进 Harness | Harness 含 Evaluator | **提升为独立 Evaluation Platform**；Harness 只调用 |
| 7 | 表述 | SUSPENDED 缺唤醒定义 | 只写 `RUNNING ⇄ SUSPENDED` | 补齐 **Wake-up 机制**（Suspended → Wait Condition → Event/Timer → Wake-up → Runnable Task → Scheduler），属 Kernel |
| 8 | 表述 | Cancellation 归属混 | Harness 与 Kernel 都有 | **Harness 可发起请求，生命周期管理属 Kernel** |
| 9 | 表述 | Observation 直接更新 State | "Observation → 更新 State" | 加 **State Reducer / State Transition** 层 |
| 10 | 表述 | confidence 暗示概率 | `confidence: 0.97` | 改名 **`confidence_signal`** / `decision_metadata`；禁止 `confidence > 0.9 → 自动执行` |
| 11 | 表述 | Checkpoint 缺恢复边界 | 只说 ≠ ContextSnapshot | 明确 Checkpoint 结构，**引用** context_snapshot_id 不内嵌 |
| 12 | 表述 | M15 顺序违反自身原则 | Domain → PG → Kernel | **Domain → StateMachine → Kernel → Repository Interfaces → PG → Redis/Kafka** |
| 13 | 风险 | 范围失控 | 未标注实现边界 | 新增 **§11.6 Future Extension**（SFT/DPO/RL、Tree Search、Actor、Marketplace 等） |

### 17.2 v1 → v2.0（第一轮评审）

| # | 优先级 | 问题 | v1 | v2 |
| --- | --- | --- | --- | --- |
| 1 | **P0** | 执行单元概念重复 | AgentRun/Attempt/Step/Task/Execution 关系模糊 | 钉死 `AgentRun ⊃ Step → Task → Execution ⊃ Attempt` |
| 2 | **P0** | ToolExecution 套娃 | "全部统一为 Execution" | 拆 Business Execution / Kernel Execution |
| 3 | **P0** | Kafka 表述 | "Kafka = Execution Truth" | "Kafka = Durable Event Log"；Truth 只有 PG |
| 4 | **P0** | Harness/Runtime/Kernel 重叠 | Runtime 含 Scheduler/StateMachine/Retry/Cancel | Harness 控制 / Runtime 驱动 / Kernel 执行 |
| 5 | **P0** | State Machine 归属 | 属于 Agent Runtime | 属于 Execution Kernel |
| 6 | **P1** | 状态机膨胀 | 8 态含 WAITING_HUMAN / TIMEOUT | 7 态 + `SUSPENDED{suspension_reason}` |
| 7 | **P1** | 协议命名 | "五大核心协议" | 三组 **Core Domain Contracts** |
| 8 | **P2** | Actor 定位 | 隐含进 Kernel 核心 | 降为 Stateful Execution 的实现模型 |
| 9 | **P2** | Qdrant 定位 | "Semantic Memory Store" | **Retrieval Index**，非事实存储 |
| 10 | **P2** | Knowledge 存储 | 未明确 | S3(原始) → PG(metadata/version) → Qdrant(index) |
| 11 | **P2** | 缺 Artifact | 无 | Execution → Artifact → S3；Observation 只存引用 |
| 12 | **P2** | Event / Observation 混用 | 部分区分 | 彻底区分 + 时间线 |
| 13 | **P2** | Contracts 分组 | 无 | Intelligence / Execution / Integration 三组 |

### 已知原文内部不一致（保留记录）

1. "核心分成 8 层" vs 配图实际 6 个框
2. Evaluation 标题写四层但只展开前三层，第四层 Outcome 仅在树状定义中
3. Plane 划分有两套：四 Plane（Control/Execution/Event/Data）vs 三 Plane（Control/Execution/Model）
4. M13 无独立标题，紧接 M12 展开
