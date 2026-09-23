# AgentOS · 长期笔记（核心精简版）

> 运维 playbook：同目录 `REFERENCE.md`（环境/工具链/变红脚本纪律/部署）。
> 详细坑位展开见 skill `agentos-milestone` §0.x；Asuka 纪律见 skill `asuka-eval`。

## AgentOS 平台
企业级 Agent 平台，价值在可靠/可观测/可审计，不是"多智能"。
50+ 冻结不变量（B/R/S/D/X/A/E/I/L/C/PR/O），口号：**宁可拒绝，不许编造**。
规模 v2.1.78：258 py / 18 迁移 / 1393 单测 / 209 集成测试。
里程碑纪律（skill `agentos-milestone`）：空洞→实现→测试→变红验证→冻结基线。
下一轮入口：空洞 250（§32 Plan Validator 只建了计划期那半，归属 M12 智能层）。

## Asuka 子系统（同 repo `asuka/`，技术文档知识库评测 MVP）
语料 redis.io 官方 MD（428 chunks）；24 题人工任务集（三档难度各 8）；97 必答要点。
检索：BM25 词法基线 + Qdrant 本地 bge-m3。入口 `asuka/README.md`。

六指标全实现：Retrieval / Citation / Correctness / Latency / Token / Cost。
+ 回归对比（这次 vs 上次，复用 `agent_evaluation.regression`）。
+ 真模型 `deepseek.py`（DeepSeekAnswerer，零第三方 urllib，引用自述 [[key]]，代价真测）。

核心层零第三方（textutil/corpus/splitters/kb/dataset/evaluate/compare/answers/
context/trace/regression/deepseek 顶层 import 全标准库），重活惰性 import。
单测 **1768 条全绿**（零依赖解释器）。
变红脚本：red91(引用23)/red92(Context18)/red93(回归27)/red94(deepseek7)，骨架 redkit。

⭐ 三条同源纪律（详 skill `asuka-eval`）：
1. embedder 不许静默换人（签名 name@dim 必须一致，否则拒检索）。
2. 自述+实测两道判据（selftest 只留实测会时红时绿，实测已确认 HashingEmbedder 完全没抓住）。
3. 不可比对照表比没有更糟（compare.py 拒绝不同 top_k/题集/GT/冒烟，且一个数字都不印）。

⭐ 关键判据纪律（详 skill）：答案级主判据不用 LLM-as-Judge（偏爱长输出），改规则式必答要点召回，判据只住 `RequiredPoint.matched_by()` 一处；"没测≠答对"（points_total==0 ⇒ recall=None 印 `—`）；引用 cited 必须模型自述（None=不可测、()=明确0，两回事），score_citations 住一处，4 结论互斥（fabricated/grounded/依据召回/依据用上率）+ 缺依据三段归因 + 比例只 macro 平均；Context 装配 C-1/C-3/C-4（top_k 是条数不是窗口、retrieved≠available 两集合、取舍按相关性、chars_per_token 全包只认 textutil.CHARS_PER_TOKEN）；Trace 五条纪律（事件闭集/seq 连续/首尾固定/输入同一性含装配参数/键不能缺）；回归（两次都不通过归 unchanged 即 backlog≠回归、题级口径通过=全部采样通过、可比性门一次报全）。

⭐ 复用现状：只真用了 `packages/agent_context/retrieval.py`（C-9/C-10）；
`budget.py`/`tokens.py`/`items.py` 已接（第六/八轮）。`agent_runtime` 等 5 大包一行没用——
Asuka 挂在 AgentOS 旁边，不是长在里面（反向也无 `import asuka`）。

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
