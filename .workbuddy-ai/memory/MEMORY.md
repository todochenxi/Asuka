# AgentOS · 长期笔记（核心精简版）

> 运维 playbook：同目录 `REFERENCE.md`（环境/工具链/变红脚本纪律/部署）。
> 详细坑位展开见 skill `agentos-milestone` §0.x；Asuka 纪律见 skill `asuka-eval`。

## AgentOS 平台
企业级 Agent 平台，价值在可靠/可观测/可审计，不是"多智能"。
50+ 冻结不变量（B/R/S/D/X/A/E/I/L/C/PR/O），口号：**宁可拒绝，不许编造**。
规模 v2.1.80：20 迁移 / 全仓 2057 单测 / 218 集成测试。
里程碑纪律（skill `agentos-milestone`）：空洞→实现→测试→变红验证→冻结基线。
M91 已闭合空洞 247（PlanNode.kind → Action 分派），冻结为 v2.1.79；M92 已闭合 AgentOS → Asuka 评测交接空洞 256/257，冻结为 v2.1.80；M93 交付 GPT 式聊天页（`/`）+ 控制台移到 `/console` + 多轮会话；M94 把 Asuka 的 LLM 调用搬进 AgentOS（删掉 Asuka 自带客户端）；M95 已闭合 Harness 的三个「有模块、没接线」尾账（Context 装配+快照落 PG / Memory 读写 / 输入·输出护栏含 REVIEW→审批）。**M96 补齐 Cost 预算（`AGENTOS_MAX_COST/TOKENS/STEPS`，默认不限）与 Memory 落 PG（020）**，Harness 六项全部接进运行时。**M97 补齐 Task Factory 一对多（扇出，`from_actions` + `payload["tasks"]`）**，Runtime 六项名副其实。**M98 收窄空洞 250**：§32 七项里 DAG Cycle / Dependency / Tool Exists 归计划期（新增 `PlanNode.tool` + `PLAN_TOOL_NOT_FOUND`），Permission·Risk·Budget **刻意**留执行期，Resource 无机制——归属表写进 `loop.py`。**M99 收口空洞 250 + M12 智能层首项**：`PlanNode.kind` 五种（task/tool/human/agent/decision）全部真分派；§32 的 Resource 落地（`PlanNode.resource_labels` + `PLAN_RESOURCE_UNAVAILABLE`，复用 Kernel 早已有的 `ResourceReq`/`WorkerCapability` 匹配）；顺带修 `state_from_dict` 不回读新字段的真 bug。**M100 梳理 M 层路线图并落地两项**：M3 Skill Runtime（`skill_runtime/`：SkillSpec 三种形态 + SkillRegistry + 派生前校验 `SKILL_NOT_FOUND`）、M5 Hybrid Search（RRF `HybridRetriever`）+ Rerank（`LexicalRerank`）。**M101 落地 M9 企业治理上半（OPA + Sandbox）**：Policy-as-Code（`agent_harness/policy_document.py`，严格加载——未知 effect/字段/action_type 加载期拒绝 P-1、`default` 必填 P-2、PDP/PEP 分离 P-3；顺带修 `PolicyEngine` 默认 DENY 会当场抛 H-1 的真 bug）+ Sandbox（`tool_runtime/sandbox.py`，`SandboxProfile` + `SandboxedCommandInvoker`；**只声称守得住的**：硬超时/输出上限/环境白名单/禁 shell/干净 cwd/声明路径，**不**提供网络/文件系统隔离旋钮；`protocol=sandbox` 必须配 `SandboxedInvoker`）。**M102 落地 M7 生产上半（CI/CD + HPA + 滚动/回滚）**：`.github/workflows/ci.yml`（unit 零依赖 / integration 真 PG + 拒绝"跳过变绿" / deploy 清单校验 / image 构建四作业）+ `deploy/k8s/07-hpa.yaml`（api/worker CPU HPA + behavior）+ `04-api.yaml` 加 `RollingUpdate{maxSurge:1,maxUnavailable:0}`（零停机，原生 pause=金丝雀 / undo=回滚）；**业务指标 HPA（队列深度）刻意不做**——需集群侧 metrics adapter，写个 External HPA 会把"没装 adapter 就不扩"伪装成"已支持"。**M103 落地 M4 连接上半（MCP）**：`packages/agent_runtime/connectivity/`——JSON-RPC 2.0 核心（J-1/2/3：版本、result/error 恰一、id 必须匹配）+ Transport（`InMemoryTransport` / `StdioTransport`，**request/send 必须分开**——通知走 send，否则 stdio 会在 readline 上死锁）+ MCP 客户端/`McpInvoker`/`register_mcp_tools`（MC-1 无名字拒绝、MC-2 只有 `readOnlyHint` 才是 READ，其余 UNKNOWN⇒T-2 要幂等键）。**M104 落地 M4 连接下半（A2A）**：`connectivity/a2a.py`——`AgentCard` + `A2AClient`（message/send、tasks/get、tasks/cancel）+ `A2AChildRunSpawner`（把 `ChildRunSpawner` 接到远端 Agent，复用 `_suspend_for_child` 那条既有委派路径）；不变量 A-1 只有终态才写终态、A-2 input-required 非终态亦非失败、A-3 状态闭集；实证坑：A2A 美式 `canceled` 必须归一化成 AgentOS 英式 `cancelled`（否则远端取消被读成失败）。**M105 收尾 M9 企业治理（IAM + Vault）**：IAM=`packages/agent_api/identity.py`（`Identity`/`IdentityProvider`/scope，I-1 无凭据=401 不退回匿名、I-2 缺 scope=403、I-3 actor 来自身份不来自 body；接进 `apps/api/app.py`，`AGENTOS_IDENTITY_TOKENS` 配了才开、默认不认证）；Vault=`packages/agent_harness/secrets.py`（`Secret` 的 repr/str/format 一律 `***`（V-1）、`SecretProvider` 端口 + env/内存实现、`secret://<scheme>/<key>` 引用解析，接进 `_dsn.resolve_dsn`）。**M106 补 M5 Knowledge Versioning**：`agent_context/versions.py`（`KnowledgeVersion` + 登记处「第一次注册即当前，换当前要显式」+ `VersionedRetriever`）——K-1 无版本声明的片拒、K-2 锁定检索绝不混版本（抛 `VersionMismatch`）、K-3 未锁定解析到 current（没有 current 拒）；版本过滤**与权限同构**（push-down + verify，C-9），但版本不匹配是**不一致**所以抛异常而不是 collected 进 denied；版本随 `ContextItem.attributes` 进 `ContextSnapshot`（可复现）。**M107 补 M7 业务指标 HPA**：`agent_api/metrics.py`（`Metric` + `render_prometheus` + `business_metrics`，M-1 值必须有限）+ `apps/api` 的 `GET /metrics`（Prometheus 文本，未配取数器 503 而非空 200；取数在组合根，A-1）+ `deploy/k8s/08-keda-worker.yaml`（KEDA ScaledObject 按 `count(*) FROM executions WHERE status='PENDING'` 扩 worker，需装 KEDA，已写明）；worker 的 CPU HPA 从 07 移除（一个 Deployment 的 replicas 只能有一个主人）。**M108 补 M4 HTTP/SSE 传输**：`connectivity/transport.py` 的 `HttpTransport`（JSON-RPC over HTTP POST；`application/json` 单响应或 `text/event-stream` SSE 读到 id 匹配；4xx 带 JSON-RPC body 当响应交上去）+ `McpClient.from_http` / `A2AClient.from_http`。用真本地 `http.server` 端到端验。**M 层可落地项至此全部收口**，仅剩 M13 认知运行时（Future Ext，有意不做）。**M109 控制台「分层记录」**：`GET /runs/{id}/trace` 的 `TraceView.layers`（`service.layers_of`：goal/plan/actions/executions/harness/state，缺的层不补空壳）+ 控制台 `index.html` 按层渲染。**M110 `GET /runs`（本进程装载过的 Run）**：控制台列出后端 Run —— 此前控制台"追踪的 Run"是**浏览器本地 localStorage**、聊天页存另一个 key，于是聊天开的 Run 在控制台没有入口；现在 `list_runs` 把后台能列的列出来（聊天/控制台经同一个 CP），控制台标「后端」可点。**M111 控制台概览「运行指标」**：前端解析 `GET /metrics`（Prometheus 文本）→ 队列深度 / 执行中 / 挂起 / 待审批四块；没配取数器（后端 503）时整块隐藏。**M112 控制台账本导航**：按 kind 过滤 + 按 Step 分组（`traceEntryHtml` 抽出复用）。**M113 分层记录补「Observation 层」**：`layers.observations`（kind/source/summary/content_keys，**不塞整个 content**）；分层卡片加「Observation · Agent 感知到的（→ State）」；State 节改名「State · 终态」。

## Asuka 子系统（同 repo `asuka/`，技术文档知识库评测 MVP）
语料 redis.io 官方 MD（428 chunks）；24 题人工任务集（三档难度各 8）；97 必答要点。
检索：BM25 词法基线 + Qdrant 本地 bge-m3。入口 `asuka/README.md`。

六指标全实现：Retrieval / Citation / Correctness / Latency / Token / Cost。
+ 回归对比（这次 vs 上次，复用 `agent_evaluation.regression`）。
+ 真模型：**模型调用归 AgentOS**（`packages/agent_runtime/model_gateway/deepseek.py`）；Asuka 不再自带 LLM 客户端。
  评测入口 `python -m asuka.agentos_eval`：**一题一条 AgentOS Run**（TOOL_CALL kb.search → LLM_CALL 带引用作答 → FINISH），
  Asuka 只读 Run 结果评分（`asuka.agentos_adapter`，B2 允许多 Run 进一次评测）。提示词/解析留在 `asuka/prompting.py`。

核心层零第三方（textutil/corpus/splitters/kb/dataset/evaluate/compare/answers/
context/trace/regression/deepseek 顶层 import 全标准库），重活惰性 import。
单测 **1798 条全绿**（零依赖解释器；含 M91 与 M92 适配器）。
变红脚本：red91(引用23)/red92(Context18)/red93(回归27)/red94(deepseek7)，骨架 redkit。

⭐ 三条同源纪律（详 skill `asuka-eval`）：
1. embedder 不许静默换人（签名 name@dim 必须一致，否则拒检索）。
2. 自述+实测两道判据（selftest 只留实测会时红时绿，实测已确认 HashingEmbedder 完全没抓住）。
3. 不可比对照表比没有更糟（compare.py 拒绝不同 top_k/题集/GT/冒烟，且一个数字都不印）。

⭐ 关键判据纪律（详 skill）：答案级主判据不用 LLM-as-Judge（偏爱长输出），改规则式必答要点召回，判据只住 `RequiredPoint.matched_by()` 一处；"没测≠答对"（points_total==0 ⇒ recall=None 印 `—`）；引用 cited 必须模型自述（None=不可测、()=明确0，两回事），score_citations 住一处，4 结论互斥（fabricated/grounded/依据召回/依据用上率）+ 缺依据三段归因 + 比例只 macro 平均；Context 装配 C-1/C-3/C-4（top_k 是条数不是窗口、retrieved≠available 两集合、取舍按相关性、chars_per_token 全包只认 textutil.CHARS_PER_TOKEN）；Trace 五条纪律（事件闭集/seq 连续/首尾固定/输入同一性含装配参数/键不能缺）；回归（两次都不通过归 unchanged 即 backlog≠回归、题级口径通过=全部采样通过、可比性门一次报全）。

⭐ 复用现状：Asuka 已接 `packages/agent_context/retrieval.py`（C-9/C-10）、`budget.py`/`tokens.py`/`items.py`，并新增 `asuka.agentos_adapter` 读取真实 AgentOS LLM Execution / ContextSnapshot / Trace；生产 Runtime 仍不 import asuka，保持 AgentOS 运行与 Asuka 评测解耦。

## 真模型基线（2026-09-23，DeepSeek-chat / redis / k10 / 窗口 8192-1024）
**bm25 s1**（首个基线）：要点召回 0.2361 · pass@1 0.0000 · 依据召回 0.5247 · 依据用上率 0.8772 ·
grounded **1.0000**（零编造）· 2248ms/题 · $0.0170（24 调用）。
**bm25 s3**（更稳）：要点召回 **0.2132** · pass@1 **0.0278** · pass@3 0.0417 · 依据召回 0.5455 · 2749ms/题。
难度梯度单调：simple **0.5271** / medium **0.1562** / hard **0.0250**（验证三档题集真能区分难度）。
13 题零要点（hard 设计题居多，语料缺合成材料；r-hard-05/r-hard-08 已声明语料缺口）。
快照在 `asuka/baselines/`（**受版本控制**；`asuka/runs/` 是 gitignored 的过程产物）。

⭐ **"检索变好" ≠ "答得变好"**（bm25 vs dense 都跑到 **s3** 的公平对照）：
dense(bge-m3) 检索级确实更好（context_recall 0.5941→**0.6594**、hit_rate 0.7917→**0.9167**），
且**传导到了引用**（端到端依据召回 0.5455→**0.6012**）；
但**正确性没跟上**：要点召回 0.2132→0.2035、**pass@1 0.0278→0.0000（归零）**、慢 44%（2749→3961ms）。
⇒ 只测检索级会误判"换 dense 就进步了"。**检索级与答案级必须同时测。**
⭐ **s1 不能下结论**：bm25 自己 s1→s3 就波动 **0.023**，与 dense-vs-bm25 在 s1 上的差（0.03）
**同量级** ⇒ 必须两边同采样数再比。

⚠️ `asuka.regression` **故意拒绝**跨配置比较（实测：换 retriever ⇒ 退出码 **2**、stdout 空、
**零个数字**、理由"变了被测系统不是退步"）。它只管**同配置、这次 vs 上次**；
换检索器 / 加采样是**实验**，走并排对照（检索级用 `compare.py`）。
⚠️ 跑 dense 前**必须**设 `ASUKA_EMBED_MODEL_PATH`（绝对路径），否则静默退化到 0 字节的
HF 缓存 config.json 并报"not a valid JSON"（详见 `REFERENCE.md` 第 5 条）。

⭐⭐ **运行间噪声 ≈ 待判效应 ⇒ 小差异不可判**（第十轮实测，目前最重要的一条）：
同一配置、同一 prompt、`temperature=0` 的**两次** bm25-s3：要点召回
**0.2132 vs 0.1889**（差 **0.0243**）；而 v1→v2 的差异是 **0.0289** ⇒ **同量级**。
⇒ DeepSeek 在 `temperature=0` 下**仍非确定性**（两次"没自述引用"条数 2 vs 1，输出真不同）。
⇒ **s1 不可信；s3 也只够看 0.05+ 的效应**。要分辨 0.03 必须 s5/s10，或接受分辨不出。
**别把噪声写成结论** —— 这是本平台最该输出的一类东西：不只说"改了没用"，
还说"**这个测量分辨不出这么小的差**"。

⚠️ **提示词是被测系统的一部分**：`AnswerReport.prompt_id`（`版本-内容指纹`）已进报告
与 `regression` 的同一性门 ⇒ 换 prompt 会被**拒绝**并排，而不是读成退步/进步。
指纹必须取自**内容**（只写版本号会被"改了文本忘了改号"绕过）；未知版本**拒绝**不回退。
代价（有意）：老基线无 `prompt_id` ⇒ 不能进回归对比，要用就重跑。变红 `red95.py` **6/6**。
第一次 prompt A/B（v2：要求简洁 + 逐条覆盖事实点）**没观测到提升**：要点召回
0.1889→0.1600、pass@1 0.0278→0.0000、每要点字符 565.9→**618.9**（**更啰嗦，与意图相反**）
—— 但受上述噪声所限，**不能判定 v2 更差**，只能说"没证明它有用"。

⭐ **丢点归因（`probe94.py`，第十一轮，零 API 成本）**：77 条"**真丢**"要点
（= **全部 3 次采样都丢**，避开噪声）拿 `any_of` 回语料查它在不在 ⇒
依据里就有（**纯生成侧**）**22.1%** ／ 语料有但不在依据 **15.6%** ／
材料在但需**跨片综合** **31.2%** ／ **真·材料缺失** **31.2%**。
⇒ **材料其实在的占 ~69%，真缺口只 ~31%** —— 主要瓶颈是**综合 + 生成**，不是语料覆盖
（与"dense 把检索 .5941→.6594 而正确性没动"完全吻合）。
⚠️ **别信严格子串匹配**：语料写 `set a timeout`、要点写 `sets a timeout`，差一个 **s**
就漏判 —— 第一版曾据此得出假的「85.7% 是语料缺口」。
⭐ 方法纪律：**一个高得可疑的数字，先怀疑自己的度量，别急着当结论**
（本项目已栽两次：严格匹配把"措辞不同"读成"材料缺失"；`HashingEmbedder` 把随机余弦读成语义）。
