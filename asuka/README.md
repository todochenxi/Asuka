# Asuka

面向**技术文档知识库**的 Agent Evaluation & Runtime（MVP）。

不是聊天机器人 —— 是让 Agent 用官方文档完成任务，而 Asuka **记录、执行、评估**整个过程。

```
K8s / Redis / Python / FastAPI / Linux / PyTorch 官方文档
    ↓ 文档处理（抓取 → 归一化 → 结构化 → 切分）
Knowledge Base（带 Citation）
    ↓ 生成
Task Dataset（question / reference_answer / required_points / evidence / difficulty）
    ↓
Agent（Retrieval + Answer）
    ↓
Runtime → Trace
    ↓
Evaluation（Retrieval / Citation / Correctness / Latency / Token / Cost）
    ↓
Evaluation Report
```

当前进度：**六指标全部有实现**（Retrieval / Citation / Correctness / Latency / Token / Cost），
Redis 一类文档端到端跑通。其余五类待做。

| 已完成 | 未做 |
|---|---|
| 语料 / 任务集 / 检索评测 / 对照 | 其余 5 类文档 |
| 答案级判据（规则式 + `pass@k`）+ 上下界校准 | |
| Citation：自述引用 + 编造探测 + 归因拆分 | |
| **Context 装配**（C-1/C-3/C-4）：`top_k` 之后再过一道 token 预算 | |
| Trace：逐步记录 + 审计视图 + 交叉核对 | |
| **回归对比**：这次 vs 上次，只报警"上次过、这次不过" | |
| 检索：BM25 与本地 bge-m3 全链路 | |
| **真 LLM 生成**：DeepSeek 已接入（`--answerer deepseek`，见下） | |

⚠️ `Correctness` 一直跑校准答案器（oracle/null 是判据上下界）。
`Latency` / `Token` / `Cost` 在 **`--answerer deepseek`** 下是**真模型侧**的数
（DeepSeek 返回的 `usage` + 声明单价）；校准答案器填 0 只是"没测"，不是"免费"。

⚠️ 这四项目前测的是**装配之后**的那一份上下文（`asuka/context.py`）：
`Token` 数的是真喂进 prompt 的 token，`Latency` 把检索和生成分开记。

## 端到端跑通（Redis）

```bash
# 0) 选解释器。单测用零依赖的那个；跑模型/索引用装了依赖的那个。
PY_UNIT="C:/Users/19644/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
PY_HEAVY="C:/Users/19644/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

# 1) 文档处理：抓取 → 归一化 → 切分 → chunks.jsonl + manifest.json
"$PY_HEAVY" -m asuka.corpus redis              # 联网抓官方 Markdown
"$PY_HEAVY" -m asuka.corpus redis --offline    # 用缓存重跑（可复现）
#   → asuka/corpus/redis/chunks.jsonl  (428 chunks)

# 2) 任务集：校验 + 落盘
"$PY_HEAVY" -m asuka.datasets redis
#   → asuka/datasets/redis.jsonl       (24 条，三档难度各 8)

# 3) 检索评测（**不需要 LLM**，现在就能跑）
"$PY_HEAVY" -m asuka.evaluate redis --retriever bm25 --top-k 10
#   → asuka/runs/retrieval/redis-bm25-<ts>.{json,md}

# 4) 向量索引（需要 Qdrant + 本地 bge-m3）
docker run -d --name asuka-qdrant -p 6333:6333 -v asuka-qdrant-data:/qdrant/storage qdrant/qdrant:latest
export ASUKA_EMBED_MODEL_PATH=.asuka-models/bge-m3

# 4a) 先自检 —— 3 秒，在建 428 条索引之前把"权重坏了/加载成别的模型"说清楚
"$PY_HEAVY" -m asuka.embedding --embedder local

"$PY_HEAVY" -m asuka.index redis --recreate --embedder local   # 建索引
"$PY_HEAVY" -m asuka.index redis --query "how do I set a TTL on a key"
"$PY_HEAVY" -m asuka.index redis --stats             # 现状 + 语料是否比索引新

# 5) 用向量检索评测，与 BM25 对照
"$PY_HEAVY" -m asuka.evaluate redis --retriever dense --embedder local --top-k 10

# 6) 并排对照（**先验可比性**，不可比就拒绝出表）
"$PY_HEAVY" -m asuka.compare \
    asuka/runs/retrieval/redis-bm25-<ts>.json \
    asuka/runs/retrieval/redis-dense-<ts>.json \
    --out asuka/runs/retrieval/compare-<ts>.md

# 7) 答案级指标（规则式必答要点召回 + pass@k + 引用核对 + 上下文装配）
"$PY_HEAVY" -m asuka.answers redis --answerer oracle     --retriever bm25 --top-k 10 --samples 1
"$PY_HEAVY" -m asuka.answers redis --answerer null       --retriever bm25 --top-k 10 --samples 1
"$PY_HEAVY" -m asuka.answers redis --answerer fabricator --retriever bm25 --top-k 10 --samples 1
#   → asuka/runs/answers/redis-<answerer>-<retriever>-k<n>-<ts>.{json,md}
#   `oracle` 必须 1.0、`null` 必须 0.0 —— 这两个数是**判据的上下界**，不是模型成绩。
#   `fabricator` 专门去踩"引用了没给它的来源"，证明**编造探测器会响**。

# 7b) **真模型**：DeepSeek（需要 key；Token/Cost/Latency 这下是模型侧的了）
export DEEPSEEK_API_KEY="sk-..."            # 没有就拒绝出表（退出码 2，提示里点名要设这个变量）
"$PY_HEAVY" -m asuka.answers redis --answerer deepseek --retriever bm25 --top-k 10 --samples 3
#   ⚠️ deepseek 会**真实调用** API（每个问题 × 采样次数一次）。报告里标「模型成绩」，
#   不标「校准」。单价默认 deepseek-chat 公开价，可经 DEEPSEEK_INPUT_COST_PER_MILLION /
#   DEEPSEEK_OUTPUT_COST_PER_MILLION 覆盖；base_url 经 DEEPSEEK_BASE_URL 覆盖（自建网关）。

# 7a) 把窗口调小 —— 看"检到了但装不进预算"这条归因真的会动
"$PY_HEAVY" -m asuka.answers redis --answerer oracle --retriever bm25 --top-k 10 \
    --context-budget 400 --reserved-for-output 0
#   实测（与上面同 top_k，只改窗口）：依据召回 0.5941 → 0.3229，
#   报告里多出"检到了但装不进预算 20 条"这一行，而"检索没检到 49 条"**不动**。

# 8) Trace：跑一条**可审计**的 Run，并摊开某一题的过程
"$PY_HEAVY" -m asuka.trace redis --retriever bm25 --answerer oracle --top-k 10 \
    --explain r-hard-05
#   → asuka/runs/traces/…-<ts>.jsonl            （逐步过程）
#   → asuka/runs/traces/…-explain-r-hard-05.md  （这一题的审计视图）
#   落盘前会拿**另一条路径**重算报告的聚合值，对不上就不写。
#   装配参数（窗口 / 预留 / chars-per-token）也在同一组命令行开关上 ——
#   它是"这次拿什么模型跑"的属性，不是常量。

# 9) 回归对比：这次 vs 上次（**先验可比性**，不可比就拒绝）
"$PY_UNIT" -m asuka.regression \
    asuka/runs/answers/<上次>.json asuka/runs/answers/<这次>.json \
    --out asuka/runs/regressions/<名字>.md --json asuka/runs/regressions/<名字>.json
#   退出码：0 可比 / 2 不可比（**stdout 一个数字都不给**，拒绝理由走 stderr）/ 3 读不回来
#   判的是"上次过、这次不过"，**不是**绝对通过率。见下面《回归对比》一节。
```

模型权重见 [`models/README.md`](models/README.md)（含分片下载脚本与三个环境陷阱）。

⚠️ `--embedder local` 不能省：`auto` 需要 `ASUKA_EMBED_API_KEY`，**没有就报错，
不会静默换人**。本地权重这条路必须被显式选中。

## 模块

| 文件 | 职责 | 第三方依赖 |
|---|---|---|
| `textutil.py` | 文本原语：原子块、代码围栏/表格保护、装箱；`estimate_tokens` **委托内核 tokenizer** | 无 |
| `splitters.py` | 切分：LangChain header+recursive；零依赖兜底 `_split_stdlib` | 可选 |
| `corpus.py` | 抓取 → 归一化 → 结构化 → 切分 → manifest | 无 |
| `embedding.py` | embedder 协议 + 身份自述；API / 本地 bge-m3 / 测试用 hashing | 可选 |
| `vectorstore.py` | Qdrant 薄封装；collection 名带模型与维度 | 可选 |
| `kb.py` | BM25 词法基线 + Qdrant dense + 权限过滤（复用 AgentOS 管线） | 无 |
| `dataset.py` | 任务集 schema + 校验（**一次报全部问题**）；必答要点 `RequiredPoint` | 无 |
| `datasets/` | 具体题目（人工整理，ground truth 来自官方文档） | 无 |
| `evaluate.py` | 检索指标 + 报告；`recall` 上限一并报出；报告读回时**自洽校验** | 无 |
| `answers.py` | 答案级指标：规则式必答要点召回 + `pass@k` + **引用核对** + 延迟/Token/成本测量位 | 无 |
| `deepseek.py` | **真模型答案器**：接 DeepSeek（OpenAI 兼容）；引用自述 + 代价真测；零第三方（标准库 `urllib`） | 无 |
| `context.py` | **装配**：`top_k` 之后再过一道 token 预算（C-1/C-3/C-4），复用内核 `agent_context` | 无 |
| `trace.py` | 一条 Run 的逐步记录（**可审计**）+ 与报告的交叉核对（含引用归因、装配参数） | 无 |
| `compare.py` | 并排对照多份报告；**先验可比性，不可比就拒绝出表** | 无 |
| `regression.py` | **这次 vs 上次**：复用 `agent_evaluation.regression` 的判定，外加可比性这道门 | 无 |
| `index.py` | 索引 CLI | 可选 |

## 五条设计决定（每条都有实证，不是风格偏好）

### 1. 用官方 Markdown，不写 HTML 清洗器

redis.io 为 AI agent 直接提供每页 Markdown（`<link rel="alternate" type="text/markdown">`），
带 ```` ```json metadata ```` 块（syntax / complexity / group / since / tableOfContents）。
**少一层启发式，就少一层错误。**

### 2. 归一化必须在**切分之前**

14/20 份 redis 文档带页面模板样板块 `## Code Examples Legend`，它把文档**无标题的概述**
拖进自己的标题路径 ⇒ citation 变成 `Redis · EXPIRE · Code Examples Legend`，
而内容是 "Set a timeout on `key`..."。

⚠️ **这不是 LangChain 的 bug** —— 它按标题切、把标题后内容归给该标题，**行为正确**。
错的是模板结构。所以修在切分之前，且移除量要记账（`legend_removed_chars`）。

⇒ **citation 说谎，Citation 指标就废了。** 实测：移除 13832 字符 / 14 份文档。

### 3. 标题层级是**语料的属性**，不是库的默认值

redis.io 的代码示例是 `##### <语言名>` 划的 codetabs。该层级不在 h1/h2/h3 里时，
**35% 的 Examples chunk 混了 ≥2 种语言**（`redis:set:008 = ['C#','Go']`）。

A/B 实测（只换 headers）：混语言 **35% → 0%**，oversized 11 → 9，代价 chunks 355 → 408。
⇒ 写进 `registry/redis.json` 的 `headers` 字段；库的 `DEFAULT_HEADERS` 仍是社区标准的 `spec`。

### 4. 代码围栏与表格**绝不跨切**

社区结论：*"A table that crosses a chunk boundary is useless to retrieve."*

`protect_atoms()` 把原子块换成占位符 → 交给 splitter → `restore_atoms()` 还原。
但保护层有副作用：LangChain 看不见代码块的真实体积（一个 6KB 的块在它眼里只有 20 字符）
⇒ 还原后必须再收敛（`fit_blocks` + 按空行分段）。
实测 p90 从 4211 字符降到 974。**策略归 LangChain，原子性归这一层。**

### 5. embedding 不许静默换人

向量库里躺着 1024 维的 bge-m3 向量，拿另一个 1024 维模型来查 ——
**维度一样、语义不同、结果全是垃圾、一声不响**。

⇒ 每个 embedder 自述 `name@dim`（`EmbedderInfo.signature`），
collection 名带模型与维度（换模型 = 换一格，可直接 A/B），
`vectorstore.json` 记下身份，查询时对不上就**拒绝**。
`build_embedder('auto')` 没配 key 时**报错，不静默降级到 hashing**。

## 评测口径（先看上限，再看数字）

```
context_recall      = |retrieved ∩ evidence| / |evidence|
context_precision   = |retrieved ∩ evidence| / |retrieved|
hit_rate@k          至少命中一处 evidence 的题目占比
MRR                 首个命中位置的倒数均值
```

⚠️ **`context_recall` 有理论上限**，报告里会一并给出：

```
上限 = mean( min(|evidence|, top_k) / |evidence| )
```

一道题声明 7 处 evidence 而 `top_k=5`，上限就是 5/7 = 0.71 ——
**无论检索多好都到不了 1.0**。不报上限，会把"上限 0.71、实际 0.44"
误读成"检索很差"，而真相可能是"已接近满分"。

### 对照之前先验可比性

`asuka.compare` 会**拒绝**四种对照，并说清是哪一件：

| 拒绝的理由 | 为什么 |
|---|---|
| `top_k` 不同 | recall 的**上限**依赖 `top_k`，上限不同的分数并排会被误读成能力差 |
| 题目集合不同 | 均值是"对**这组题**求的"，换一组题就不可比 |
| 每题 `evidence` 不同 | ground truth 变了，那是在比两套标注 |
| 一边是冒烟（`embedder_semantic=False`） | 拿噪声当基线 |

> **一个不可比的对照表比没有对照表更糟 —— 它看起来是结论。**
> 所以拒绝时输出里**一个数字都不出现**（有测试钉住这条）。

### 报告读回时会自洽校验

`recall` / `precision` / `mrr` / 上限都是**派生量**：`load()` 按
`evidence` / `retrieved` / `hits` **重算**，并核对文件里存的那份。

这不是洁癖 —— 上线就抓到了真东西：一份报告里 `context_recall_ceiling` 存的是
`1.0000`，重算是 `0.9568`，它写于**加上限之前的版本**。
拿这种报告和新报告并排比，比出来的差**全是指标定义的差，不是检索的差**。

### 缺键 ≠ 空值：两道"宁可拒绝"的门

后加的字段会带来一个隐蔽的坑：旧报告里**没有这个键**，`d.get(..., ())` 读成空，
而**空在读起来就是"没有"**。所以两道门都是直接拒绝：

| 门 | 拒绝什么 | 按空读会怎样 |
|---|---|---|
| `out_of_corpus` 缺键 | 旧格式报告 | 归因**说反了**：把"语料本来就不够"的题归到"该去查检索" |
| `items` / `retriever` 缺键 | `compare` 的输出（**对照表**，不是报告） | 两份对照表能互相"对照"出一张**全是 0 的表**，而它看起来是结论 |

> 重跑一次的成本，远低于一次错误归因。

## 答案级指标：规则式判据，不是 LLM-as-Judge

六个指标里，这一项是唯一**需要判断**的（Retrieval / Citation / Latency / Token / Cost
都是测量或纯计算）。

**主判据不用 LLM-as-Judge**：社区实测它有"**偏爱长输出**"的偏见 ——
写得长更容易被判对，等于把"啰嗦"变成了得分项。

所以每道题在 `TaskItem.required_points` 里声明**必答要点**，判"答到了几条"：

```python
RequiredPoint("-2 = key 不存在", any_of=("returns -2 if the key does not exist", "-2 if the key does not exist"))
```

判据的三条规矩，每条都是被实际缺陷逼出来的：

| 规矩 | 为什么 |
|---|---|
| 一条要点可以有**多个说法**（`any_of`） | 写死一个字符串，会把"答对了但换了措辞"判成错 —— 分数反映的是措辞像不像我 |
| 单词短语必须**词边界**匹配 | 否则要点 `set` 会在 `subset` 里命中 —— 假命中让分数**虚高**，比漏判更危险 |
| 匹配前剥掉 markdown 标记 | 参考答案是带 `**bold**` / `` `code` `` 的，答案里通常没有；不剥掉，声明对了也匹配不上，**且失败静默** |

**要点声明本身可以被证伪**（`Dataset.validate`，一次报全部）：

> 一条要点如果**参考答案自己都答不到**，它是错的声明 —— 不是"这题难"。
> 它会让这题**永久**低分，而读者会把它读成"模型不行"。

Redis 24 题共 **97 条**要点，全部通过这道证伪。

### `pass@1` 与 `pass@k` 必须成对看

```
pass@k = 1 - C(n-c, k) / C(n, k)      # n 次采样里 c 次成功
```

通过的定义是**全部要点都答到**（`recall == 1.0`）。不用"recall ≥ 阈值"，
因为阈值是个自由旋钮，而旋钮会被调到来凑结论；梯度信息由 `mean_recall` 提供。

`pass@k` 衡量"能不能做到"，`pass@1` 衡量"能不能稳定做到"，**差值本身就是信息**。

### 判据的已知缺口，用**度量**补，不用第二个判据补

判据是要点召回 ⇒ **把整篇文档抄进答案也会得高分**。这是故意的取舍。
报告里同时给出 `chars_per_hit_point`（每答到一个要点花了多少字符）作为**信号** ——
实测：参考答案 **104.5**，同一题抄满填充文本是 **786**。

试过加 `must_not_include`（"答案里不该出现的说法"）来补精确度，**又撤掉了**：
自由文本里否定句会让子串匹配**反向命中**（正确答案写"并不返回 -1 …"会被判成答错）。
**一个会误判的字段比没有更糟**，而且它声明了却没有可靠的 producer。

### `oracle` / `null` / `fabricator`：这是**校准**，不是成绩

没有 LLM API key 时，答案级指标仍然要能被验证 —— 否则它只是没人跑过的代码。
**真模型**走 `--answerer deepseek`（见下），它自述 `is_calibration=False`，报告里标「模型成绩」。

| answerer | 行为 | 必须 |
|---|---|---|
| `oracle` | 直接返回参考答案 | 要点召回 **1.0**（不是 1.0 ⇒ 声明写错了） |
| `null` | 返回空串 | 要点召回 **0.0**（不是 0 ⇒ 判据在送分） |
| `fabricator` | 答得像样但引**编的** id | `grounded` 必须 0.0、且被点名 |

`is_calibration` 必须由 answerer **自述**，不说的会被 `evaluate_answers` **拒绝** ——
默认当成"真模型"是最坏的选择：校准分数会被读成模型成绩，而报告里没有任何东西提醒你。
报告最上面会印 **`⚠️ 这是校准跑，不是模型成绩`**（deepseek 那栏印「模型成绩」）。

⚠️ `oracle` **不能**证明"要点覆盖了参考答案的全部含义"—— 那需要裁判模型（见 M12）。
它能证明的是"每条要点的说法确实能在参考答案里找到"。

### 判据必须能区分，否则它只是装饰

用几条"像模型写的但浅"的答案探过：

| 答案 | recall |
|---|---|
| 把 TTL 的 -1 / -2 混成一句（最典型的错） | **0.33** |
| "SADD returns an integer"（没说返回的是**新增**数） | **0.00** |
| rate limiter 只说 INCR + EXPIRE（没提顺序陷阱 / 竞态 / 边界） | **0.20** |

## 引用（Citation）：答对了 ≠ 知识库起作用了

要点召回只回答"答到了几条"，不回答"它是**凭什么**答的"。
一个靠参数记忆答对的模型，和一个真读了语料答对的模型，在要点召回上**一模一样**。
对企业知识库来说这两者完全不同 —— 前者意味着**知识库根本没起作用**。

所以 `Answer.citations` 是答案器必须**自述**的一项：它用了哪几个 `chunk_id`。

```
Answer(text=..., citations=("redis:expire:037", "redis:ttl:002"))
```

⚠️ 自述**不是**信任 —— 它立刻被拿去和两样东西核对：

| | 是什么 | 从哪来 |
|---|---|---|
| `available` | **真正喂进 prompt** 的上下文 | 装配之后的 id（⚠️ **不含**被权限拒绝的，也**不含**被预算丢掉的） |
| `evidence` | 这题声明该引的依据 | `dataset.resolved` |

于是有三条**互不替代**的结论：

| 指标 | 定义 | 回答什么 |
|---|---|---|
| `fabricated` | `cited − available` | 引了**没给它**的东西 ⇒ **编造**。硬判据，点名。 |
| `grounded` | `1 − |fabricated| / |cited|` | 引用有没有依据（< 1 就是有编的） |
| `依据召回` | `|cited ∩ evidence| / |evidence|` | 该引的依据引到没有（端到端） |
| `依据用上率` | `|cited ∩ evidence| / |cited ∩ evidence ⊎ ignored|` | **纯生成侧**：给了它的引了几成 |

`依据召回` **单独看会误判** —— 它同时受检索、预算、生成影响。所以缺的依据被拆成三份：

```
evidence_not_retrieved = evidence − retrieved               ⇒ 检索根本没检到，换检索器
evidence_dropped       = (evidence ∩ retrieved) − available ⇒ 检到了但装不进预算，加窗口
evidence_ignored       = (evidence ∩ available) − cited     ⇒ 给了它却没引，改 prompt
```

实测（`bm25` `top_k=5`，88 条 ground-truth 依据）：**62 条检索没检到，26 条给了**。

| answerer | grounded | 依据召回 | 依据用上率 | 检索没检到 | 装不下 | 给了没引 |
|---|---|---|---|---|---|---|
| `oracle` | 1.0000 | 0.4424 | **1.0000** | 62 | 0 | **0** |
| `null` | — | 0.0000 | 0.0000 | 62 | 0 | 26 |
| `fabricator` | **0.0000** | 0.0000 | 0.0000 | 62 | 0 | 26 |

`oracle` 那一行就是这张表的意义：**生成侧满分（给了的全引了），缺口 100% 在检索**。
`依据召回 0.4424` 与检索报告的 `context_recall` **逐位相同** —— 这不是巧合，
是刻意钉的：`oracle` 引 `contexts` 而不是引 ground truth，所以它的端到端依据召回
就等于检索上限；两个数**同口径**（逐样本求均值），**同一个量只有一处定义**。

把窗口压小之后"装不下"那一栏才开始动 —— 见下面的「上下文装配」一节
（⚠️ 那张表是 `top_k=10` 的，**不要**和这张 `top_k=5` 的表混着读）。

### `None` 和 `()` 是两件事

| `cited` | 含义 | 三个比例 |
|---|---|---|
| `None` | **没自述** | 全是 `—`（**不可测**），这一题退出引用分母 |
| `()` | 明确说"没引用任何来源" | 可测：召回 0.0 |

混起来的话，"没测"会读成"答得没依据"，而报告里没有任何东西会提醒你。
`null` 校准器必须返回 `()` 而不是 `None` —— 否则下界从"引用召回 0"
退化成"引用不可测"，**校准会静默失效**。报告里 `—` 一律印成破折号，不印 `0.0000`。

### 为什么**不**报 `citation_precision`

`citation_precision` = 引的东西里有多少条属于 ground truth。
问题是 ground truth 是**最少必要依据**，不是**唯一允许引的依据** ——
引了别的真实上下文并不算错。用 precision 罚它等于**奖励"少引"**，
把一个好行为变成了扣分项。

### 为什么**不**再分"编造的 id 语料里到底有没有"

"语料里根本没有"（凭空编）和"语料里有但没检到"（在用记忆）诊断价值不同。
但要分就得把整份语料的 id 传进来，而**传不进来时静默降级**会让这一栏
读起来像"没有这类问题"。**一个会静默降级的判据，比没有更危险。**

### 编造探测器必须被证明会响

`oracle`（全对）和 `null`（全空）都碰不到"引了没给它的东西"这条路径。
没有 `fabricator`，`fabricated` 那一栏可能是**一段永远为空的代码**，
而报告看起来一切正常。所以有个假答案器专门去踩它 —— 它必须红。

## 上下文装配：`top_k` 是**条数**，不是**窗口占用**

`kb.search(limit=10)` 说的是"给我 10 片"。10 片是多少 token？不知道。
而模型的窗口是按 token 算的 —— 10 片长文档可以轻松超过 8192。

**一个按条数控制的检索器会静默地把请求撑爆**，而失败发生在**模型那一侧**
（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧：评测跑得好好的，线上全崩。

所以检索之后必须有一步 `asuka/context.py` 的装配，复用内核 `packages/agent_context`：

| 不变量 | 内容 | 内核落点 |
|---|---|---|
| **C-1** | `Chunk`（Knowledge）→ `ContextItem`（Context）**显式**转换 | `RetrievalPipeline.to_context_items()` |
| **C-3** | Token Budget 是**硬约束**：装不下就丢，不许超 | `budget.allocate()` |
| **C-4** | **静默截断是 bug**：每条被丢的都要留 `(chunk_id, tokens, 原因)` | `DroppedItem` |
| **C-10** | 进 Context 的知识片**必须带 citation** | `ContextItem.__post_init__` |

### 检到了 ≠ 模型看见了：两个集合

```
retrieved   检索管线留下的（过了 C-9 权限 + C-10 citation）
available   真正进 prompt 的（再过一道 C-3 token 预算）⊆ retrieved
```

混成一个的话，"被预算丢掉的片"会被读成"模型见过它" ⇒ 引用判据里的 `available`
变大 ⇒ **编造被读成有依据**。这个方向**只会让分数变好看**，没有任何东西会报错。
所以 trace 里两个字段名不同、含义不同，引用判据用的是后者。

于是"缺的依据"从两段变**三段**（修法完全不同，合成一段会把人指向错误的地方）：

| 归因 | 条件 | 该动什么 |
|---|---|---|
| 检索**根本没检到** | `evidence − retrieved` | 换检索器 / 扩语料 |
| 检到了但**装不进预算** | `(evidence ∩ retrieved) − available` | 加窗口 / 降 top_k |
| 给了它却**没引** | `(evidence ∩ available) − cited` | 改 prompt / 换模型 |

实测（`bm25` `top_k=10`，同一批题只改窗口）：

| 窗口 | 实际占用 | 预算丢掉 | 依据召回 | 检索没检到 | 装不下 | 给了没引 |
|---|---|---|---|---|---|---|
| 8192（默认） | 1479 / 7168 tokens（10.0 片） | 0 片 | **0.5941** | 49 | 0 | 0 |
| **400** | 365 / 400 tokens（2.9 片） | 170 片 / 24 题 | **0.3229** | 49 | **20** | 0 |

⚠️ 注意**"检索没检到"那一栏两边都是 49** —— 换窗口不改检索结果。
这个不变本身就是归因正确性的证据：两栏在动的是**不同**的东西。
分数低看起来像检索差，实际是**配置**不合适 —— 第二行和第一行必须分开，
不然人会去改一个没坏的东西。

### 取舍顺序按**相关性**，不按 `chunk_id` 字母序

`allocate()` 的取舍顺序是 `sorted(items, key=(not pinned, -priority, key))`。
内核 `knowledge_chunk()` 给所有知识片的 `priority` 是**同一个常数** 30，
于是"装不下先丢谁"退化成按 `chunk_id` 的**字母序** —— 而 RAG 里该丢谁必须由
**相关性**决定。字母序丢掉第一名、留下最后一名，是**静默**的：分数照出，只是低了一点。

`priority` 的语义就是"越大越重要；只在取舍时用"，所以装配时把**检索名次**写进去 ——
**用**内核给的旋钮，不是绕过它。

⚠️ `allocate()` 是**贪心"塞得下就放"**：名次 1 装不下时会被丢掉、名次 2 顶上。
这是**对**的（宁可整片丢，不截断），但读结果时要记得：
**`context_size < top_k` 不等于"检索少检了"** —— 去看报告里那一栏"装不下"。

### `chars_per_token` 是**语料的属性**，必须显式声明并印出来

同一段文本，`chars_per_token=4`（英文经验值）和 `3`（中文保守）差 **33%**。
两处各写一个数字 ⇒ 同一个 chunk 在语料清单里和在装配时算出的 token 数不一样，
而**两边都不会报错**。所以全包只认一个常量（`textutil.CHARS_PER_TOKEN`），
`estimate_tokens` **委托**内核 `HeuristicTokenizer`（不自己再写一遍），并且它**进报告**：
"这个分母是谁划的"必须能被读出来。

### 为什么不直接用内核的 `ContextAssembler`

它要 `run_id`，并把 `ContextSnapshot` 存进 `ContextSnapshotStore`（C-2：一次调用一份快照）。
Asuka 的审计凭证是 `asuka.trace`；而且评测里的 "run" 是 `task_id × 采样序号`，
**不是** AgentOS 的 `AgentRun`。把两套 run 语义缝在一起，比各管各的更危险。

## Trace：报告回答不了的那半个问题

评测报告回答"这组题整体怎么样"。但最常被问的是另一个问题：

> 这道题答错了 —— 是**没检到该检的**，还是**检到了没用上**？

报告里的 `context_recall` 和要点召回是**分别聚合**的，对着一道具体的题答不上来。
所以有了 trace：一条 Run 的**逐步过程**（问了什么 → 检到哪几片 → 答了什么 →
判成答到哪几条、**凭什么**）。

```
run.started → retrieval → generation → scoring → … → run.finished
```

`--explain <task_id>` 把某一题摊开成审计视图，实测输出（`r-hard-05`，`bm25`）：

```
## 检索
- 检到 10 片（拒绝 0 · 无 citation 丢弃 0），耗时 0.2 ms
  1. `redis:set:024`  2. `redis:expire:037`  … 10. `redis:brpop:001`
## 生成
- `oracle` · 610 字符 · 0.0 ms · token 0+0 · $0.0000
- 自述引用 5 条：['redis:set:024', 'redis:expire:037', …]
## 判分
- 要点 5/5
  - ✅ 极大基数应换概率结构（HyperLogLog）—— 凭『hyperloglog』判为答到
- ⚠️ 这题**声明了语料缺口** —— 有些要点语料里根本没有，低分不该全记在生成头上
### 引用
- 没有编造引用
- 该引但**检索根本没检到**（记检索头上）：['redis:sadd:001', 'redis:sadd:018', …]
```

一眼就能看出：**检到的 10 片里没有一片来自 `SADD`**，而答案要求的正是 `SADD` 那条路径。
现在这句话还带上了**归因**：缺的 4 条依据落在"检索没检到"这一栏，不是"给了它没用"。

检索那一段同时列出 `kept` 与 `context`，被预算丢掉的那些会带一个显式标记：

```
## 检索
- 检到 2 片（拒绝 0 · 无 citation 丢弃 0），耗时 0.3 ms
  1. `redis:expire:000`  ← **被预算丢掉，模型没看见**
  2. `redis:ttl:000`
- **装配后喂进 prompt** 1 片 / 9 tokens；预算丢掉 1 片
```

只列 `kept` 的话，被丢掉的那些读起来像"给它了"。

### 五条纪律（每条都对应一个"读起来像没事"的失效）

| 纪律 | 失效长什么样 |
|---|---|
| 事件种类是**闭集**，未知值拒绝 | 兜底成"其它"⇒ 新事件静默地不被处理，而 trace 看起来是完整的 |
| `seq` 必须从 0 **连续** | 缺号 ⇒ 事件丢了，而"少了几个事件"读起来像"本来就没发生" |
| 首尾必须是 `run.started` / `run.finished` | **半截 trace 比没有 trace 更危险** —— 它看起来是一次完整运行 |
| 必须带上**输入的同一性**（语料/任务集 `sha256`） | 语料换了、题改了、embedder 换了，同样的分数含义完全不同 |
| **必填字段的值可以是 `null`，但键不能缺** | `.get("citations", ())` 把"旧格式缺字段"和"答案器没自述"读成同一个东西 |

⚠️ **装配参数也是输入的同一性**：`context_budget` / `reserved_for_output` /
`chars_per_token` 都在 `run.started` 里。"丢了 3 片"脱离"窗口多大"无法解释；
两条窗口不同的 trace 放一起比引用指标就是在比两件事。
所以 `verify_against` 会拿报告的这三个字段和 identity 对账 —— 对不上就不写。

⚠️ trace **只记 `chunk_id`，不复制正文**（正文在语料里，按 id 查得到；复制会产生第二个真相源），
但**记答案全文** —— 审计最常问的就是"它到底答了什么"，几十 KB 不值得为省这点空间丢掉可审计性。

### 三条路径算同一个数，必须对得上

| 核对 | 两条路径 | 对不上说明 |
|---|---|---|
| `verify_against(report)` | 报告的聚合值（从 `AnswerScore` 对象算） vs trace 的 `scoring` 事件（从**序列化后的 JSON** 算） | 其中一条坏了 |
| `verify_against` 的**引用**那一段 | 报告里的 `fabricated`（由 `score_citations` 写） vs **`generation.citations − retrieval.context` 对减**（不碰 `score_citations`） | 两条路径都调同一个函数的话，这个核对只是在证明"我等于我自己" |
| `verify_retrieval_against(report)` | trace 的 `kept` vs 检索报告的 `retrieved`（**两条命令**分别产出的） | 它们**不是同一次检索**，放一起读就是把两件事当一件事 |

⚠️ 引用那一段对减的右边是 `context`（**喂进 prompt 的**）不是 `kept`（**检到的**）。
被预算丢掉的片模型没看见，引用了它就是编造 —— 用 `kept` 会把编造读成有依据，
而这个方向**只会让分数变好看**，没有任何东西会报错。

CLI 在落盘**之前**跑第一道核对 —— 对不上就不写。

⚠️ 它证明不了的事也要说清：`pass@k` 的 `k` 两条路径都是**从外面读进来的**，
所以这个核对只证明"同一份 k 下两条路径一致"，不证明 `k` 对。
`k` 与逐题样本条数的对账在 `AnswerReport._verify_aggregates` 里做。

### 为 LLM 步骤预留的位置

`generation` 事件里现在装的是 `oracle` / `null` / `fabricator`。接入真模型时装的是**一样的字段**
（`text` / `latency_ms` / `prompt_tokens` / `completion_tokens` / `cost_usd` / `error` / `citations`），
其余全链路不用改 —— 只需要写一个实现 `Answerer` 协议的类：

```python
class MyLLM:
    name = "gpt-x"
    is_calibration = False          # ⚠️ 必须自述，否则被拒
    def answer(self, item, contexts) -> Answer:
        ...                          # 失败时填 Answer.error，别用空文本冒充"答错"
        return Answer(text=..., citations=(...))   # ⚠️ 引用要自述；不填 = 不可测
```

## 回归对比：这次 vs 上次

一份报告的**绝对通过率**说明不了什么 —— 一条用例这次通过了，它可能一直都通过。
真正要报警的是**它上次通过、这次不通过了**。

判定规则**不住在 Asuka 里**，住在 `packages/agent_evaluation/regression.py`
（AgentOS 那边本来就有，只是从来没人接上）：

| 类别 | 含义 | 要不要报警 |
|---|---|---|
| `regressed` | 上次通过 → 这次不通过 | **唯一必须报警的一类** |
| `improved` | 上次不通过 → 这次通过 | 不用 |
| `unchanged` | 两次一样（**含"两次都不通过"**） | 不用 |
| `new` | 基线里没有这道题 | 不用 |

⚠️ **"两次都不通过"归 `unchanged` 是刻意的**：一条一直失败的用例是 **backlog**，
不是回归。把它报成回归，回归信号会淹没在噪声里 ——
那正是"每轮 3 条红、其实 0 个新问题"这种疲惫感的来源。
所以报告里 backlog 单独成节，标题直接写着**不是这次退步**。

### Asuka 在外面加的两层

**一、题级口径：通过 = 这道题的**全部采样**都通过**

`AnswerScore.passed` 是**一条样本**的判定；回归对比是**一道题对一道题**。
`samples_per_task > 1` 时两者不是一回事，所以口径写死、并印在报告第一段：

```
每题通过 ⟺ 这道题 pass@1 == 1.0（全部采样都过）
```

不用"至少一次通过"：那个口径下 `3/3 → 1/3` 会被读成 `unchanged` ——
一道正在塌的题被记成"没事"。反过来，本口径下**一次采样翻转就算回归**，
所以报告里**必须**同时印前后比例（`2/3 → 3/3` 与 `0/3 → 3/3` 是两回事）：
**判定给结论，比例给分辨力。**

⚠️ 为什么**不**逐样本对：两次跑的第 2 次采样**不是同一个东西** ——
采样没有种子，`samples_per_task=3` 的两次运行之间没有可对齐的样本身份。
拿位置当身份，等于编一个不存在的对应关系。

**二、不可测的题不进对比**

`points_total == 0` ⇒ `AnswerScore.passed` 恒为 `False`。直接拿去比的话，
一道**没测**的题会以"两次都不通过"的形状落进 `unchanged` ——
读起来是"已知问题"，真相是"压根没测"。所以它们被排除，并在报告里单独点名。

### 可比性：配置变了就不是"这次 vs 上次"

和 `compare.py` 同一道门（**不可比的对照表比没有对照表更糟**，因为它看起来是结论）。
**一次报全部原因**，不只报第一个 —— 只报第一个的话，人会改一项再跑一次，
那会把人训练成"多跑几次"，而不是"看一次报告"。

| 不一致的字段 | 为什么不可比 |
|---|---|
| `topic` | 不同的语料 |
| `retriever` / `answerer` | 换了被测系统 ⇒ "模型 A 换模型 B"不是回归 |
| `top_k` | 改它等于改检索范围，两次看到的上下文不是同一批 |
| `samples_per_task` | **判定口径依赖它**（全部采样通过），换了口径就换了结论 |
| `context_budget` / `reserved_for_output` | 窗口变了 ⇒ 喂进 prompt 的那一份变了 |
| `chars_per_token` | 它是**语料的属性**，变了说明语料或假设变了 |
| `corpus_chunks` | 语料变了 ⇒ 检索的宇宙变了 |
| `calibration` | 拿判据校准和模型成绩比，比出来的差是指标的差 |
| 逐题 `question` | 同一个题号下是两道不同的题 —— `compare()` **看不见**这一层 |
| 逐题 `points_total` | "通过"的含义变了（原来是全中这一组，现在是全中另一组） |

⚠️ **已知缺口**：`corpus_chunks` 只是个**计数**，不是指纹 ——
语料重新切分后条数可能恰好不变，而内容全变了。
所以"它相等"**不等于**"语料同源"。这一条写在报告和模块 docstring 里，
不假装它被守住了（要真判同源得存语料哈希，那是另一次报告格式变更）。

### 拒绝时**一个指标数字都不印**

拒绝理由走 stderr、退出码 2、stdout 保持空。
但 `--out` / `--json` **照写** —— 这一点和 `compare.py` 不同：
`--out` 是**显式给的** flag，静默忽略它属于"承诺了却没交付"，
而且"这次为什么不能比"本身就是该留档的结论。

### 判定之外的另一个维度：指标漂移

报告最后一张表把 `要点召回` / `依据召回` / 延迟 / token 前后并排。
它**不参与** `regressed` / `improved` 的判定 ——
一道题可以两次都通过，而它的 `依据召回` 掉了。那种"分数低了一点"
正是最容易被放过去的一类。

`None`（不可测）一律印 `—`，不印 `0`：`— → 0.5941` 读起来是"从 0 涨上来了"，
而真相是"上次没测"。两边都不可测时**连方向都不该判**。

### 实测（Redis 语料，同配置两次 `null` 跑）

`null` 全答空串 ⇒ 24 题两次都不通过：

| 类别 | 题数 |
|---|---|
| **regressed** | **0** |
| improved | 0 |
| unchanged（**全是 backlog**） | 24 |
| new | 0 |

报告里 24 道题全部落在《已知问题 —— **两次都不通过，是 backlog，不是这次退步**》一节，
而《回归》一节印的是 **「没有回归。」** —— 并且紧跟一句
**「这句话不等于『一切都好』」**：`unchanged` 里混着"两次都过"和"两次都不过"两种东西，
不分开说，那个 0 就会被读成一个让人安心的句号。

对照：`oracle`（8192 窗口）vs `oracle`（400 窗口）**被拒绝**，
理由是 `context_budget` 与 `reserved_for_output` 两项不一致 ——
换窗口改的是"喂进 prompt 的那一份"，不是系统退步。

## 实测结果（Redis 语料 428 chunks / 24 题）

BM25 vs 本地 bge-m3（`BAAI/bge-m3@1024`，Qdrant 1.19.1，CPU）：

| top_k | 检索器 | recall | 上限 | precision | hit_rate | MRR | 平均耗时 |
|---|---|---|---|---|---|---|---|
| 5 | `bm25` | 0.4424 | 0.9568 | 0.2167 | 0.7083 | 0.5153 | **0.3 ms** |
| 5 | `dense` | **0.5410** | 0.9568 | **0.2750** | **0.8333** | **0.5917** | 673 ms |
| 10 | `bm25` | 0.5941 | 0.9931 | 0.1625 | 0.7917 | 0.5274 | **0.2 ms** |
| 10 | `dense` | **0.6594** | 0.9931 | **0.1917** | **0.9167** | **0.6046** | 651 ms |

**dense 在质量上全面胜出，但代价是 ~2000× 的延迟**（673 ms vs 0.3 ms 每条查询）。
这个数量级差不该被"dense 更好"一句话盖过去 —— 24 题 × 单查询的规模下无所谓，
但它是**唯一**能解释"要不要上向量检索"的数字。

⚠️ **dense 不是均匀地更好**。`top_k=10` 时 **hard 档 BM25 反而更高**：

| top_k=10 分难度 | `bm25` recall / hit | `dense` recall / hit |
|---|---|---|
| simple | 0.9062 / 1.0000 | 0.9688 / 1.0000 |
| medium | 0.4688 / 0.6250 | **0.6542 / 1.0000** |
| hard | **0.4074** / 0.7500 | 0.3554 / 0.7500 |

dense 的优势几乎全在 medium（+0.185），hard 上反而输 0.052。
⇒ **按难度分层看，是这套评测存在的主要理由**：只看总分会把这两件事抵消掉。

⚠️ **两边都检不到的题**（`top_k=10`）：`r-hard-02`、`r-hard-05`。
这不是检索器的锅 —— **换谁都没用**，要去查语料里到底有没有这条答案
（可能该补文档，也可能该改标注）。`compare.py` 会把这类题单独列出来，
并把它和「语料覆盖不全」的声明**分开说**：

```
- **两边都检不到**（2）：['r-hard-02', 'r-hard-05']
  - 其中 1 道**同时**在「语料覆盖不全」名单里（['r-hard-05']）—— 就算检索修好了，答案级分数仍有天花板。
  - 剩下 1 道**没有**语料缺口声明（['r-hard-02']）—— 这才是该去查检索或标注的。
```

### 语料覆盖不全：这是**归属表**，不是待办

3 道题的参考答案里有**语料撑不住**的部分（`TaskItem.out_of_corpus`，人工声明）：

| 题 | 语料里完全没有 |
|---|---|
| `r-hard-04` | `ZRANK` / `ZREVRANK`（语料只有 `ZADD` / `ZRANGE`） |
| `r-hard-05` | `HyperLogLog`（`PFADD` / `PFCOUNT`） |
| `r-hard-08` | `HDEL` |

⚠️ 试过**自动扫**"参考答案里的命令名是否在语料里"：24 题只抓到 2 个，
**漏掉了 `r-hard-05`** —— 它缺的是 `HyperLogLog`，不是一个大写命令 token。
**一个会漏的检查器看起来权威，比没有更危险**，所以改成显式声明，
并给它一个**可证伪**的校验（声明点名的符号若语料里其实有，则报错）。

要真修，得先决定是**扩语料**还是**改题** —— 那是语料范围的决定，
不该由评测脚本替你下，所以报告里写成归属表而不是待办清单。

### 答案级校准（`oracle` / `null` / `fabricator`）

| answerer | 样本 | 要点召回 | pass@1 | 每要点字符 | grounded | 依据召回 | 依据用上率 | 编造 |
|---|---|---|---|---|---|---|---|---|
| `oracle` | 24 | **1.0000** | 1.0000 | 104.5 | **1.0000** | 0.4424 | **1.0000** | 0 |
| `null` | 24 | **0.0000** | 0.0000 | 0.0 | — | **0.0000** | **0.0000** | 0 |
| `fabricator` | 24 | 1.0000 | 1.0000 | 104.5 | **0.0000** | 0.0000 | 0.0000 | **48 条 / 24 题** |

这些数是**判据的上下界**，不是模型成绩。真模型成绩用 `--answerer deepseek` 跑
（DeepSeek，需要 `DEEPSEEK_API_KEY`；Token/Cost/Latency 那下是模型侧的了，不是填 0）。

`fabricator` 那一行是**自检**：它证明"引用了没给它的来源"这条路径真的会被抓到 ——
`oracle` 和 `null` 都碰不到它，没有它，`fabricated` 可能是一段永远为空的代码。

## 为什么 BM25 词法检索不是"妥协"而是"对照组"

这个平台的目的是**比较**：同一份语料、同一任务集，换检索器 / 换模型会怎样。

没有词法基线，"向量检索得了 0.72" 这个数字**无法解释** ——
它比随机好多少？比一个三十年前就成熟的算法好多少？
⇒ 词法基线是**评测平台必须有的那个分母**。

⚠️ 但 BM25 **必须去停用词**，这不是优化而是必要条件。
redis.io 有几节标题是问句形状的（"What key is served first? What client?..."），
于是 `what` 在这份语料里极其罕见 ⇒ idf 高达 **4.56**（比 `multi` 的 3.04 还高），
靠它拿了**过半的分**，真正该第一的 `multi:001` 掉到**第 7 名**。

失效模式是：**罕见的功能词冒充高信息量词**。去掉停用词后：
`recall 0.219→0.442`、`hit_rate 0.333→0.708`、`MRR 0.285→0.515`。

## 测试

```bash
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_corpus_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_kb_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_dataset_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_evaluate_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_compare_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_answers_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_citation_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_context_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_trace_contract
PYTHONPATH=. "$PY_UNIT" -m unittest tests.unit.test_asuka_regression_contract
```

用 `unittest`（不是 pytest）。**核心层零第三方依赖**，所以单测跑在零依赖的解释器上。
上面十条锁的是实证过的缺陷，不是推演出来的担心。当前 **1768 条全绿**。

新增判据一律做**变红验证**（把代码改坏，确认测试真的会红）——
没红过的测试不算测试。骨架在 `redkit.py`（锚点唯一性断言、残留防护、信号处理）：

```bash
python red94.py     # DeepSeek 真模型：7 条变异（生成失败文案 / cost 公式 / 序号引用 / 不可测 / 归一化 / 提示词 [[key]] / 缺 key 早退）
python red93.py     # 回归对比：27 条变异（口径 / 不可测 / 同源 / 可比性门 / 渲染 / CLI）
python red92.py     # Context 装配：18 条变异（C-1 / C-3 / C-4 / 取舍顺序）
python red91.py     # 引用指标：23 条变异
```

⚠️ **必须后台跑**：一轮 = 变异数 × 全量套件（约 10s），20 多条就是四五分钟。
前台被超时杀掉时 `finally` 不执行，变异会**留在源码里**，下一次的"基线"就把残留
当成了正常代码（真踩过）。`redkit.py` 启动时会比对备份与源码，不一致就**拒绝启动**
而不是猜 —— 更早几轮的记录在 `.workbuddy-ai/memory/2026-09-23.md`。

⚠️ **跑变红期间不许改被验证的源文件**。这一轮又踩了一次：跑到一半改了
`asuka/regression.py`，`redkit` 还原时按启动时抓的备份把改动**覆盖**掉了，
而且进程停在变异中间、残留留在了源码里。纪律是：**改完 → 冻结 → 再跑**；
跑完发现源码 ≠ 备份，按提示**删掉备份目录**重跑（不要自动还原，见 `redkit` 的说明）。
