# AgentOS 项目长期笔记（核心）

> 运维 playbook（环境/工具链/变红脚本纪律/部署命令/端口惯例/版本库状态）在**同目录
> `REFERENCE.md`** —— 本文件只留会被自动注入的核心。**碰到测试、环境、变红脚本、
> 部署相关的问题，先读 `REFERENCE.md`。**

## 定位与判据

企业级 Agent 平台。价值不在"多智能"，在**可靠 / 可观测 / 可审计**。
50+ 条已冻结不变量（B/R/S/D/X/A/E/I/L/C/PR/O 系列）都朝这个方向。
反复出现的判据：**宁可拒绝，不许编造**。

## 规模（v2.1.78，2026-09-22）

| 项 | 数量 |
|---|---|
| Python 源文件 | 258 个 |
| PG 迁移 | 18 份 |
| 单元测试 | **1,393 条全绿** |
| 集成测试（真 PG） | **209 条全绿** |

已完成：基础设施、领域、内核、运行时、控制面、Contracts、部署编排（M67~M75）、
可恢复性收口（M85/M86）、计划消费语义（M87）、声明即契约（M88）、计划归属（M89）、
动作类型即契约（M90）。
**未实现**：M12 智能层（Decision Engine / Planner 只有 Protocol，`DemoPlanner` 顶着）、
M13 认知运行（依赖 M12）。部分：M11（SDK/CLI/Manifest 已做，缺 GitOps）。
**下一轮入口：空洞 250**（§32 的 `Plan Validator` 只建了"计划期能判死"那一半，归属 M12）。

## 里程碑流程

skill `agentos-milestone`：空洞 → 实现 → 测试 → **变红验证** → **冻结基线**。缺后两步不算完。
**接手时四条检查**：① 代码里的 M 编号是否大于文档里的？② 有没有**已诊断但未固化**的结论？
③ 版本号三处落点是否同源？（文档名 / `app.py` / k8s **14 处** tag）④ 冻结动作与测试运行**不许重叠**。

## 活跃空洞

| # | 形状 | 处置 |
|---|---|---|
| 225 | 不存在的 Run 叫停返回 200 unknown | 架构限制（无 runs 表）；页面诚实显示"已落下意图" |
| 237 | 部署清单指向 `examples.demo_stack` | **M12 落地时必须回来改这三行** |
| 238 | 预算耗尽不给 State 留痕 | 登记不治；Intelligence 要读"我为什么停了"时必须补 |
| 240 | 集成版本断言做了跨时代比较 → 改文件与跑测试重叠就必红 | 登记不治；纪律：冻结与测试串行 |
| 243 | ad-hoc 步序号用 `len(steps_of_run)` → 编号会跳过 | 登记不治：名字只是标签 |
| 244 | `PlanNode.expected_output` 能序列化能还原，**从不被读** | 登记不治：自由文本，做判据需裁判模型 |
| 245 | 文档"版本变更"段 v2.1.66 之后是**倒序** | 登记不治：重排要移动 ~10 个大段 |
| 246 | `Plan.root_nodes()` 调用者**只有测试** | 登记不治：它是**正确**的图入口，M12 会用上 |
| 247 | `SUPPORTED_PLAN_NODE_KINDS` 只有 `task`（声明了五种） | 登记不治（有归属）：M12 扩它，拒绝自动消失 |
| 248 | §32 的 `Plan Validator` 全仓不存在；七项只有 2 项有人做 | **已由 M89 收窄**：建了"计划期能判死"的那部分。余下见 250 |
| 249 | `Plan.constraints` 是自由字符串，`_plan_shape()` 读它只为判"是不是同一条路"，**从不执行** | 登记不治：要变判据需要一套**策略语言**（Policy/Harness 的活）。与 244 同族 |
| **250** | **§32 的 Validator 只建了一半**：七项里 **5 项不在计划期**（Permission/Risk/Budget 换到执行期逐步；Tool Exists/Resource 连表达都表达不了） | **下一轮入口（归属 M12）**：要么落地真校验阶段，要么把 §32 改成**实话**。归属表已写进 `loop.py` 模块级注释 |
| 251 | 计划的落点是 **State/快照**，不是 **trace** ⇒ 只看 trace 审计不了"这条 Run 用了哪份计划" | 登记不治（**是事实不是缺陷**）；与 234 同族：**信息在，但在另一层** |
| 252 | `SuspensionReason.TIMER` **有 consumer 无 producer**（`wakeup_controller.py` 真读它，生产代码零设置） | **登记不治（归属 Wake-up Controller）**：这正是 `wait` 执行不了的**真因**。要能执行 `wait` 必须先有 TIMER 的 producer |
| 253 | `SuspensionReason.EXTERNAL_EVENT` **全仓零引用**（连 consumer 都没有） | 登记不治：与 252 同族（"概念冻结了，实现从没跟上"），更彻底 |
| 254 | `StepOutcome.PLANNED` 声明了、**零 producer**（`_record(PLANNED)` 全仓 0 次） | 登记不治（**无害**）：无人产也无人读。留着的代价是"读枚举的人以为有一个'刚规划完'的状态" |

（231/234/236/239/241/242 已闭合或登记不治，细节见当日日志。）

## 已冻结的关键不变量（近几轮）

| 不变量 | 内容 |
|---|---|
| **I-6** | 判据住 `Observation.__post_init__`，**双向**：来源 `EXECUTION_RESULT` ⟹ 必绑非空 `execution_id` + `attempt_no>=1`；非执行来源**不许**绑 |
| **I-10** | 重规划**必须吃预算**（`steps += 1`），否则一直 REPLAN 的 Run 永远跑下去 |
| **I-11** | 完成前必须没有"自上次规划以来"未处理的失败，有则 REPLAN。判据钉在 **Loop 的 FINISH 分支** |
| **I-12** | 重规划必须产出**形状不同**的计划（比 `(node_id,name,kind)+constraints`，**不比 plan_id**） |
| **I-13/14** | 委派失败进 State（`execution.failed`，绑真实 Execution）；**查不出来的失败也算** → `execution_unresolved`（**不许冒充 `failed`**） |
| **I-15** | 三个终态父侧信息量必须分得开：`failed`／`cancelled`（谁+理由）／`unknown` |
| **I-16** | Plan 依赖图是**可执行约束**：进 Step 依据是"**它准备好了**"，不是"下标轮到它了"。游标属于**这份计划**。**卡住** → 不编造 ad-hoc，走 REPLAN；与"**用完**"分得开 |
| **I-17** | `run()` 停止条件 = "**这条 Run** 到了终态"，**不是**"这一步的结果属于某张白名单"。`FAILED` 作为"这一步的结果"不该进白名单，作为"Run 终态"必须让它停 |
| **I-18** | `PlanNode.kind` 是**闭集**；未知值**拒绝**，**不"兜底成 task"**。运行时必须**自述**能执行的集合（`SUPPORTED_PLAN_NODE_KINDS`，现只有 `task`），集合外 → **零副作用**判死 + 点名（哪个节点/声明什么/支持什么/**为什么不能凑合**）。**不 REPLAN** |
| **I-19** | 计划必须**属于这条 Run**：`plan.run_id != state.run_id` → **零副作用**判死 + 点名**两个** run_id。**不 REPLAN**。与 I-18 共用同一道门（`step()` 入口的 §32 Plan Validator）；`PlanDefect(code, detail)` 分两半，**多条缺陷同时成立时全都要报** |
| **I-20** | `ActionType` 里**执行不了**的成员（现 `WAIT`）：**执行任何动作之前**判死 + 点名（哪个类型/支持什么/为什么不能凑合/**正确替代路径**），**零副作用**。不许执行/跳过/崩在没账本记录的异常上，**不 REPLAN**。集合从 `ACTION_TO_TASK` **推导**（`EXECUTABLE_ACTION_TYPES`），手写会静默漏掉新成员 |
| **R-7** | 有状态注入实现必须 `progress()` 自述 + 能 `resume(p)` 接上；**先接上、接不上才点名拒绝**。不许静默重来 |
| **B-2** | `AgentRun.status` 是派生值，只能经 `sync()` 写 |
| **B-7** | 一个事实一处定义。Intelligence 的知识不许泄漏进 Runtime |
| **B-8 / B-12** | 取消/审批归因必填（`reason`+`by`）；终态声明**必须带原因**，落 `run.finished` payload |
| **D-37** | 子 Run 交给父 Run 的必须是它**真正的死因**。缺失时说"没记录到"，**不许用 summary 顶替** |
| **PR-14/15 / 24 / 33** | 客户端**惰性** import、`packages/` `apps/` 零第三方依赖；对外版本 = 冻结基线；版本号三处落点必须**同源** |

## ⭐ 反复踩到的坑 · A. 判据住在哪、覆盖谁

（B 断言与变异 / C 信息与状态 / D 流程与纪律 / E 环境与工具链 → 见 `REFERENCE.md`；
每条展开版见 skill `agentos-milestone` 的 §0.x）

| # | 一句话 | 轮次 |
|---|---|---|
| 1 | **承诺句不是机制**：`frozen` 只保证"不可变"，**不保证"构造受控"** ⇒ 判据住**所有构造路径的汇合处**（`__post_init__`）；问"除工厂外还有几条路能造出它" | M85 |
| 2 | 一句"这是全部"**漏了不在自己手里的那一半** ⇒ 先问：**这个"全部"的边界，是谁划的？** | M86 |
| 3 | **判据读什么，就决定了它能看见什么** ⇒ 补完再问：它依赖的那些事实，**是不是每一扇门都在写？** | M81 |
| 4 | 判据要挡的是"**坏的那条路**"，不是"那条路" ⇒ 新判据打红既有测试时，先问打红的这条是不是在描述**正常行为** | M86 |
| 5 | 读「**位置**」还是读「**意义**」 ⇒ 这个量是**问题本身**还是**问题的编号**？（`plan.nodes[len(steps)]` 该读"依赖完成没有"）配套：**同一个量身兼两职最危险**；**派生量优先现算不另存** | M87 |
| 6 | **声明与能力之间的差必须由系统说出来**；**沉默的差读起来像没有差** ⇒ 两半缺一不可：① 声明成为闭集 ② 运行时**自述能力**，集合外零副作用判死 + 点名 | M88 |
| 7 | 判据住「**所有路径的汇合处**」 ⇒ 问：**这个东西还有别的来路吗？**（`_plan()` 走 reducer；**快照恢复**直接写 `current_plan`） | M88/89 |
| 8 | 拒绝"整体做不到"时**查整份，不查"下一个"** ⇒ "宁可拒绝" = **在产生任何副作用之前**拒绝；断言要**双份**（没执行 / 没开 Step） | M88 |
| 9 | **"存在" ≠ "被处理"**：`reducer.py` 末尾兜底分支让任何 observation 都留在 State ⇒ `assertIn(kind, kinds)` 守不住 | M82 |
| 10 | **不要为不存在的能力写区分** ⇒ **账本上每一列都该对应一条真实路径** | M83 |
| 11 | **"每一处都在写、没有一处在读"的字段是注释不是约束** ⇒ 再 grep **同义字段的所有读写点**；⚠️ **跨对象**判据住**两个对象都在场**那层，作**参数**给（B-7），不能自证 | M89 |
| 12 | **重构会打死老的变红脚本；探针会说旧话** ⇒ 锚点**命中 0 次**会静默 SKIP（像"这条不重要"）；**任何"读旧世界"的断言都要在改完世界后重跑** | M89 |
| 13 | **"承诺与现实的差"写成归属表，比造一个像门的空壳诚实** ⇒ "换了时机/无机制"标成**事实**不是待办 | M89 |
| 14 | **`None` 一值三义 ⇒ 一义都不是** ⇒ 扫"**枚举里有没有没归宿的成员**"（问**谁产谁消费**）；⚠️ **"有 consumer 无 producer"最隐蔽**（像"路径被实现了"）；数 producer 用 **AST** 不用 grep | M90 |

---

# Asuka 子系统（同 repo 的 `asuka/` 包，2026-09-22 起）

面向技术文档知识库的 **Agent Evaluation & Runtime MVP**。复用 AgentOS 的
`packages/agent_context`（Chunk / citation / RetrievalPipeline / PermissionFilter）。
语料 = redis.io 官方 Markdown；24 条人工任务集（三档难度各 8）；428 chunks。
检索器两个：**BM25 词法基线**（对照组）与 **Qdrant + 本地 bge-m3**。
入口文档 `asuka/README.md`，环境陷阱 `REFERENCE.md`。

**三条与 AgentOS 同源的纪律**

1. **embedder 不许静默换人**：签名 `name@dim` 必须与索引落盘的一致；
   索引若由**不承载语义**的 embedder 建成，拒绝一切检索（要冒烟必须显式
   `allow_non_semantic=True` / `--allow-non-semantic`，默认 **False**）。
2. **自述 vs 实测，两道判据都要**：`embedding.selftest()` —— ① `info.semantic`
   必须 True（**确定性**，抓自述不承载语义的）② 相关句对余弦显著高于无关
   （**实测**，抓说谎的）。⭐ **只留实测是错的**：`HashingEmbedder` 的余弦差是
   随机量，会**时红时绿**，变红验证实测确认它**完全没抓住**。
3. **不可比的对照表比没有对照表更糟 —— 它看起来是结论**：
   `compare.py` 拒绝 `top_k` 不同 / 题目集合不同 / ground truth 不同 / 一边是冒烟，
   且拒绝时**一个数字都不出现**。

**派生量优先现算不另存**（同 A-5）：报告里的 `recall`/`precision`/`mrr`/上限
`load()` 时**重算**并核对文件里那份。上线即抓到一份**上限存 1.0、实为 0.9568**
的旧报告 —— 拿它并排比，差的全是指标定义的差。

**核心层零第三方**：`textutil`/`corpus`/`splitters`/`kb`/`dataset`/`evaluate`/`compare`/
`answers`/`context`/`trace`/`regression` 顶层 import 全是标准库（`context`/`regression` 还顶层引
`packages.agent_context.*` / `packages.agent_evaluation`（内核，同仓零依赖包）），
重活（langchain / qdrant-client / sentence-transformers）一律惰性 import。
单测跑零依赖解释器，**1753 条**。

**答案级判据（#117）**：主判据**不用 LLM-as-Judge**（它偏爱长输出 ⇒ 把"啰嗦"变成得分项），
改成规则式**必答要点召回** —— `RequiredPoint(label, any_of)`，Redis 24 题共 **97 条**。

⭐ 四条反复有用的规矩：

1. **判据只能有一份**：匹配规则住在 `RequiredPoint.matched_by()`，
   `Dataset.validate`（校验期）和 `answers.score_answer`（判据期）**调同一个方法**。
   两处各写一份 ⇒ 会出现"校验说声明没问题、判分说答不到"，而**两边测试都绿**。
2. **声明必须可被证伪**：一条要点若**参考答案自己都答不到** ⇒ 报错。
   它不是"这题难"，是标注写错了，且会让这题**永久**低分，而读者读成"模型不行"。
3. **假命中比漏判更危险**：单词短语必须**词边界**匹配（否则 `set` 在 `subset` 里命中，
   分数**虚高**）；参考答案的 markdown 标记（`**bold**`/`` `code` ``）必须**剥掉**
   （否则声明对了也匹配不上，且**失败静默**）。
4. **"没测"不是"答对了"**：`points_total == 0` ⇒ `recall = None`，**既不算 0 也不算 1**，
   报告里单列"不可测的题"并点名。混起来会让覆盖率看起来比实际高。

⭐ **校准不是成绩**：`oracle`（返回参考答案）必须 1.0、`null`（返回空串）必须 0.0，
这是**判据的上下界**。`is_calibration` 由 answerer **自述**，不说的被**拒绝** ——
默认当"真模型"是最坏选择：校准分数会被读成模型成绩，而报告里没有东西提醒你。

⭐ **判据的已知缺口用「度量」补，不用第二个判据补**：要点召回能被"抄满文档"刷，
所以报 `chars_per_hit_point`（参考答案 104.5 / 抄满 786）作为**信号**。
加 `must_not_include` 补精确度**又撤掉了** —— 自由文本里否定句会让子串匹配
**反向命中**（正确答案写"并不返回 -1 …"被判成答错）。**会误判的字段比没有更糟**。

**Trace（`trace.py`）**：报告回答"整体怎么样"，回答不了"**这题错在哪一步**"
（报告里的检索与答案是**分别聚合**的）。trace 是逐步过程：
`run.started → retrieval → generation → scoring → run.finished`。

⭐ 五条纪律，每条对应一个"读起来像没事"的失效：

1. **事件种类闭集**，未知值拒绝 —— 兜底成"其它" ⇒ 新事件静默不被处理。
   配套：测试断言 `set(EVENT_KINDS) == set(REQUIRED_FIELDS)`
   （新种类忘了声明字段 ⇒ 它永远不被校验，而"没校验"读起来和"校验通过"一样）。
2. **`seq` 从 0 连续** —— 缺号 ⇒ 事件丢了，而"少了几个事件"读起来像"本来就没发生"。
3. **首尾必须是 `run.started` / `run.finished`** —— **半截 trace 比没有 trace 更危险**，
   它看起来是一次完整运行。
4. **必须带输入的同一性**（语料/任务集 `sha256` + embedder 签名）—— 语料换了、
   题改了，同样的分数含义完全不同（同 AgentOS #251：信息在，但在另一层）。
   ⚠️ **装配参数也是同一性**：`context_budget` / `reserved_for_output` /
   `chars_per_token` 都在 `run.started` 里，并由 `verify_against` 与报告对账 ——
   "丢了 3 片"脱离"窗口多大"无法解释。加参数时**三处要同源**（identity/报告/CLI 默认）。
5. **必填字段的值可以是 `null`，但键不能缺** —— `.get(..., ())` 会把"旧格式缺字段"
   和"答案器没自述"读成同一个东西。

⭐ **三条路径算同一个数，必须对得上**：`verify_against(report)`（报告从对象算 vs
trace 从**序列化后的 JSON** 算）、`verify_retrieval_against(report)`（**两条命令**的
产物是否配套）。CLI **落盘之前**跑核对 —— 对不上就不写。
⚠️ 同时要说清它证明不了什么：`pass@k` 的 `k` 两条路径都从外面读进来。

⭐ **记 id 不复制正文，但记答案全文**：chunk 正文按 id 查得到（复制会产生第二个真相源），
而"它到底答了什么"是审计的主问题，几十 KB 不值得省。


**Citation（#118，六指标收口）**：要点召回只答"答到了几条"，不答"**凭什么**答的" ——
靠参数记忆答对和真读语料答对，在要点召回上**一模一样**。
所以 `Answer.citations` 是答案器**必须自述**的 chunk_id 元组。

⭐ **`None` 与 `()` 是两件事**：`None`=没自述 ⇒ **不可测**（退出分母，报告印 `—`）；
`()`=明确说"没引用任何来源" ⇒ 可测，召回 0。混起来"没测"会读成"答得没依据"。

判据 `score_citations(cited, *, available, retrieved, evidence)`（住一处）：
`available` = **真正喂进 prompt** 的（装配之后的 id，⚠️ **不含被权限拒绝的**、
**不含被预算丢掉的** —— 它看不到，引用了就是编造）；`retrieved` = 检索留下的。
四个结论各答一个问题，**不能互相替代**：
`fabricated = cited − available`（**编造**，硬判据，点名）／`grounded`／
`依据召回`（端到端，**同时受检索+预算+生成影响**）／`依据用上率`（**纯生成侧**）。
缺的依据必须**拆三段归因**（互斥、合起来恰好是 `evidence`）：
`evidence_not_retrieved = evidence − retrieved`（换检索器）／
`evidence_dropped = (evidence ∩ retrieved) − available`（**加窗口**）／
`evidence_ignored = (evidence ∩ available) − cited`（改 prompt）——
合成一段会把"装不下"读成"没检到"，改错地方。
实测 bm25 k5：**62 条检索没检到 / 26 条给了**；k10 压窗口到 400：
**49 没检到 / 20 装不下 / 0 给了没引**（⚠️ k5 与 k10 **不可比**）。

⭐ 三条别做的事：① **别报 `citation_precision`** —— ground truth 是**最少必要依据**不是
**唯一允许引的依据**，用 precision 罚它等于**奖励"少引"**；② **别再分"编造的 id 语料里
有没有"** —— 要分就得传整份语料 id，而**传不进来时静默降级**会让那栏读起来像
"没有这类问题"，**会静默降级的判据比没有更危险**；③ **`evidence_cited` 必须同时
`x in available`** —— 只判 `x in evidence` 的话，靠记忆背出答案的模型会拿满依据召回。

⭐ **比例只能有一种平均方式**：一开始用 Σ/Σ 得 0.2955，而检索报告里**同一个量**
`context_recall` 用逐样本均值得 0.4424 —— 同一个东西两个定义，读者只会以为看错了。
改 macro 后**逐位相同**，有测试钉住（`oracle` 的依据召回 == `context_recall`）。

⭐ **编造探测器必须被证明会响**：`oracle`（全对）/`null`（全空）都碰不到
"引了没给它的东西"这条路径 ⇒ 没有第三个校准答案器 `fabricator`，
`fabricated` 那栏可能是**一段永远为空的代码**，而报告看起来一切正常。

⭐ **交叉核对必须真的换一条算法**：引用那栏两条路径都调 `score_citations` 的话，
核对只是在证明"我等于我自己"。trace 侧从
`generation.citations − retrieval.context` **对减** ——
⚠️ 右边是 `context`（**喂进 prompt 的**）不是 `kept`（**检到的**）：被预算丢掉的片
模型没看见，引用了就是编造；用 `kept` 会把编造读成有依据，而这个方向**只会让分数变好看**。

⭐ **"改坏了没红"是查"这字段有没有人读"最快的办法**（red91/red92 的没红都指向真问题）：
`Answer.as_dict/from_dict` 零调用者 ⇒ **删掉**（不是补测试）；
`assertIn("编造", text)` 太松（否定句 `"- 没有编造引用"` 里也有）⇒ 收紧成整行匹配；
red92 的 N18 没红 ⇒ **测试漏了**"丢片场景下 `context_size` 该等于喂进去的片数"。
⇒ **断言里只出现一个词，就很可能在否定句里也出现。**

## Context 装配（`context.py`，2026-09-23 第六轮）

**动机**：`kb.search(limit=top_k)` 的 `top_k` 是**条数不是 token 数**。
**一个按条数控制的检索器会静默地把请求撑爆**，失败发生在**模型那一侧**
（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧 —— 评测跑得好好的，线上全崩。
⇒ 检索之后必须有装配这一步，**复用内核**（这是 Asuka 第二次真复用 AgentOS）：

| 不变量 | 内容 | 内核落点 |
|---|---|---|
| **C-1** | `Chunk`(Knowledge) → `ContextItem`(Context) **显式**转换 | `RetrievalPipeline.to_context_items()` |
| **C-3** | Token Budget 是**硬约束**：装不下就丢 | `budget.allocate()` |
| **C-4** | **静默截断是 bug**：每条被丢的都留 `(id, tokens, 原因)` | `DroppedItem` |
| **C-10** | 进 Context 的知识片必须带 citation | `ContextItem.__post_init__` |

⭐ **两个集合必须分开**：`retrieved`（检到了，过了 C-9/C-10）与 `available`
（**模型看见了**，再过一道 C-3 预算）⊆ `retrieved`。混成一个 ⇒ 被丢的片读成
"模型见过它" ⇒ 编造读成有依据 ⇒ **分数只会变好看，没有任何东西会报错**。

⭐ **取舍顺序必须按相关性**：`allocate()` 的键是
`sorted(items, key=(not pinned, -priority, key))`，而内核 `knowledge_chunk()` 给的
`priority` 是**同一个常数 30** ⇒ 退化成按 `chunk_id` **字母序**丢。名次与字母序反向时
**丢掉第一名、留下最后一名**，而且**静默**（分数照出，只是低了一点）。
⇒ 装配时把**检索名次**写进 `priority` —— **用**内核的旋钮，不是绕过它。
⚠️ `allocate()` 是**贪心"塞得下就放"**：名次 1 装不下会被丢、名次 2 顶上。
这是**对**的（不截断），但 `context_size < top_k` **不等于**"检索少检了"。

⚠️ **`chars_per_token` 是语料的属性**（4 英文 / 3 中文保守，差 **33%**）。
两处各写一个数 ⇒ 语料清单说 120 token、装配说 160 token，而**两边都不报错**。
⇒ 全包只认 `textutil.CHARS_PER_TOKEN`，`estimate_tokens` **委托**内核
`HeuristicTokenizer`（不再自己写一遍），并且它**进报告**。
⚠️ **不用内核的 `ContextAssembler` 整体**：它要 `run_id` 并存 `ContextSnapshot`，
而 Asuka 的审计凭证是 `trace`、评测里的 run 是 `task_id × 采样序号` 不是 `AgentRun`。
**这条要说出来** —— 不说的话读的人以为漏了。

## ⚠️ Asuka ↔ AgentOS 的复用现状（2026-09-23 实测）

**用了 1 个模块**：`packages/agent_context/retrieval.py`（Chunk / RetrievalQuery /
RetrievalPipeline / PermissionFilter）= **C-9 权限过滤 + C-10 citation 必填**的载体。
"被拒 / 无 citation 丢弃"两条拒绝路径是**复用**的。

**一行没用**：`agent_runtime`(10881 行) / `agent_domain`(3691) / `execution_kernel`(3465)
/ `agent_api`(1522) / `agent_harness`(1308) / **`agent_evaluation`(458)** / `agent_context`
其余 6 个 / manifest+registry+sdk。**反向也没有**：全仓没有一处 `import asuka`。
⇒ 现状是**挂在 AgentOS 旁边**，不是长在它里面。

**两处"还没接上"（不是重复）**：
1. `agent_context/tokens.py` 有 `Tokenizer` 端口。**已部分接上**：`estimate_tokens`
   现在委托 `HeuristicTokenizer`。但 `Answer.prompt_tokens` 仍是**答案器自己填的**
   （oracle/null 填 0）⇒ **Token 指标要到真 LLM 接上才有模型侧含义**（#116）。
2. ~~`agent_evaluation/regression.py` 的"这次 vs 上次"~~ —— **第七轮已接上**：
   `asuka/regression.py` 复用 `compare`/`regressions`/`Delta`，外面加**可比性门**
   （topic/retriever/answerer/top_k/samples/窗口/预留/cpt/corpus_chunks/calibration +
   逐题 question/points_total）+ **题级口径**（通过 = 这道题**全部采样**通过，不是至少一次）+
   **不可测点名**。CLI `asuka.regression <旧> <新> --out --json`。变红 `red93.py` 27 条。
3. ~~两处 token 估算系数不同~~ —— **已闭合**：全包只认 `textutil.CHARS_PER_TOKEN`，
   装配时显式传入并**记进报告与 trace identity**。

**曾经"另一个咬人的点"**：`kb.search` 的 `top_k` 是条数不是 token 数、没有
C-3/C-4 的概念 —— **已闭合**，见上面的「Context 装配」。
⇒ 复用一个包**不等于接上它**：`retrieval.py` 是"拿来用"，`budget.py`/`tokens.py`/`items.py`
才是"接上"。第六轮接的是后三个。
