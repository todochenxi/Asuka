# Asuka 答案级评测 · redis

- answerer：`deepseek`
- 检索器：`dense`（top_k=10）
- 采样：每题 3 次
- 语料：428 chunks
- 上下文窗口：8192 tokens（留 1024 给输出 ⇒ 可放 7168） · 每 token 4 字符
- 生成时间：2026-09-23T13:38:07+0800

## 总体

| 指标 | 值 |
|---|---|
| 必答要点召回（mean） | 0.2035 |
| pass@1（稳定做到） | 0.0000 |
| pass@3（能做到） | 0.0000 |
| 检索耗时（mean） | 619.3 ms |
| 生成耗时（mean） | 3341.8 ms |
| 合计耗时（mean） | 3961.2 ms |
| 上下文 token（mean） | 1614.4 |
| 上下文片数（mean） | 10.0 |
| 生成 token（mean，prompt+completion） | 2248.2 |
| 总 token | 161873 |
| 每答到一个要点的字符数 | 492.8 |

| 总成本 | $0.0546 |

⚠️ 有 **1** 次生成**报错**（不是答错）。见逐题明细的 `err` 列。

> `pass@k` 的通过定义是**全部要点都答到**（recall = 1.0），不用阈值 —— 阈值是个自由旋钮，会被调到来凑结论。梯度信息由第一行的 `mean_recall` 提供。

> ⚠️ `chars_per_hit_point` 是**已知缺口**的度量：判据是要点召回，所以把整篇文档抄进答案也会得高分。这个数越大越可疑 —— 但它只是**信号**，不是判据（要真判『答得冗不冗』需要裁判模型）。

## 上下文（Context）

- 窗口 8192 tokens，可放 **7168**（留给输出 1024）
- 实际占用 **1614.4** tokens（均值）· 10.0 片 · 共 116235 tokens
- 被预算丢掉 **0** 片，涉及 **0** 条样本

> ⚠️ `top_k` 是**条数**，不是窗口占用。检索之后必须有这一步装配：10 片长文档能撑爆 8k 窗口，而失败发生在**模型那一侧**（`CONTEXT_LENGTH_EXCEEDED`），不在评测这一侧 —— 评测跑得好好的，线上全崩。

> ⚠️ 取舍顺序按**相关性**（检索名次写进 `ContextItem.priority`）。不写的话内核的 `allocate` 会退化成按 `chunk_id` **字母序**丢 —— 丢掉第一名、留下最后一名，而且是**静默**的：分数照出，只是低了一点。

## 引用（Citation）

| 指标 | 值 |
|---|---|
| 引用有依据率 grounded | 1.0000 |
| 依据召回（端到端） | 0.6012 |
| 依据用上率（纯生成侧） | 0.8780 |
| 编造引用 | 0 条 / 0 题 |

> 三个数**各答一个问题，不能互相替代**：`grounded` = 引的东西**给它了吗**（< 1 就是编造）；`依据召回` = 该引的依据引到没有（⚠️ **同时受检索和生成影响**，单独看会误判）；`依据用上率` = **给了它的**依据它引了几成（纯生成侧，检索漏没漏与它无关 —— 检索没给的**不进这个分母**，否则检索越差它越高，方向就反了）。

> 三个数一律**逐样本求均值**，与检索报告的 `context_recall` 同口径 —— `oracle` 跑出来的 `依据召回` 就等于那份报告的 `context_recall`（它把拿到的依据一条不漏地引了）。**同一个量只有一处定义。**

> 为什么**不**报 `citation_precision`（引的东西里有多少条属于 ground truth）：ground truth 是**最少必要依据**，不是**唯一允许引的依据**。引了别的真实上下文不算错，用 precision 罚它等于**奖励『少引』**。

⚠️ 有 **1** 条样本**没有自述引用**，它们**不在**任何引用分母里（分母是 71 条）。『没自述』不等于『没引用』—— 所以这里既不算 0 也不算 1。

### 缺的依据归谁

| 归因 | 条数 | 该动什么 |
|---|---|---|
| 检索**根本没检到** | 126 | 换检索器 / 扩语料 |
| 检到了但**装不进预算** | 0 | 加窗口 / 降 top_k |
| 给了它却**没引** | 21 | 改 prompt / 换模型 |

> ⚠️ 这是**归因**，不是三个待办。`依据召回` 低时先看这三行再下结论 —— 不然『检索没检到』会被读成『模型没用依据』，改错地方。
> ⚠️ 第二行和第一行**必须分开**：『检到了但装不下』是这套评测里最容易误读的失败 —— 分数低看起来像检索差，实际是**配置**（窗口 / top_k）不合适。

## 分难度

| 难度 | 样本 | recall | pass@1 | pass@k | 平均 token |
|---|---|---|---|---|---|
| simple | 24 | 0.4438 | 0.0000 | 0.0000 | 2112.6 |
| medium | 24 | 0.1250 | 0.0000 | 0.0000 | 2174.3 |
| hard | 24 | 0.0417 | 0.0000 | 0.0000 | 2457.8 |

## 一个要点都没答到的题（13）

- `r-simple-03` (simple) What does TTL return when the key exists but has no expiration, and wh
  - 缺：-2 = key 不存在
  - 缺：-1 = key 在但无过期
  - 缺：否则返回剩余秒数
- `r-simple-06` (simple) How do the start and stop indexes of LRANGE behave, including negative
  - 缺：下标 0 基
  - 缺：负下标从尾部数，-1 是最后一个
  - 缺：越界不报错（空 / 截断）
- `r-medium-01` (medium) What is the difference between BLPOP and BRPOP?
  - 缺：都是阻塞弹出（等到有元素或超时）
  - 缺：BLPOP 从头部（左）
  - 缺：BRPOP 从尾部（右）
  - 缺：其余参数相同
- `r-medium-03` (medium) What is the difference between EXPIRE and TTL?
  - 缺：互为反向：EXPIRE 写、TTL 读
  - 缺：EXPIRE 返回 1 / 0
  - 缺：TTL 返回剩余秒数；-1 无过期、-2 key 不存在
  - 缺：TTL 不是通用的 key 存在性检测
- `r-medium-05` (medium) When should you use HSET/HGET instead of SET/GET?
  - 缺：SET/GET 是单个不透明字符串：改一个字段要重写整个值
  - 缺：HSET/HGET 操作 hash，可按字段读写
  - 缺：选型：记录 / 多字段 ⇒ hash；单标量 ⇒ string
- `r-medium-07` (medium) What is the difference in role between MULTI/EXEC and WATCH?
  - 缺：MULTI/EXEC ⇒ 执行的原子性（不可打断的一块）
  - 缺：WATCH ⇒ 乐观并发控制（标记 key）
  - 缺：被别的客户端改动 ⇒ 事务中止，EXEC 返回 nil
  - 缺：单靠 MULTI/EXEC 挡不住读改写竞态
- `r-medium-08` (medium) What is the difference between using PUBLISH/SUBSCRIBE and using a lis
  - 缺：PUBLISH/SUBSCRIBE = fire-and-forget：只有发布瞬间的订阅者收到
  - 缺：无持久化，断线就丢
  - 缺：list 当队列会**存住**消息，直到被消费
  - 缺：消费者临时下线后仍能拿到
- `r-hard-01` (hard) Design a fixed-window rate limiter that allows at most N requests per 
  - 缺：用 INCR 做原子计数 + EXPIRE 设窗口过期
  - 缺：安全性来自 INCR 的原子性（并发不会同读同判）
  - 缺：顺序陷阱：EXPIRE 必须与计数一起创建，否则无 TTL 永久堵死
  - 缺：GET-then-SET 不等价于 INCR（读改写竞态）
  - 缺：已知弱点：窗口边界可突发到 2N
- `r-hard-02` (hard) Design a job queue in Redis where a worker crashing mid-job does not l
  - 缺：生产 LPUSH/RPUSH、消费 BRPOP/BLPOP（阻塞而非轮询）
  - 缺：朴素做法是 at-most-once：弹出即移除，崩了丢任务
  - 缺：要 at-least-once 需第二步：移入 in-flight 结构并 ack
  - 缺：要有 reaper 把超租约的任务放回队列
  - 缺：失败窗口 = pop 与 ack 之间 ⇒ 消费者必须幂等
- `r-hard-03` (hard) You need to decrement a stock counter only if it is greater than zero,
  - 缺：WATCH → GET 读值 → 判断 > 0 → MULTI 里 DECR → EXEC
  - 缺：失败条件：WATCH 与 EXEC 之间有别的客户端改了 key
  - 缺：中止时 EXEC 返回 nil，必须重试整个读改写
  - 缺：不重试 = 静默丢弃请求
  - 缺：高竞争下可能饿死
- `r-hard-04` (hard) Design a leaderboard with ZADD and ZRANGE: how do you insert a score, 
  - 缺：ZADD 插入 / 更新（同一成员再加 = 改分数）
  - 缺：top 10 = ZRANGE key 0 9（可 REV / WITHSCORES）
  - 缺：名次用 ZRANK / ZREVRANK（0 基）
  - 缺：名次与分页不是原子地一起读
  - 缺：同分按字典序，不是任意顺序
  - ⚠️ 这题还**声明了语料缺口** —— 有些要点语料里根本没有，低分不该全记在生成头上。
- `r-hard-06` (hard) Design a cache read path with EXPIRE and GET. Why must the application
  - 缺：写入带 EX 让条目自己过期；GET 返回 nil 视为 miss 并回源重建
  - 缺：GET 完全不携带剩余 TTL 信息
  - 缺：要剩余寿命必须显式 TTL/PTTL，且 -1 与 -2 是两种情形
  - 缺：缓存雪崩：热点 key 过期时同时 miss，所以过期要加抖动
- `r-hard-08` (hard) You store a user profile as a Redis hash. How do you update a single f
  - 缺：HSET key field value 只改命名字段，其余不动
  - 缺：对比 SET 会整体替换值
  - 缺：HGET key field 读单个字段
  - 缺：删掉最后一个字段 ⇒ 整个 key 消失（空 hash 不存在）
  - 缺：因此后续 TTL/EXISTS 会看到 key 不存在
  - ⚠️ 这题还**声明了语料缺口** —— 有些要点语料里根本没有，低分不该全记在生成头上。

## 逐题明细

| task | 难度 | 通过 / 采样 | recall | 字符 | ctx | token | 检ms | 生ms | 引用 | 依据 | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `r-simple-01` | simple | 0/3 | 0.50 | 217 | 2392 | 2676 | 10903 | 2073 | 1 条 | 3/12 | — |
| `r-simple-02` | simple | 0/3 | 0.80 | 243 | 1557 | 2284 | 166 | 2064 | 1 条 | 3/3 | — |
| `r-simple-03` | simple | 0/3 | 0.00 | 118 | 1866 | 2264 | 194 | 2048 | 2 条 | 6/6 | — |
| `r-simple-04` | simple | 0/3 | 0.50 | 178 | 1130 | 1676 | 134 | 1781 | 1 条 | 3/3 | — |
| `r-simple-05` | simple | 0/3 | 0.75 | 494 | 1572 | 2298 | 148 | 2449 | 4 条 | 6/6 | — |
| `r-simple-06` | simple | 0/3 | 0.00 | 246 | 1344 | 1911 | 164 | 2408 | 3 条 | 3/3 | — |
| `r-simple-07` | simple | 0/3 | 0.33 | 230 | 1433 | 2035 | 149 | 1984 | 2 条 | 3/3 | — |
| `r-simple-08` | simple | 0/3 | 0.67 | 120 | 1282 | 1756 | 171 | 1855 | 2 条 | 3/3 | — |
| `r-medium-01` | medium | 0/3 | 0.00 | 141 | 1612 | 2142 | 133 | 2285 | 2 条 | 6/12 | — |
| `r-medium-02` | medium | 0/3 | 0.50 | 793 | 1512 | 2156 | 188 | 2455 | 2 条 | 6/6 | — |
| `r-medium-03` | medium | 0/3 | 0.00 | 1128 | 1834 | 2543 | 161 | 3046 | 5 条 | 6/18 | — |
| `r-medium-04` | medium | 0/3 | 0.25 | 416 | 1811 | 2483 | 153 | 2138 | 2 条 | 6/12 | — |
| `r-medium-05` | medium | 0/3 | 0.00 | 182 | 1338 | 1365 | 164 | 21626 | 1 条 | 2/11 | ! |
| `r-medium-06` | medium | 0/3 | 0.25 | 664 | 1529 | 2477 | 112 | 3344 | 4 条 | 6/15 | — |
| `r-medium-07` | medium | 0/3 | 0.00 | 180 | 1332 | 1870 | 154 | 2535 | 2 条 | 6/6 | — |
| `r-medium-08` | medium | 0/3 | 0.00 | 651 | 1587 | 2357 | 175 | 3398 | 4 条 | 6/12 | — |
| `r-hard-01` | hard | 0/3 | 0.00 | 559 | 1661 | 2442 | 218 | 3529 | 7 条 | 18/36 | — |
| `r-hard-02` | hard | 0/3 | 0.00 | 224 | 2151 | 2984 | 204 | 2429 | 1 条 | 0/15 | — |
| `r-hard-03` | hard | 0/3 | 0.00 | 196 | 1536 | 2117 | 186 | 3520 | 2 条 | 6/9 | — |
| `r-hard-04` | hard | 0/3 | 0.00 | 631 | 1437 | 2408 | 192 | 2678 | 3 条 | 1/15 | — |
| `r-hard-05` | hard | 0/3 | 0.13 | 174 | 1782 | 2359 | 174 | 2526 | 4 条 | 0/12 | — |
| `r-hard-06` | hard | 0/3 | 0.00 | 415 | 1898 | 2625 | 193 | 2949 | 3 条 | 2/21 | — |
| `r-hard-07` | hard | 0/3 | 0.20 | 512 | 1470 | 2274 | 203 | 2748 | 6 条 | 12/12 | — |
| `r-hard-08` | hard | 0/3 | 0.00 | 483 | 1679 | 2450 | 224 | 2335 | 2 条 | 3/12 | — |

> `ctx` = 喂进去的上下文 token（受预算约束）。`检ms` / `生ms` **分开** —— 合成一个数的话，检索器的差距会被生成耗时稀释掉。
> `引用` / `依据` 两列印 `—` = **这题没自述引用**（不可测），不是『引用了 0 条』。

