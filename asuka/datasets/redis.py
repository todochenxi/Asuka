"""Redis 任务集（人工整理，ground truth 来自官方文档本身）。

--------------------------------------------------------------------------
为什么是人工整理，不是 LLM 生成

`reference_answer` 是**判分的尺子**。用 LLM 生成尺子，等于让被测模型
顺手定义了什么叫对 —— 尺子和被测物同源，指标就没有意义了。

所以这一版全部**人工从官方文档整理**，并逐条声明 `evidence`：
"这题的答案在哪个单元的哪一节"。校验器会核对每一个 (unit, section) 真实存在，
写错一个就报错（而且**一次报全部**）。

后续要扩到几百条时，LLM 可以**起草**，但必须人工过一遍 ——
起草可以省时间，**定尺子不行**。

--------------------------------------------------------------------------
三档难度（用户给的语义）

    simple  定义：是什么 / 语法 / 复杂度 / 返回值 / 边界值
    medium  对比与选择：A 与 B 的差别、这种场景该用哪个
    hard    设计场景：用这些命令拼出一个方案，并说清它的**失效边界**
"""
from __future__ import annotations

from . import Dataset, Evidence, RequiredPoint, TaskItem  # noqa: F401  (re-export)

# 便于书写：`E("expire", "overview")` / `R("要点名", ("说法A", "说法B"))`
E = Evidence
R = RequiredPoint


def build() -> Dataset:
    items: tuple[TaskItem, ...] = (
        # ---------------------------------------------------------- simple
        TaskItem(
            task_id="r-simple-01",
            question="What does the EXPIRE command do, what is its syntax, and what is its time complexity?",
            reference_answer=(
                "EXPIRE sets a timeout, in seconds, on a key; once the timeout has expired the key is "
                "automatically deleted. Syntax: `EXPIRE key seconds [NX | XX | GT | LT]`. "
                "Time complexity is O(1). It returns 1 if the timeout was set and 0 if it was not "
                "(for example when the NX/XX/GT/LT condition is not met, or the key does not exist)."
            ),
            source_document="redis:expire",
            difficulty="simple",
            evidence=(E("expire", "command spec"), E("expire", "overview")),
            required_points=(
                R(
                    label='作用：设超时，到期自动删除',
                    any_of=(
                        'sets a timeout',
                        'automatically deleted',
                    ),
                ),
                R(
                    label='语法：EXPIRE key seconds',
                    any_of=(
                        'expire key seconds',
                    ),
                ),
                R(
                    label='复杂度 O(1)',
                    any_of=(
                        'o(1)',
                    ),
                ),
                R(
                    label='返回 1 / 0',
                    any_of=(
                        'returns 1 if the timeout was set',
                        'returns 1',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-02",
            question="What is the full syntax of the SET command in modern Redis?",
            reference_answer=(
                "`SET key value [NX | XX | IFEQ ifeq-value | IFNE ifne-value | IFDEQ ifdeq-digest | "
                "IFDNE ifdne-digest] [GET] [EX seconds | PX milliseconds | EXAT unix-time-seconds | "
                "PXAT unix-time-milliseconds | KEEPTTL]`. The conditional options (NX/XX/IFEQ/IFNE/IFDEQ/"
                "IFDNE) decide whether the write happens; GET returns the previous value; "
                "EX/PX/EXAT/PXAT set an expiry, KEEPTTL retains the existing one."
            ),
            source_document="redis:set",
            difficulty="simple",
            evidence=(E("set", "command spec"),),
            required_points=(
                R(
                    label='基本形式 SET key value',
                    any_of=(
                        'set key value',
                    ),
                ),
                R(
                    label='条件写入 NX / XX',
                    any_of=(
                        'nx | xx',
                        'nx/xx',
                    ),
                ),
                R(
                    label='GET 返回旧值',
                    any_of=(
                        'get returns the previous value',
                        'returns the previous value',
                    ),
                ),
                R(
                    label='过期选项 EX / PX / EXAT / PXAT',
                    any_of=(
                        'ex/px/exat/pxat set an expiry',
                        'exat',
                        'pxat',
                    ),
                ),
                R(
                    label='KEEPTTL 保留现有 TTL',
                    any_of=(
                        'keepttl',
                        'retains the existing one',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-03",
            question="What does TTL return when the key exists but has no expiration, and when the key does not exist?",
            reference_answer=(
                "Starting with Redis 2.8, TTL returns -2 if the key does not exist, and -1 if the key "
                "exists but has no associated expiration. Otherwise it returns the remaining time to live "
                "in seconds. (In Redis 2.6 or older both cases returned -1, which is why the distinction "
                "was introduced.)"
            ),
            source_document="redis:ttl",
            difficulty="simple",
            evidence=(E("ttl", "overview"), E("ttl", "Return information")),
            required_points=(
                R(
                    label='-2 = key 不存在',
                    any_of=(
                        'returns -2 if the key does not exist',
                        '-2 if the key does not exist',
                    ),
                ),
                R(
                    label='-1 = key 在但无过期',
                    any_of=(
                        '-1 if the key exists but has no associated expiration',
                        '-1 if the key exists',
                    ),
                ),
                R(
                    label='否则返回剩余秒数',
                    any_of=(
                        'remaining time to live in seconds',
                        'remaining time to live',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-04",
            question="What does SADD return?",
            reference_answer=(
                "SADD returns an integer: the number of elements that were **added** to the set, "
                "not counting elements that were already present. So adding an existing member "
                "contributes 0."
            ),
            source_document="redis:sadd",
            difficulty="simple",
            evidence=(E("sadd", "Return information"),),
            required_points=(
                R(
                    label='返回**新增**的元素个数（不是集合大小）',
                    any_of=(
                        'number of elements that were added',
                        'number of elements that were added to the set',
                    ),
                ),
                R(
                    label='已存在的成员不计入（贡献 0）',
                    any_of=(
                        'not counting elements that were already present',
                        'contributes 0',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-05",
            question="What does ZADD return, and how do the CH and INCR options change the return value?",
            reference_answer=(
                "Without CH, ZADD returns the number of **new** members added. With CH it returns the "
                "number of new **or updated** members. With INCR it returns the updated score of the "
                "member as a bulk string (behaving like ZINCRBY). If the operation was aborted because of "
                "a conflict with XX/NX/LT/GT, it returns nil."
            ),
            source_document="redis:zadd",
            difficulty="simple",
            evidence=(E("zadd", "Return information"),),
            required_points=(
                R(
                    label='默认返回**新增**成员数',
                    any_of=(
                        'number of new members added',
                        'number of new members',
                    ),
                ),
                R(
                    label='CH ⇒ 新增或更新',
                    any_of=(
                        'number of new or updated members',
                        'new or updated',
                    ),
                ),
                R(
                    label='INCR ⇒ 返回更新后的分数',
                    any_of=(
                        'returns the updated score of the member',
                        'updated score of the member',
                    ),
                ),
                R(
                    label='冲突中止 ⇒ nil',
                    any_of=(
                        'returns nil',
                        'conflict with xx/nx/lt/gt',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-06",
            question="How do the start and stop indexes of LRANGE behave, including negative values?",
            reference_answer=(
                "LRANGE returns the specified range of elements. Indexes are zero-based; negative indexes "
                "count from the end of the list, so -1 is the last element. Out-of-range indexes are "
                "handled gracefully rather than raising an error (an out-of-range range simply yields an "
                "empty list or a truncated result)."
            ),
            source_document="redis:lrange",
            difficulty="simple",
            evidence=(E("lrange", "overview"),),
            required_points=(
                R(
                    label='下标 0 基',
                    any_of=(
                        'zero-based',
                    ),
                ),
                R(
                    label='负下标从尾部数，-1 是最后一个',
                    any_of=(
                        'negative indexes count from the end',
                        '-1 is the last element',
                    ),
                ),
                R(
                    label='越界不报错（空 / 截断）',
                    any_of=(
                        'handled gracefully',
                        'out-of-range',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-07",
            question="What is MULTI used for in Redis?",
            reference_answer=(
                "MULTI marks the start of a transaction: subsequent commands are queued rather than "
                "executed immediately, and EXEC runs them all. The queued commands are executed "
                "sequentially and are not interrupted by other clients' commands."
            ),
            source_document="redis:multi",
            difficulty="simple",
            evidence=(E("multi", "overview"),),
            required_points=(
                R(
                    label='MULTI 开启事务：后续命令入队而非立刻执行',
                    any_of=(
                        'marks the start of a transaction',
                        'queued rather than executed immediately',
                    ),
                ),
                R(
                    label='EXEC 执行全部',
                    any_of=(
                        'exec runs them all',
                        'exec runs',
                    ),
                ),
                R(
                    label='顺序执行且不被打断',
                    any_of=(
                        'executed sequentially',
                        'not interrupted by other clients',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-simple-08",
            question="What is the syntax of SUBSCRIBE and how many channels can it take at once?",
            reference_answer=(
                "`SUBSCRIBE channel [channel ...]` — it takes one or more channels and subscribes the "
                "client to each of them. Once subscribed, the client enters subscribe mode and can only "
                "issue a restricted set of commands (SUBSCRIBE, UNSUBSCRIBE, PSUBSCRIBE, PUNSUBSCRIBE, "
                "PING, QUIT, RESET)."
            ),
            source_document="redis:subscribe",
            difficulty="simple",
            evidence=(E("subscribe", "command spec"),),
            required_points=(
                R(
                    label='语法 SUBSCRIBE channel ...',
                    any_of=(
                        'subscribe channel',
                    ),
                ),
                R(
                    label='可一次订阅多个频道',
                    any_of=(
                        'one or more channels',
                    ),
                ),
                R(
                    label='进入 subscribe 模式，只能用受限命令集',
                    any_of=(
                        'subscribe mode',
                        'restricted set of commands',
                    ),
                ),
            ),
        ),
        # ---------------------------------------------------------- medium
        TaskItem(
            task_id="r-medium-01",
            question="What is the difference between BLPOP and BRPOP?",
            reference_answer=(
                "Both are blocking list pops that wait until an element is available or the timeout "
                "expires, but they pop from opposite ends: BLPOP pops from the **head** (left) and BRPOP "
                "from the **tail** (right). Their arguments are otherwise the same: "
                "`BLPOP key [key ...] timeout`."
            ),
            source_document="redis:blpop",
            difficulty="medium",
            evidence=(
                E("blpop", "overview"),
                E("brpop", "overview"),
                E("blpop", "command spec"),
                E("brpop", "command spec"),
            ),
            required_points=(
                R(
                    label='都是阻塞弹出（等到有元素或超时）',
                    any_of=(
                        'blocking list pops',
                        'wait until an element is available or the timeout expires',
                    ),
                ),
                R(
                    label='BLPOP 从头部（左）',
                    any_of=(
                        'blpop pops from the head',
                        'from the head',
                    ),
                ),
                R(
                    label='BRPOP 从尾部（右）',
                    any_of=(
                        'brpop from the tail',
                        'from the tail',
                    ),
                ),
                R(
                    label='其余参数相同',
                    any_of=(
                        'arguments are otherwise the same',
                        'blpop key',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-02",
            question="What is the difference between LPUSH and RPUSH, and how does it affect the order elements come out with RPOP?",
            reference_answer=(
                "LPUSH inserts elements at the **head** (left) of the list, RPUSH at the **tail** (right). "
                "With LPUSH a, b, c the list becomes c, b, a — so LPUSH followed by RPOP yields "
                "last-in-first-out (stack). RPUSH followed by LPOP also yields LIFO, while RPUSH followed "
                "by RPOP yields first-in-first-out (queue)."
            ),
            source_document="redis:lpush",
            difficulty="medium",
            evidence=(E("lpush", "overview"), E("rpush", "overview")),
            required_points=(
                R(
                    label='LPUSH 插头部',
                    any_of=(
                        'lpush inserts elements at the head',
                        'at the head',
                    ),
                ),
                R(
                    label='RPUSH 插尾部',
                    any_of=(
                        'rpush at the tail',
                        'at the tail',
                    ),
                ),
                R(
                    label='LPUSH a b c ⇒ c b a（后进先出）',
                    any_of=(
                        'becomes c, b, a',
                        'last-in-first-out',
                    ),
                ),
                R(
                    label='RPUSH + RPOP ⇒ 先进先出（队列）',
                    any_of=(
                        'first-in-first-out',
                        'yields first-in-first-out',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-03",
            question="What is the difference between EXPIRE and TTL?",
            reference_answer=(
                "They are inverses: EXPIRE **sets** a timeout on a key (write), while TTL **reads** the "
                "remaining time to live (introspection). EXPIRE returns 1/0 for whether the timeout was "
                "set; TTL returns the seconds remaining, or -1 when there is no expiration and -2 when the "
                "key does not exist. Note that TTL is not a test for key existence in the general sense — "
                "-2 is."
            ),
            source_document="redis:expire",
            difficulty="medium",
            evidence=(
                E("expire", "overview"),
                E("expire", "command spec"),
                E("ttl", "overview"),
                E("ttl", "Return information"),
            ),
            required_points=(
                R(
                    label='互为反向：EXPIRE 写、TTL 读',
                    any_of=(
                        'they are inverses',
                        'expire sets a timeout',
                        'ttl reads the remaining time',
                    ),
                ),
                R(
                    label='EXPIRE 返回 1 / 0',
                    any_of=(
                        'expire returns 1/0',
                    ),
                ),
                R(
                    label='TTL 返回剩余秒数；-1 无过期、-2 key 不存在',
                    any_of=(
                        'no expiration and -2 when the key does not exist',
                        '-2 when the key does not exist',
                    ),
                ),
                R(
                    label='TTL 不是通用的 key 存在性检测',
                    any_of=(
                        'not a test for key existence',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-04",
            question="In SET, what is the difference between the EX, PX, EXAT and PXAT options?",
            reference_answer=(
                "All four set an expiry, differing in unit and in whether the value is a duration or an "
                "absolute time: EX is seconds as a duration, PX is milliseconds as a duration, "
                "EXAT is a Unix timestamp in seconds, PXAT is a Unix timestamp in milliseconds. "
                "There is also KEEPTTL, which retains the existing TTL instead of replacing it."
            ),
            source_document="redis:set",
            difficulty="medium",
            evidence=(E("set", "Optional arguments"),),
            required_points=(
                R(
                    label='四个都设过期，区别在**单位**与**时长 vs 绝对时间**',
                    any_of=(
                        'differing in unit',
                        'duration or an absolute time',
                    ),
                ),
                R(
                    label='EX / PX = 时长（秒 / 毫秒）',
                    any_of=(
                        'ex is seconds as a duration',
                        'px is milliseconds as a duration',
                    ),
                ),
                R(
                    label='EXAT / PXAT = Unix 时间戳（秒 / 毫秒）',
                    any_of=(
                        'exat is a unix timestamp in seconds',
                        'pxat is a unix timestamp in milliseconds',
                    ),
                ),
                R(
                    label='KEEPTTL 保留原 TTL',
                    any_of=(
                        'keepttl',
                        'retains the existing ttl',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-05",
            question="When should you use HSET/HGET instead of SET/GET?",
            reference_answer=(
                "SET/GET store a single opaque string value per key, so updating one logical field means "
                "rewriting the whole value. HSET/HGET operate on a hash, letting you store an object as "
                "multiple named fields and update or read **individual fields** without touching the "
                "others. Use the hash form when the value is a record with several fields; use the plain "
                "string form when it is a single scalar."
            ),
            source_document="redis:hset",
            difficulty="medium",
            evidence=(
                E("hset", "overview"),
                E("hget", "overview"),
                E("set", "overview"),
                E("get", "overview"),
            ),
            required_points=(
                R(
                    label='SET/GET 是单个不透明字符串：改一个字段要重写整个值',
                    any_of=(
                        'single opaque string value',
                        'rewriting the whole value',
                    ),
                ),
                R(
                    label='HSET/HGET 操作 hash，可按字段读写',
                    any_of=(
                        'operate on a hash',
                        'individual fields',
                    ),
                ),
                R(
                    label='选型：记录 / 多字段 ⇒ hash；单标量 ⇒ string',
                    any_of=(
                        'record with several fields',
                        'single scalar',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-06",
            question="What is the difference between SADD and ZADD?",
            reference_answer=(
                "SADD adds members to an unordered **set** — membership only, no ordering or scores. "
                "ZADD adds members to a **sorted set**, each with an associated score, which gives a "
                "deterministic ordering by score (and lexicographic ordering among equal scores). "
                "SADD returns the number of newly added members; ZADD returns the number of new members, "
                "or new-or-updated members when CH is used."
            ),
            source_document="redis:sadd",
            difficulty="medium",
            evidence=(
                E("sadd", "overview"),
                E("sadd", "Return information"),
                E("zadd", "overview"),
                E("zadd", "Return information"),
            ),
            required_points=(
                R(
                    label='SADD ⇒ 无序集合，只有成员关系，无分数',
                    any_of=(
                        'unordered set',
                        'no ordering or scores',
                    ),
                ),
                R(
                    label='ZADD ⇒ 有序集合，成员带 score',
                    any_of=(
                        'sorted set',
                        'associated score',
                    ),
                ),
                R(
                    label='按 score 排序，同分按字典序',
                    any_of=(
                        'deterministic ordering by score',
                        'lexicographic ordering among equal scores',
                    ),
                ),
                R(
                    label='返回值语义：新增 / 新增或更新（CH）',
                    any_of=(
                        'number of newly added members',
                        'new-or-updated members when ch',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-07",
            question="What is the difference in role between MULTI/EXEC and WATCH?",
            reference_answer=(
                "MULTI/EXEC gives **atomicity of execution**: the queued commands run as a single "
                "uninterrupted block. WATCH gives **optimistic concurrency control**: it marks keys so "
                "that if any of them is modified by another client before EXEC, the whole transaction is "
                "aborted and EXEC returns nil. MULTI/EXEC alone does not protect against a "
                "read-then-write race; WATCH is what adds that check."
            ),
            source_document="redis:watch",
            difficulty="medium",
            evidence=(E("watch", "overview"), E("multi", "overview")),
            required_points=(
                R(
                    label='MULTI/EXEC ⇒ 执行的原子性（不可打断的一块）',
                    any_of=(
                        'atomicity of execution',
                        'single uninterrupted block',
                    ),
                ),
                R(
                    label='WATCH ⇒ 乐观并发控制（标记 key）',
                    any_of=(
                        'optimistic concurrency control',
                        'it marks keys',
                    ),
                ),
                R(
                    label='被别的客户端改动 ⇒ 事务中止，EXEC 返回 nil',
                    any_of=(
                        'aborted and exec returns nil',
                        'whole transaction is aborted',
                    ),
                ),
                R(
                    label='单靠 MULTI/EXEC 挡不住读改写竞态',
                    any_of=(
                        'does not protect against a read-then-write race',
                        'read-then-write race',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-medium-08",
            question="What is the difference between using PUBLISH/SUBSCRIBE and using a list (LPUSH + BRPOP) for messaging?",
            reference_answer=(
                "PUBLISH/SUBSCRIBE is fire-and-forget: messages are delivered only to clients that are "
                "subscribed at the moment of publishing, there is no persistence, and a client that is "
                "disconnected misses the message. A list used as a queue (producer LPUSH/RPUSH, consumer "
                "BRPOP/BLPOP) **stores** the messages: they remain in the list until a consumer takes "
                "them, so a consumer that is temporarily down will still receive them later."
            ),
            source_document="redis:publish",
            difficulty="medium",
            evidence=(
                E("publish", "overview"),
                E("subscribe", "overview"),
                E("brpop", "overview"),
                E("blpop", "overview"),
            ),
            required_points=(
                R(
                    label='PUBLISH/SUBSCRIBE = fire-and-forget：只有发布瞬间的订阅者收到',
                    any_of=(
                        'fire-and-forget',
                        'subscribed at the moment of publishing',
                    ),
                ),
                R(
                    label='无持久化，断线就丢',
                    any_of=(
                        'there is no persistence',
                        'disconnected misses the message',
                    ),
                ),
                R(
                    label='list 当队列会**存住**消息，直到被消费',
                    any_of=(
                        'remain in the list until a consumer takes them',
                        'stores the messages',
                    ),
                ),
                R(
                    label='消费者临时下线后仍能拿到',
                    any_of=(
                        'temporarily down will still receive them',
                    ),
                ),
            ),
        ),
        # ---------------------------------------------------------- hard
        TaskItem(
            task_id="r-hard-01",
            question=(
                "Design a fixed-window rate limiter that allows at most N requests per key per window, "
                "using only Redis commands. Which commands do you use and what makes the counter safe?"
            ),
            reference_answer=(
                "Use INCR on a per-window key to get an atomic counter (INCR is O(1) and returns the new "
                "value), and set an expiry on that key with EXPIRE so the window resets by itself. "
                "The counter is safe because INCR is atomic, so concurrent clients cannot both read the "
                "same value and both decide they are under the limit. A correct answer should note the "
                "two ordering hazards: EXPIRE must be applied when the counter is created (otherwise a "
                "crash between INCR and EXPIRE leaves a key with no TTL that blocks the key forever), and "
                "a plain GET-then-SET is NOT equivalent to INCR because it is a read-modify-write race. "
                "The window boundary is the known weakness: traffic can burst to 2N across a boundary."
            ),
            source_document="redis:incr",
            difficulty="hard",
            evidence=(
                E("incr", "Pattern: counter"),
                E("incr", "Pattern: rate limiter"),
                E("incr", "Pattern: rate limiter 2"),
                E("expire", "overview"),
                E("incr", "overview"),
            ),
            required_points=(
                R(
                    label='用 INCR 做原子计数 + EXPIRE 设窗口过期',
                    any_of=(
                        'incr on a per-window key',
                        'set an expiry on that key with expire',
                    ),
                ),
                R(
                    label='安全性来自 INCR 的原子性（并发不会同读同判）',
                    any_of=(
                        'incr is atomic',
                        'concurrent clients cannot both read the same value',
                    ),
                ),
                R(
                    label='顺序陷阱：EXPIRE 必须与计数一起创建，否则无 TTL 永久堵死',
                    any_of=(
                        'expire must be applied when the counter is created',
                        'crash between incr and expire',
                    ),
                ),
                R(
                    label='GET-then-SET 不等价于 INCR（读改写竞态）',
                    any_of=(
                        'not equivalent to incr',
                        'read-modify-write race',
                    ),
                ),
                R(
                    label='已知弱点：窗口边界可突发到 2N',
                    any_of=(
                        'burst to 2n',
                        'window boundary',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-02",
            question=(
                "Design a job queue in Redis where a worker crashing mid-job does not lose the job. "
                "Which commands, and what is the failure window?"
            ),
            reference_answer=(
                "Producer pushes with LPUSH (or RPUSH); worker consumes with BRPOP (or BLPOP) so it blocks "
                "instead of busy-polling. The naive version is **at-most-once**: BRPOP removes the element "
                "the moment it is handed to the worker, so a worker that dies between popping and finishing "
                "loses the job. To get at-least-once you need a second step — move the job to an "
                "'in-flight' structure (e.g. a processing list or a sorted set scored by lease deadline) "
                "and acknowledge it when done, with a reaper returning expired leases to the queue. "
                "A correct answer should state the failure window explicitly (crash between pop and ack) "
                "and note that this makes delivery at-least-once, so consumers must be idempotent."
            ),
            source_document="redis:brpop",
            difficulty="hard",
            evidence=(
                E("brpop", "overview"),
                E("brpop", "command spec"),
                E("lpush", "overview"),
                E("blpop", "Blocking behavior"),
                E("blpop", "Non-blocking behavior"),
            ),
            required_points=(
                R(
                    label='生产 LPUSH/RPUSH、消费 BRPOP/BLPOP（阻塞而非轮询）',
                    any_of=(
                        'producer pushes with lpush',
                        'blocks instead of busy-polling',
                    ),
                ),
                R(
                    label='朴素做法是 at-most-once：弹出即移除，崩了丢任务',
                    any_of=(
                        'at-most-once',
                        'dies between popping and finishing loses the job',
                    ),
                ),
                R(
                    label='要 at-least-once 需第二步：移入 in-flight 结构并 ack',
                    any_of=(
                        'in-flight',
                        'acknowledge it when done',
                    ),
                ),
                R(
                    label='要有 reaper 把超租约的任务放回队列',
                    any_of=(
                        'reaper',
                        'returning expired leases to the queue',
                    ),
                ),
                R(
                    label='失败窗口 = pop 与 ack 之间 ⇒ 消费者必须幂等',
                    any_of=(
                        'crash between pop and ack',
                        'consumers must be idempotent',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-03",
            question=(
                "You need to decrement a stock counter only if it is greater than zero, with many "
                "concurrent writers. How do you do it with WATCH/MULTI/EXEC, and when does the transaction fail?"
            ),
            reference_answer=(
                "WATCH the stock key, read its current value with GET, and if it is greater than zero "
                "queue a DECR (or SET) inside MULTI and run EXEC. If any watched key was modified by "
                "another client between WATCH and EXEC, EXEC aborts and returns nil — the client must "
                "then retry the whole read-modify-write loop. So the transaction fails exactly when a "
                "concurrent writer touched the key, which is the point: it converts a lost-update race "
                "into a retry. A correct answer should note the retry loop is mandatory (aborting without "
                "retrying silently drops the request) and that the loop can starve under heavy contention."
            ),
            source_document="redis:watch",
            difficulty="hard",
            evidence=(
                E("watch", "overview"),
                E("watch", "command spec"),
                E("multi", "overview"),
            ),
            required_points=(
                R(
                    label='WATCH → GET 读值 → 判断 > 0 → MULTI 里 DECR → EXEC',
                    any_of=(
                        'watch the stock key',
                        'queue a decr',
                    ),
                ),
                R(
                    label='失败条件：WATCH 与 EXEC 之间有别的客户端改了 key',
                    any_of=(
                        'between watch and exec',
                        'modified by another client',
                    ),
                ),
                R(
                    label='中止时 EXEC 返回 nil，必须重试整个读改写',
                    any_of=(
                        'exec aborts and returns nil',
                        'retry the whole read-modify-write loop',
                    ),
                ),
                R(
                    label='不重试 = 静默丢弃请求',
                    any_of=(
                        'aborting without retrying',
                        'silently drops the request',
                    ),
                ),
                R(
                    label='高竞争下可能饿死',
                    any_of=(
                        'starve under heavy contention',
                        'starve',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-04",
            question=(
                "Design a leaderboard with ZADD and ZRANGE: how do you insert a score, get the top 10, "
                "and get a specific player's rank?"
            ),
            reference_answer=(
                "Insert or update with ZADD key score member — re-adding the same member updates its "
                "score. The top 10 is ZRANGE key 0 9 (optionally REV for descending order, or WITHSCORES "
                "to include scores). A specific player's rank comes from ZRANK (ascending) or ZREVRANK "
                "(descending), which return the 0-based position — note this is a separate call from "
                "ZRANGE, so rank and page are not read atomically together. A correct answer should also "
                "note the tie-breaking rule: members with the same score are ordered lexicographically, "
                "so equal scores do not produce an arbitrary order."
            ),
            source_document="redis:zadd",
            difficulty="hard",
            evidence=(
                E("zadd", "overview"),
                E("zadd", "Elements with the same score"),
                E("zrange", "overview"),
                E("zrange", "Lexicographical ranges"),
            ),
            out_of_corpus=(
                "ZRANK / ZREVRANK 在语料里**完全没有**（语料只有 ZADD / ZRANGE 两个单元）。"
                "参考答案要求的『取某个玩家的排名』这一步，检索再完美也拿不到 —— "
                "这不是检索差，是语料没覆盖。",
            ),
            required_points=(
                R(
                    label='ZADD 插入 / 更新（同一成员再加 = 改分数）',
                    any_of=(
                        'zadd key score member',
                        're-adding the same member updates its score',
                    ),
                ),
                R(
                    label='top 10 = ZRANGE key 0 9（可 REV / WITHSCORES）',
                    any_of=(
                        'zrange key 0 9',
                        'withscores',
                    ),
                ),
                R(
                    label='名次用 ZRANK / ZREVRANK（0 基）',
                    any_of=(
                        'zrank',
                        'zrevrank',
                    ),
                ),
                R(
                    label='名次与分页不是原子地一起读',
                    any_of=(
                        'separate call from zrange',
                        'not read atomically',
                    ),
                ),
                R(
                    label='同分按字典序，不是任意顺序',
                    any_of=(
                        'ordered lexicographically',
                        'same score are ordered lexicographically',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-05",
            question=(
                "You must count distinct visitors per day without storing the full visitor list. "
                "Which Redis commands, and what is the accuracy trade-off?"
            ),
            reference_answer=(
                "Use SADD with a per-day key and take the cardinality of the set; SADD returns how many "
                "elements were actually **added**, so it is already a distinct-count signal, and "
                "SMEMBERS/SCARD gives the total distinct count. The trade-off is memory: an exact set "
                "stores every distinct visitor, so it grows without bound as the cardinality grows. "
                "For very large cardinalities the exact set is the wrong structure and a probabilistic "
                "sketch (HyperLogLog) should be used instead — it trades a small bounded error for "
                "constant memory. A correct answer must name the exactness/memory trade-off rather than "
                "claiming sets scale."
            ),
            source_document="redis:sadd",
            difficulty="hard",
            evidence=(
                E("sadd", "overview"),
                E("sadd", "Return information"),
                E("smembers", "overview"),
                E("smembers", "Return information"),
            ),
            out_of_corpus=(
                "HyperLogLog（PFADD / PFCOUNT）在语料里**完全没有** —— 语料只有 "
                "SADD / SMEMBERS 这类精确集合。而题目问的是『不存全量访客列表』，"
                "参考答案也要求点名『概率结构换常量内存』这条权衡："
                "这一半在语料里无从获得。⚠️ 本题的检索失败**另有原因**（问句用词"
                "『distinct visitors』与 SADD 页面的『members』语义距离远），"
                "两件事别混为一谈。",
            ),
            required_points=(
                R(
                    label='SADD 到按天的 key，用『实际新增数』作去重信号',
                    any_of=(
                        'sadd returns how many elements were actually added',
                        'distinct-count signal',
                    ),
                ),
                R(
                    label='SCARD / SMEMBERS 取总数',
                    any_of=(
                        'scard',
                        'smembers',
                    ),
                ),
                R(
                    label='权衡是内存：精确集合随基数无界增长',
                    any_of=(
                        'grows without bound',
                        'stores every distinct visitor',
                    ),
                ),
                R(
                    label='极大基数应换概率结构（HyperLogLog）：小而有界的误差换常量内存',
                    any_of=(
                        'hyperloglog',
                        'constant memory',
                    ),
                ),
                R(
                    label='必须点明『精确性 / 内存』这条权衡，而不是说集合能扛住',
                    any_of=(
                        'exactness/memory trade-off',
                        'rather than claiming sets scale',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-06",
            question=(
                "Design a cache read path with EXPIRE and GET. Why must the application not rely on "
                "'the key is still there' to mean 'the value is still fresh'?"
            ),
            reference_answer=(
                "Write with SET key value EX seconds (or SET followed by EXPIRE) so the entry expires by "
                "itself; read with GET and treat a nil reply as a miss, then recompute and repopulate. "
                "The subtlety is that GET cannot tell you how much TTL is left — GET returns the value "
                "with no expiry information at all. If the application needs the remaining lifetime it "
                "must call TTL (or PTTL) explicitly, and it must handle -1 (no expiration) and -2 (key "
                "gone) as distinct cases. A correct answer should also note the cache-stampede hazard: "
                "when a hot key expires, every reader misses simultaneously and stampedes the backing "
                "store, which is why the expiry is usually jittered."
            ),
            source_document="redis:expire",
            difficulty="hard",
            evidence=(
                E("expire", "overview"),
                E("expire", "command spec"),
                E("get", "overview"),
                E("ttl", "overview"),
                E("ttl", "Return information"),
            ),
            required_points=(
                R(
                    label='写入带 EX 让条目自己过期；GET 返回 nil 视为 miss 并回源重建',
                    any_of=(
                        'set key value ex seconds',
                        'treat a nil reply as a miss',
                    ),
                ),
                R(
                    label='GET 完全不携带剩余 TTL 信息',
                    any_of=(
                        'no expiry information at all',
                        'cannot tell you how much ttl is left',
                    ),
                ),
                R(
                    label='要剩余寿命必须显式 TTL/PTTL，且 -1 与 -2 是两种情形',
                    any_of=(
                        'call ttl',
                        '-1 (no expiration) and -2 (key gone)',
                    ),
                ),
                R(
                    label='缓存雪崩：热点 key 过期时同时 miss，所以过期要加抖动',
                    any_of=(
                        'cache-stampede',
                        'expiry is usually jittered',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-07",
            question=(
                "Design a real-time notification fan-out with PUBLISH/SUBSCRIBE. What delivery guarantee "
                "does it provide, and what must you add if you need more?"
            ),
            reference_answer=(
                "Publishers call PUBLISH channel message and each subscribed client receives it; a client "
                "subscribes with SUBSCRIBE channel. The guarantee is **at-most-once** (fire-and-forget): "
                "messages are not persisted, a subscriber that is disconnected or slow simply misses them, "
                "and PUBLISH's return value (the number of receivers) tells you nothing about whether "
                "anyone acted on it. If you need durability you must add a persistent path alongside it — "
                "for example writing the event to a list or a stream and having the consumer read from "
                "there, so a consumer that was down can catch up. A correct answer must state at-most-once "
                "explicitly and must not describe the receiver count as an acknowledgement."
            ),
            source_document="redis:publish",
            difficulty="hard",
            evidence=(
                E("publish", "overview"),
                E("publish", "Return information"),
                E("subscribe", "overview"),
                E("subscribe", "command spec"),
            ),
            required_points=(
                R(
                    label='PUBLISH 发、SUBSCRIBE 收',
                    any_of=(
                        'publish channel message',
                        'subscribes with subscribe channel',
                    ),
                ),
                R(
                    label='保证是 at-most-once（fire-and-forget）',
                    any_of=(
                        'at-most-once',
                        'fire-and-forget',
                    ),
                ),
                R(
                    label='消息不持久化，断线 / 慢订阅者直接丢',
                    any_of=(
                        'messages are not persisted',
                        'misses them',
                    ),
                ),
                R(
                    label='PUBLISH 的返回值（接收者数）**不是** ack',
                    any_of=(
                        'tells you nothing about whether anyone acted on it',
                        'receiver count',
                    ),
                ),
                R(
                    label='要持久化必须并行加一条持久路径（list / stream）',
                    any_of=(
                        'persistent path alongside it',
                        'writing the event to a list or a stream',
                    ),
                ),
            ),
        ),
        TaskItem(
            task_id="r-hard-08",
            question=(
                "You store a user profile as a Redis hash. How do you update a single field without "
                "clobbering the others, and what happens to the key when the last field is removed?"
            ),
            reference_answer=(
                "Use HSET key field value for each field — it sets or updates only the named field(s) and "
                "leaves the rest of the hash untouched, which is exactly what a plain SET could not do "
                "(SET would replace the whole value). Read one field with HGET key field. Deleting the "
                "last field removes the key entirely, since a Redis hash with no fields cannot exist — so "
                "code that assumes the key persists after emptying it is wrong, and TTL/EXISTS checks "
                "afterwards will see a missing key. A correct answer should mention HDEL and this "
                "empty-hash-becomes-missing-key behaviour."
            ),
            source_document="redis:hset",
            difficulty="hard",
            evidence=(
                E("hset", "overview"),
                E("hset", "command spec"),
                E("hget", "overview"),
                E("hget", "Required arguments"),
            ),
            out_of_corpus=(
                "HDEL 在语料里**完全没有**（语料只有 HSET / HGET）。参考答案要求的"
                "『删掉最后一个字段后整个 key 消失』这一步没有直接支撑 —— "
                "语料只能间接说明 hash 的存在性，说不出删除语义。",
            ),
            required_points=(
                R(
                    label='HSET key field value 只改命名字段，其余不动',
                    any_of=(
                        'sets or updates only the named field',
                        'leaves the rest of the hash untouched',
                    ),
                ),
                R(
                    label='对比 SET 会整体替换值',
                    any_of=(
                        'set would replace the whole value',
                        'replace the whole value',
                    ),
                ),
                R(
                    label='HGET key field 读单个字段',
                    any_of=(
                        'hget key field',
                    ),
                ),
                R(
                    label='删掉最后一个字段 ⇒ 整个 key 消失（空 hash 不存在）',
                    any_of=(
                        'removes the key entirely',
                        'a redis hash with no fields cannot exist',
                    ),
                ),
                R(
                    label='因此后续 TTL/EXISTS 会看到 key 不存在',
                    any_of=(
                        'will see a missing key',
                        'assumes the key persists after emptying it is wrong',
                    ),
                ),
            ),
        ),
    )
    return Dataset(
        topic="redis",
        version="0.1",
        notes=(
            "人工整理自 redis.io 官方文档（20 条命令）。"
            "evidence 指向 (unit_id, section)，加载时解析成 chunk_id —— "
            "chunk_id 是切分参数的函数，写死会在改参数时静默失效。"
        ),
        items=items,
    )
