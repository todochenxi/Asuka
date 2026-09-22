"""子 Run 派生（M25 / §63）。

--------------------------------------------------------------------------
为什么这两个洞是**同一个洞**

M23 点名过两个未覆盖组合：`native:skill` 与 `agent_runtime:agent_delegation`。
它们看起来是"两个执行器没人写"，其实是同一件事：

    Skill            = Procedure（§25）—— 内部有多步
    Agent Delegation = 起一个 Child AgentRun（A2A，§26）

两者都意味着"**开一条子 Run**"。而 Kernel 的 `Executor.execute()` 契约是
**同步返回一个最终结果** —— 它没有"已启动，稍后完成"这个状态。

所以把任何一个当成普通 Task 交给 Worker，都会撞上同一堵墙：
一条 Execution（`Task : Execution = 1 : 1`，E-19）里塞进一整条子 Run 的
N 条 Execution。后果不是"跑得慢一点"：

    · 子 Run 无法独立恢复 —— 父 Execution 的 Lease 一过期，
      Recovery 重跑父 Task → **整条子 Run 从头再来**，副作用重放一遍
    · 子 Run 没有自己的 Lease / Attempt / Cancellation 粒度
    · S-13 说 SKILL_CALL / AGENT_DELEGATION 都可补偿，
      但"撤销整个技能"在只跑了 3/5 步时是错的 —— 粒度根本对不上

这与 M23 里 `ApprovalGateExecutor` 的情形是**同构**的：
闸门也不由 Worker 执行，由 Loop 在派发前挂起（H-4 / X-11）。
所以 M25 沿用同一条先例。

--------------------------------------------------------------------------
D-1：重试不得开出第二个子 Run

父 Task 会因为 Attempt #2、Recovery 重排队而被**再次执行**。
如果每次执行都派生一次，就会开出第二条子 Run ——
这正是 M24 修掉的那个"第二个 Run"（A-3）在子 Run 上的重演，
而且更难发现：父 Run 看起来完全正常，只是后台多跑了一份。

所以派生键必须是 **父 Execution 的 execution_id**（E-21：跨 Attempt 稳定），
`ChildRunRegistry.bind()` 对同一个键一律返回**第一次**那条。
这条是 D-1，被测试钉住。

--------------------------------------------------------------------------
Skill 落成什么形态

§25 说 Skill 可以是 Prompt / Workflow / Agentic 三种。
M25 只落地 **Agentic Skill**（一条子 AgentRun）。
理由不是省事：三种形态里只有 Agentic Skill 在"子 Run"这个抽象上是自洽的 ——
Prompt / Workflow 的多步结构同样需要子 Run，只是**解释器**不同，
那份解释器属于 M3（Skill Runtime），现在不存在。
所以 `CHILD_SKILL` 这个挂起原因先立起来，形态留给 M3，
而"现在没有 Skill Runtime"这件事由报错**点名**，不静默。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.action import ActionType

if TYPE_CHECKING:                                    # pragma: no cover
    # `assembly` → `loop` → `delegation` → `assembly` 会成环。
    # 这里只需要 `RuntimeStack` 做类型标注，而 `from __future__ import annotations`
    # 已经让所有标注是字符串 —— 所以运行时根本不需要它。
    from .assembly import RuntimeStack


#: D-18：一次派生的**等待上限** —— 父 Run 等多久算"等不到结果"。
#:
#: 刻意与 `cancellation.DEFAULT_CANCELLATION_GRACE`（15 分钟）**不同**：
#: 两列都叫"等到什么时候为止"，回答的却是两个问题（B-7）——
#:
#:   `abandon_after`  我要求它停，等多久确认它停了（取消语义，R-11）
#:   `wait_until`     我等它的结果，等多久算等不到（挂起语义，D-18）
#:
#: 时长不同不是随手取的：确认"停没停"要快，因为那条意图正占着
#: 取消通道的队首（空洞 226 的形状）；而子 Run 跑几个小时是**正常**的，
#: 把结果等待压到 15 分钟会把大量正常委派判成"等不到"。
#:
#: 它与 `015_child_wait_deadline.sql` 里那句 `interval '30 minutes'` 回填
#: 必须相等 —— `tests/unit/test_child_wait_deadline.py` 用正则把那一句抠出来
#: 与这里对齐，改一边忘另一边会红。
DEFAULT_CHILD_WAIT_TIMEOUT = timedelta(minutes=30)

#: D-32：一次派生**能要到的最长等待** —— 平台上限，物理的（016 的 CHECK）。
#:
#: --------------------------------------------------------------------------
#: 为什么"开入口"和"定边界"是**同一件事的两半**
#:
#: 空洞 230 的形状是"没有按次派生覆盖的入口"。但只把入口开出来，
#: 比不开更糟 —— 不开的时候至少还有全局 30 分钟兜着。
#:
#: 因为等待上限属于**控制**（Harness），不属于**决策**（Intelligence）：
#:
#:   · "这次委派大概要跑多久"确实是决策的一部分 —— 只有 Intelligence
#:     知道它派出的是个两小时的深度研究还是两秒钟的查表
#:   · 但"父 Run 什么时候不再等"是**控制** —— 它决定一条挂住的 Run
#:     能挂多久，而"挂住的父 Run"正是 M37~M42 连续四轮在治的形状
#:
#: 把裁决权一起交出去，一个幻觉出来的 `wait_timeout_seconds: 99999999`
#: 就能让父 Run 挂三年，而界面上显示的还是"在等子 Agent，一切正常"。
#:
#: 所以 D-31（这次派生可以自己说等多久）与 D-32（说出来的数必须落在
#: `(0, MAX]` 内）是一份设计，不是两个功能。
#:
#: 它与 `016_child_wait_ceiling.sql` 里那句 `interval '6 hours'` 必须相等 ——
#: 与 015 那句 `interval '30 minutes'` 是同一条规矩，改一边忘另一边会红。
MAX_CHILD_WAIT_TIMEOUT = timedelta(hours=6)


class ChildRunKind(str, Enum):
    """子 Run 的形态。决定挂起原因与"target"字段怎么解释。"""

    AGENT = "agent"          # 子 AgentRun（A2A）
    SKILL = "skill"          # 子 SkillRun


#: 哪些 Action 是"派生子 Run"而不是"干一件活"。
#: 这张表是 `ACTION_TO_TASK` 的反面：那里说"产生什么 Task"，
#: 这里说"哪些 Task 不该被 Worker 当普通活干"。
CHILD_RUN_ACTIONS: dict[ActionType, ChildRunKind] = {
    ActionType.AGENT_DELEGATION: ChildRunKind.AGENT,
    ActionType.SKILL_CALL: ChildRunKind.SKILL,
}


def child_run_kind_of(action_type: Any) -> ChildRunKind | None:
    """`SKILL_CALL` / `AGENT_DELEGATION` → 形态；其余 → None（是普通活）。"""
    return CHILD_RUN_ACTIONS.get(action_type)


def _check_wait_bounds(who: str, agreed: timedelta, maximum: timedelta) -> None:
    """D-32：一次派生的等待上限必须落在 `(0, maximum]` 内。

    `who` 是报错里点名的那个对象 —— 提议侧（"这次派生要了多久"）与
    冻结侧（"这一行的上限是多少"）用的是同一条判据，但**被问的人不同**，
    于是报错能说出是哪一个越了界（PR-19）。
    """
    if agreed <= timedelta(0):
        raise InvariantViolation(
            f"D-32: {who} is {agreed}, which is not after its spawn; a derivation "
            f"that is already overdue at the moment it is created never gets a "
            f"chance to be waited for — the deadline must be strictly later than "
            f"spawned_at"
        )
    if agreed > maximum:
        raise InvariantViolation(
            f"D-32: {who} is {agreed}, which is longer than the ceiling {maximum}; "
            f"a derivation may ask for a longer wait (D-31), but no derivation may "
            f"keep its parent suspended for longer than the platform allows — "
            f"shorten the request, or raise the ceiling in "
            f"016_child_wait_ceiling.sql (it is a physical constraint, not a knob)"
        )


def _as_wait_timeout(value: Any) -> timedelta | None:
    """D-31 / D-32：把调用方的声明归一化成一个 `timedelta`。

    --------------------------------------------------------------------------
    为什么 `bool` 要**先**挡掉

    `True` 是 `int` 的子类。`isinstance(True, int)` 为真，于是
    `timedelta(seconds=True)` 等于"等 1 秒" —— 一次手滑被静默采纳，
    而它的后果是父 Run 在一秒之后就被判"等不到"（PR-34：宁可拒绝，不要编）。

    --------------------------------------------------------------------------
    为什么字符串**不**解析

    模型输出的 JSON 里出现 `"3600"` 与 `3600` 是两种东西：前者多半是
    prompt 没约束住，后者才是声明。静默解析前者，等于替一次格式错误
    编出一个含义；拒绝它会让那次派生**带着原因**失败（PR-19）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvariantViolation(
            f"D-32: wait_timeout must be a duration, got bool {value!r}; "
            f"True is an int in Python and would silently become a 1-second wait"
        )
    if isinstance(value, timedelta):
        timeout = value
    elif isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise InvariantViolation(
                f"D-32: wait_timeout must be a finite number of seconds, "
                f"got {value!r}; a wait that lasts forever or never is not a "
                f"deadline (D-18)"
            )
        timeout = timedelta(seconds=float(value))
    else:
        raise InvariantViolation(
            f"D-32: wait_timeout must be a timedelta or a number of seconds, "
            f"got {type(value).__name__} ({value!r}); refusing rather than "
            f"guessing what it means (PR-34)"
        )
    _check_wait_bounds(f"the requested wait_timeout ({timeout})", timeout, MAX_CHILD_WAIT_TIMEOUT)
    return timeout


def _resolve_ceiling(max_wait_timeout: timedelta | None) -> timedelta:
    """D-33：登记处的裁决上限。部署只能**更严格**，不能更宽松。

    平台上限是 016 的一条 CHECK（物理的），而它不是配置。
    于是这里允许部署把上限调小（更严），但调大只会被数据库挡回来 ——
    提前在这里点名拒绝，是为了让那条错发生在**启动时**而不是
    第一次派生时（PR-19）。
    """
    if max_wait_timeout is None:
        return MAX_CHILD_WAIT_TIMEOUT
    if max_wait_timeout <= timedelta(0):
        raise InvariantViolation(
            f"D-32: max_wait_timeout must be positive, got {max_wait_timeout}; "
            f"a registry that refuses every wait cannot spawn a child run at all"
        )
    if max_wait_timeout > MAX_CHILD_WAIT_TIMEOUT:
        raise InvariantViolation(
            f"D-32: max_wait_timeout {max_wait_timeout} exceeds the platform "
            f"ceiling {MAX_CHILD_WAIT_TIMEOUT}; the ceiling is a physical "
            f"constraint (016), so raising it requires a migration, not a config"
        )
    return max_wait_timeout


def freeze_wait_deadline(
    handle: "ChildRunHandle",
    *,
    default: timedelta,
    maximum: timedelta,
) -> "ChildRunHandle":
    """D-18 / D-31 / D-32 / D-34：裁决并冻结一次派生的等待上限。

    这是"一次派生的上限到底是多少"的**唯一**一处定义（B-7）：
    内存登记处与 PG 登记处都调它，于是替身与真库不可能给出两个答案。

    D-34：冻结之后 handle 上的 `wait_timeout` 会被**清掉**。
    一个 handle 上同时挂着"这次要了多久"和"约定到什么时候"，
    就有了两个上限；清掉之后"这次派生约定了多久"只有一个答案：
    `wait_until - spawned_at`。
    """
    if handle.wait_until is None:
        # 归一化放在**这里**而不是"谁造了这个 handle"那里（PR-23）：
        # 生产路径上 `wait_timeout` 已经在 `ChildRunRequest` 里归一过一次，
        # 但 `ChildRunHandle` 是可以被手搓的（测试、回填、将来的第二个 spawner）。
        # 只认 `timedelta` 的话，一个 `90` 会在下面那行 `spawned_at + 90` 上
        # 炸出 `TypeError` —— 响是响了，可它没说出是谁要了多少（PR-19）。
        declared = _as_wait_timeout(handle.wait_timeout)
        agreed = declared if declared is not None else default
        handle = replace(handle, wait_until=handle.spawned_at + agreed)
    else:
        # 已经冻结过（从库里读回来的行）：不改写 —— 改写会把
        # "当时约定等多久"换成一个新的数（D-18）。
        # 但边界**照样**过一遍：判据是关于这一行的，不是关于"谁填的"。
        agreed = handle.wait_until - handle.spawned_at
    _check_wait_bounds(
        f"the wait deadline of child run {handle.child_run_id!r} ({agreed} after "
        f"its spawn)",
        agreed,
        maximum,
    )
    return replace(handle, wait_timeout=None)


class ChildRunUnavailable(Exception):
    """派生不了，且**说清楚为什么**。

    和 `ConfigurationError` 同一类：缺了就不能假装能跑。
    刻意不复用 `ExecutorError` —— 这件事发生在派发**之前**（Loop 侧），
    用 Kernel 的执行错误码会让人以为"Worker 跑了一次然后失败了"，
    而事实是**根本没派发出去**。
    """


@dataclass(frozen=True)
class ChildRunRequest:
    """派生一条子 Run 的请求。"""

    parent_run_id: str
    #: D-1：派生键。跨 Attempt 稳定（E-21），所以重试拿回同一条子 Run。
    parent_execution_id: str
    kind: ChildRunKind
    #: AGENT → 目标 agent_id；SKILL → 技能名
    target: str
    #: 引起这次派生的那个 Action。**必须一起落库**（S-1）：
    #: 它挂着 `compensation`（逆操作声明），而父 Run 在**恢复之后**才等到子 Run 的结果 ——
    #: 那一刻如果拿不到 Action，`_record_compensation` 会静默跳过，
    #: 补偿账本就缺一条，缺的正是子 Run 留在外部世界的副作用（A-12：变错）。
    action: Any = None
    #: 父侧那条 Task 的 id。补偿记录要它（`CompensationRecord.task_id`）。
    #: 同理：恢复后手上没有 Task 对象，只有一个 id 可以带过来。
    parent_task_id: str = ""
    instruction: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    #: D-31：这一**次**派生自己声明的等待上限。
    #:
    #: 接受 `timedelta` 或"秒数"；`None` 表示"这次不说" → 用登记处的全局默认。
    #: 归一化与边界检查在 `__post_init__` 里做完，于是下游拿到的永远是一个
    #: `timedelta` 或 `None` —— 不必再猜一次（B-7：一个事实一处定义）。
    #:
    #: 为什么它是"这次说了算，没说才听默认的"，而不是"覆盖全局默认"：
    #: "这次派生该等多久"是**一次派生**的属性，而全局默认是**一次部署**的属性。
    #: 用后者回答前者的代价是双向的（见 §81.1）。
    wait_timeout: Any = None

    def __post_init__(self) -> None:
        if not self.parent_run_id:
            raise InvariantViolation("D-2: ChildRunRequest.parent_run_id is required")
        if not self.parent_execution_id:
            raise InvariantViolation(
                "D-1: ChildRunRequest.parent_execution_id is required — it is the "
                "derivation key, without it a retry would spawn a second child run"
            )
        if not self.parent_task_id:
            raise InvariantViolation(
                "D-2: ChildRunRequest.parent_task_id is required — the compensation "
                "ledger names the task that produced the side effect; without it a "
                "restored run cannot record the undo"
            )
        if self.action is None:
            raise InvariantViolation(
                "S-1: ChildRunRequest.action is required — it carries the "
                "compensation declaration; a spawn that cannot say what it is "
                "undoing leaves an unrecorded side effect"
            )
        if not isinstance(self.kind, ChildRunKind):
            raise InvariantViolation("D-2: ChildRunRequest.kind must be a ChildRunKind")
        if not self.target:
            raise InvariantViolation("D-2: ChildRunRequest.target is required")
        object.__setattr__(self, "payload", dict(self.payload))
        # D-32：声明在这里就被裁决一次。放在请求侧而不是等 `bind()`，
        # 是因为**这里才认识调用方**：它知道是哪个 payload 字段、哪个模型输出
        # 写出了这个数，于是报错能说出"谁要求了多少"（PR-19）。
        # `bind()` 那一道仍然要有 —— 它守的是全局默认被配错的情况（PR-23）。
        object.__setattr__(self, "wait_timeout", _as_wait_timeout(self.wait_timeout))


@dataclass(frozen=True)
class ChildRunHandle:
    """派生结果。

    `child_run_id` 是**唯一**能用来回查子 Run 的东西 ——
    它会被写进 `Suspension.wait_condition`，于是"父 Run 在等谁"有据可查，
    而不是一句"在等子 Agent"。
    """

    child_run_id: str
    kind: ChildRunKind
    parent_run_id: str
    parent_execution_id: str
    target: str
    #: 引起这次派生的 Action（含 compensation）。见 `ChildRunRequest.action`。
    action: Any = None
    parent_task_id: str = ""
    status: str = "created"
    spawned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: M30 / 空洞 209：子 Run **终态的结果**（`010_child_run_result.sql`）。
    #:
    #: 为什么结果必须住在登记处而不只住在事件里（X-5）：
    #: Kafka 有 retention，PG 没有。结果是"这条子 Run 干了什么"的**事实**，
    #: 事实属于 Truth（PG）；事件只是"去叫醒父 Run"的**消息**，属于 Log。
    #: 只放在 Log 里，等于让 Truth 少一块 —— 而缺的这块正是补偿账本要用的。
    #:
    #: 这也是唤醒路径能**不依赖 Kafka** 的前提：事件丢了，扫 PG 照样拿得到结果。
    result: Mapping[str, Any] = field(default_factory=dict)
    #: 终态时刻。非 NULL ⟺ status 是终态（D-6，由 CHECK 钉住）。
    completed_at: datetime | None = None
    #: 结果交回父 Run 的时刻。非 NULL ⟺ 父 Run 已经被唤醒过（D-7）。
    delivered_at: datetime | None = None
    #: D-14：父侧叫停的**痕迹**。`012_child_run_cancel_request.sql`
    #:
    #: 这一格**不是终态**（D-14），也**不会**被结算清掉 ——
    #: 它回答的是"这次派生的结局是不是发生在被叫停之后"，
    #: 而 `run_cancellations`（011）回答的是"谁要求停这条 Run"、
    #: 并且会被 `settle()` 结掉。两个问题不同，生命周期也不同，
    #: 所以它们各有一格而不是共用一格（B-7 的另一半：
    #: 一个事实一处定义，指的是**同一个问题**不许有两个答案）。
    cancel_requested_at: datetime | None = None
    #: B-8：叫停的归因。与 `cancel_requested_at` 同进同退（CHECK 钉住）。
    cancel_reason: str = ""
    cancel_requested_by: str = ""
    #: D-31：这次派生**自己声明**等多久。**只在冻结之前有意义**：
    #: `bind()` 裁决完会把它清掉（D-34），于是 handle 上只剩 `wait_until` 一个答案。
    #:
    #: 为什么它必须挂在 handle 上而不是 `bind()` 的一个额外参数：
    #: 冻结点只有 `bind()` 一个，而"这次派生要了多久"必须**随这次派生一起**
    #: 走到那个点。让它走另一条路（参数），就等于"上限从哪来"有了两个入口 ——
    #: 其中一个漏掉时，表现是"这次要的 2 小时没人听见"（静默，PR-34）。
    wait_timeout: timedelta | None = None
    #: D-18：这次派生的**等待上限**。在第一次 `bind()` 那一刻固化（不后来补）。
    #:
    #: 为什么它**不是**一个"超时重试试试看"的参数：到期之后的事实是
    #: "没有结果，且不知道为什么"，不是"失败了"。这两件事的处置完全不同
    #: （D-19），所以这里存的不是重试次数而是一个**时刻** ——
    #: 时刻能被 SQL 拿来比较（`idx_child_runs_overdue` 的谓词就靠它）。
    wait_until: datetime | None = None
    #: D-20：等待到期的**处置时刻**。非 NULL ⟺ 已经处置过一次。
    #:
    #: 它**不是终态**（与 `cancel_requested_at` 同款）：这条子 Run 后来
    #: 真的交回结果时，唤醒路径照样认它 —— `undelivered()` 的谓词里没有这一列。
    #: 它唯一的作用是让出 `overdue()` 的队首（R-13 的同款形状）。
    wait_expired_at: datetime | None = None

    @property
    def is_wait_expired(self) -> bool:
        """这次等待是不是已经到期处置过。"""
        return self.wait_expired_at is not None

    def is_overdue(self, now: datetime) -> bool:
        """D-18：到 `now` 为止，这次等待是不是已经超期且**还没处置**。

        `wait_until is None` → False：历史行（015 回填之前的那些）
        不该被自动判成逾期。与 `RunCancellation.is_expired` 同一条处理 ——
        "没有约定过上限"不等于"上限已过"。
        """
        if self.wait_until is None:
            return False
        # `not self.is_wait_expired` 这一半就是 R-13 的那一格：
        # 处置过却没退出队列的行，`wait_until` 永远最小，
        # 于是它永久占着 `overdue()` 的队首 —— 攒够 LIMIT 条之后，
        # 真正需要处理的新到期一个也进不来。
        return (
            not self.is_finished
            and not self.is_delivered
            and not self.is_wait_expired
            and now > self.wait_until
        )

    @property
    def is_cancel_requested(self) -> bool:
        """父侧有没有叫停过它。

        **不看 status**：请求不是终态（D-14），一条被叫停过的子 Run
        可以同时是 `created`（还在跑）、`completed`（跑赢了赛跑）
        或 `cancelled`（它读到请求后自己停了）。
        """
        return self.cancel_requested_at is not None

    @property
    def finished_after_cancel_request(self) -> bool:
        """D-16：赛跑里"完成赢"的那一半 —— 被叫停过，却还是跑到了终态。

        这一格是**运维要看的第一张清单**：这些子 Run 的副作用已经落进外部世界，
        而父 Run 早就把它们判成"不必再要了"，于是账本必须由 D-13 接住。
        """
        return self.is_finished and self.is_cancel_requested

    @property
    def is_finished(self) -> bool:
        """跑完了没有。**不看 status 字符串** —— 看 completed_at。

        D-6 让两者必须同进同退，于是"哪个才是真的"这个问题没有第二个答案。
        反过来若这里写 `status in (...)`，就等于在 Python 侧复制一份终态集合，
        而 SQL 里已经有一份（B-7 同款理由：一个事实不许有两个定义）。
        """
        return self.completed_at is not None

    @property
    def is_delivered(self) -> bool:
        return self.delivered_at is not None


@dataclass(frozen=True)
class ChildRunIdentity:
    """M30：一条 Run 作为**子 Run** 的身份 —— 我是谁 + 我向谁汇报。

    为什么这两样必须绑成一个对象：

    只有"我是谁"（`handle`）而没有"向谁汇报"（`registry`），
    子 Run 跑到终态时结果**无处可写** —— 而那正是空洞 212~214 的形状：
    不报错，只是结果永远不来，父 Run 永远等一个不会来的东西。

    反过来只有 registry 没有 handle，就回到被推翻的那条老路：
    让子 Run 拿自己的 `run_id` 去登记处反查，而"能不能反查到"
    由**每份 provider 工厂**决定。

    绑在一起之后，漏掉一半在物理上就写不出这个对象 ——
    这是 209 与 212~214 真正的差别：前者无处可写时会**说**，
    后者无处可写时会**沉默**。
    """

    handle: ChildRunHandle
    registry: Any  #: `ChildRunRegistryPort`（标注成 Protocol 会与 dataclass 的相等性冲突）


class ChildRunRegistryPort(Protocol):
    """派生的登记处。两个实现：内存（测试）与 PG（`008_child_runs.sql`）。

    端口而不是具体类 —— 组合根要能在两者之间换，
    而"换掉它之后测试还红不红"正是 PR-23 的判据：
    D-1 约束的是**登记处是不是持久的**，不是 Loop 有没有先查一下。
    """

    def bind(self, handle: ChildRunHandle) -> ChildRunHandle: ...
    def for_execution(self, parent_execution_id: str) -> ChildRunHandle | None: ...
    def for_child(self, child_run_id: str) -> ChildRunHandle | None: ...
    def children_of(self, parent_run_id: str) -> tuple[ChildRunHandle, ...]: ...

    def mark_finished(
        self,
        child_run_id: str,
        status: str,
        result: Mapping[str, Any],
        *,
        completed_at: datetime | None = None,
    ) -> ChildRunHandle: ...

    def request_cancel(
        self,
        child_run_id: str,
        *,
        reason: str,
        by: str,
        requested_at: datetime | None = None,
    ) -> ChildRunHandle:
        """D-14：登记一条**取消请求**，不写终态。

        与 `mark_finished` 的区别不是"多一个状态"，是**谁有资格说话**：
        `mark_finished` 只有子 Run 自己能调（它知道它跑到哪一步了），
        `request_cancel` 只有父 Run 能调（它只知道"我不要这个结果了"）。
        把两者合成一个方法，就等于允许父 Run 替一个它看不见的对象作证。
        """
        ...

    def mark_delivered(
        self, child_run_id: str, *, delivered_at: datetime | None = None
    ) -> bool: ...

    def undelivered(self, limit: int = 64) -> Sequence[ChildRunHandle]: ...

    def overdue(self, now: datetime, limit: int = 64) -> Sequence[ChildRunHandle]:
        """D-18：等到上限还没有结果、且还没处置过的那些派生。

        与 `undelivered()` 是**两支不同的队列**，刻意不做成一个方法加参数：

            undelivered()  有结果没交回  → 交回结果（唤醒路径，D-7）
            overdue()      没结果且超期  → 不再等（到期路径，D-19）

        前者是"好事迟到了"，后者是"不知道还有没有好事"。
        并进一条队列就会用同一个 `mark_*` 去结掉两种结局，
        而"结掉"在后者身上是**说谎**（PR-19）。
        """
        ...

    def mark_wait_expired(
        self, child_run_id: str, *, expired_at: datetime | None = None
    ) -> bool:
        """D-20：登记"这次等待已经处置过"，让它退出 `overdue()` 的队首。

        刻意**不**动 `status` / `completed_at` / `delivered_at`（D-20 那一节）。
        """
        ...


class ChildRunRegistry:
    """`parent_execution_id → ChildRunHandle`（D-1 的落点）—— **内存版**。

    ⚠️ 它只在**进程活着**的时候守得住 D-1。重启即失效：
    新进程拿到的是一张空表，父 Execution 被 Recovery 重新驱动时
    会派生出第二条子 Run（M26 探针实测）。

    生产必须用 `PostgresChildRunRegistry`（`008_child_runs.sql`）：
    那里 D-1 是 `UNIQUE(parent_execution_id)`，一条物理约束。
    """

    def __init__(
        self,
        wait_timeout: timedelta = DEFAULT_CHILD_WAIT_TIMEOUT,
        max_wait_timeout: timedelta | None = None,
    ) -> None:
        self.wait_timeout = wait_timeout
        #: D-33：裁决上限。部署只能比平台上限**更严格**（`_resolve_ceiling`）。
        self.max_wait_timeout = _resolve_ceiling(max_wait_timeout)
        self._by_execution: dict[str, ChildRunHandle] = {}
        self._by_run: dict[str, list[ChildRunHandle]] = {}
        self._by_child: dict[str, ChildRunHandle] = {}

    def bind(self, handle: ChildRunHandle) -> ChildRunHandle:
        """登记。**已存在则返回第一次那条**（D-1），不覆盖。

        覆盖等于抹掉幂等本身 —— 第二次派生出来的那条子 Run
        从此谁也查不到，却照样在跑。
        """
        existing = self._by_execution.get(handle.parent_execution_id)
        if existing is not None:
            return existing
        # D-18：等待上限在**第一次登记**这一刻固化。
        #
        # 为什么在这里而不是在 `ChildRunHandle` 构造时算：
        # 构造 handle 的地方（`InProcessChildRunSpawner.spawn`）不是唯一入口 ——
        # 跨进程派生走的是 PG 的 `bind()`。放在登记处，
        # "上限是谁定的"就只有一个答案（B-7 / R-11 同款理由）。
        #
        # 已经带着上限来的（从 PG 读回来的行）不改写 ——
        # 改写会把"当时约定等多久"换成一个新的数。
        #
        # D-31/D-32/D-34：裁决与清场都在 `freeze_wait_deadline` 里，
        # 与 PG 登记处共用同一份定义（替身与真库不许给出两个答案）。
        handle = freeze_wait_deadline(
            handle, default=self.wait_timeout, maximum=self.max_wait_timeout
        )
        self._by_execution[handle.parent_execution_id] = handle
        self._by_run.setdefault(handle.parent_run_id, []).append(handle)
        self._by_child.setdefault(handle.child_run_id, handle)
        return handle

    def for_execution(self, parent_execution_id: str) -> ChildRunHandle | None:
        return self._by_execution.get(parent_execution_id)

    def for_child(self, child_run_id: str) -> ChildRunHandle | None:
        """按子 Run 自己的 id 回查 —— 唤醒路径手上只有这个 id。

        快照里存的是"我在等**哪一条**子 Run"（R-6），
        事件里带的也是 child_run_id；派生键 `parent_execution_id` 反而是唤醒时不知道的。
        """
        return self._by_child.get(child_run_id)

    def children_of(self, parent_run_id: str) -> tuple[ChildRunHandle, ...]:
        return tuple(self._by_run.get(parent_run_id, ()))

    # ---------------------------------------------------------- 结果 / 交付（M30）
    def mark_finished(
        self,
        child_run_id: str,
        status: str,
        result: Mapping[str, Any],
        *,
        completed_at: datetime | None = None,
    ) -> ChildRunHandle:
        existing = self._must(child_run_id)
        if existing.is_finished:
            # 幂等：同一条事件被重复投递，结果一样 → 直接返回，不重写。
            # 若**结果不一样**则不是重复，是 B-3 的破口 —— 终态子 Run
            # 不可能变成另一个终态。静默接受会让父 Run 拿到第二种说法。
            if existing.status != status:
                raise InvariantViolation(
                    f"B-3: child run {child_run_id!r} is already "
                    f"{existing.status!r}; it cannot become {status!r}"
                )
            return existing
        return self._store(
            replace(
                existing,
                status=status,
                result=dict(result),
                completed_at=completed_at or datetime.now(timezone.utc),
            )
        )

    def request_cancel(
        self,
        child_run_id: str,
        *,
        reason: str,
        by: str,
        requested_at: datetime | None = None,
    ) -> ChildRunHandle:
        """D-14：登记请求。**不写终态**，也**不改**已经写下的终态。

        ------------------------------------------------------------------
        三个"什么都不做"，各自理由不同

        1. 子 Run 已经终态 → 原样返回（D-15）。
           这是赛跑里"完成赢"的那一半，不是错误：
           取消只能拦住还没产生的结果。
        2. 已经请求过 → 原样返回，**不覆盖第一次的原因**。
           第二次叫停通常来自另一个人（用户点了第二次、Sweeper 又扫到），
           把 `cancel_reason` 改写成第二次的那条，
           等于让审计回答不了"最初是谁叫的"（A-8）。
        3. reason / by 为空 → 抛（B-8）。
           一条查不到归因的叫停，等于取消这件事没发生过。

        ------------------------------------------------------------------
        为什么这里**不**抛"已经终态"

        调用方（`AgentLoop._cancel_pending_child`）要拿返回值**分辨**赛跑的
        两种结局：它停了、还是它先跑完了。抛异常会把"它跑赢了"伪装成
        "叫停失败"，而那两件事的处置完全不同（D-16）。
        """
        if not reason:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty reason; "
                "a cancellation nobody can explain is unauditable"
            )
        if not by:
            raise InvariantViolation(
                "B-8: request_cancel requires a non-empty 'by'; "
                "an anonymous cancellation cannot be attributed (A-8)"
            )
        existing = self._must(child_run_id)
        if existing.is_finished:
            return existing
        if existing.is_cancel_requested:
            return existing
        return self._store(
            replace(
                existing,
                cancel_requested_at=requested_at or datetime.now(timezone.utc),
                cancel_reason=reason,
                cancel_requested_by=by,
            )
        )

    def mark_delivered(
        self, child_run_id: str, *, delivered_at: datetime | None = None
    ) -> bool:
        existing = self._must(child_run_id)
        if existing.is_delivered:
            return False
        if not existing.is_finished:
            # D-7：不能交付一个还没有的结果。静默放行会让
            # "父 Run 被叫醒了"与"结果其实还没产生"同时成立。
            raise InvariantViolation(
                f"D-7: child run {child_run_id!r} has no result yet; "
                f"mark_finished() must run before mark_delivered()"
            )
        self._store(
            replace(
                existing, delivered_at=delivered_at or datetime.now(timezone.utc)
            )
        )
        return True

    def undelivered(self, limit: int = 64) -> list[ChildRunHandle]:
        pending = [
            h for h in self._by_child.values() if h.is_finished and not h.is_delivered
        ]
        pending.sort(key=lambda h: h.completed_at or h.spawned_at)
        return pending[:limit]

    def overdue(self, now: datetime, limit: int = 64) -> list[ChildRunHandle]:
        """D-18 的队首。**排序键是 `wait_until`**，与 015 的索引一致。

        排序键为什么是上限而不是 `spawned_at`：索引那边按 `wait_until` 排，
        这里若按 `spawned_at` 排，替身（sqlite）与真库（PG）会给出**不同的
        队首顺序** —— 而"谁先被处置"在 LIMIT 下是会影响结果的。
        """
        due = [h for h in self._by_child.values() if h.is_overdue(now)]
        due.sort(key=lambda h: h.wait_until or h.spawned_at)
        return due[:limit]

    def mark_wait_expired(
        self, child_run_id: str, *, expired_at: datetime | None = None
    ) -> bool:
        existing = self._must(child_run_id)
        if existing.is_wait_expired:
            return False
        if existing.is_finished:
            # 结果来了：不再是"等不到"，该走唤醒路径（D-7），走到期路径是错的。
            raise InvariantViolation(
                f"D-20: child run {child_run_id!r} already produced a result "
                f"({existing.status!r}); its wait cannot expire — deliver it "
                f"instead of declaring the wait over"
            )
        self._store(
            replace(
                existing, wait_expired_at=expired_at or datetime.now(timezone.utc)
            )
        )
        return True

    # ---------------------------------------------------------- 内部
    def _must(self, child_run_id: str) -> ChildRunHandle:
        handle = self._by_child.get(child_run_id)
        if handle is None:
            raise InvariantViolation(
                f"D-2: child run {child_run_id!r} is not registered; "
                f"a result can only be recorded for a child run that was bound"
            )
        return handle

    def _store(self, handle: ChildRunHandle) -> ChildRunHandle:
        """把 handle 的新版本放回**三个**索引 —— 漏一个就会出现
        "按 execution 查到旧的、按 child 查到新的"这种两个答案的状态。"""
        self._by_child[handle.child_run_id] = handle
        self._by_execution[handle.parent_execution_id] = handle
        siblings = self._by_run.setdefault(handle.parent_run_id, [])
        for i, old in enumerate(siblings):
            if old.child_run_id == handle.child_run_id:
                siblings[i] = handle
                break
        else:  # pragma: no cover - bind() 已经写过
            siblings.append(handle)
        return handle

    def __len__(self) -> int:
        return len(self._by_execution)


class ChildRunSpawner(Protocol):
    """派生一条子 Run。Loop 只认这个端口，不认识任何具体形态。

    **B-9：起得出，就必须叫得停。**

    `cancel_child` 是这个端口的第二 half，不是可选项。
    一个只能派生不能叫停的 spawner，会让 `AgentLoop.cancel()` 在
    "这条 Run 正在等子 Run"时无事可做 —— 于是取消只停了一半：
    父 Run 判了 `CANCELLED`，子 Run 还在别的进程里花钱、产生副作用。

    把它写进端口而不是靠 `getattr` 碰运气，理由与 PR-26 同款：
    兜底不能是主要保证，而"碰不到就跳过"比没有兜底更糟 ——
    它把一个失败伪装成了一次成功。
    """

    def spawn(self, request: ChildRunRequest) -> ChildRunHandle: ...

    def cancel_child(
        self, child_run_id: str, *, reason: str, by: str = "system"
    ) -> ChildRunHandle: ...


@dataclass
class InProcessChildRunSpawner:
    """进程内派生：用与 Control Plane 同一个 `factory` 起一条真的子 Run。

    `factory` 的签名与 `packages.agent_api.service.InProcessControlPlane.factory`
    完全一致（`Callable[[str, ApprovalStore], RuntimeStack]`）——
    **刻意不另发明一个**：起一个 Run 只有一种方式，
    多一种就多一套"Run 是怎么开始的"的定义。

    Skill 走同一条路，target 转成 `skill:<name>`：
    这是 §25 的 **Agentic Skill** 形态，不是假装 Skill Runtime 存在。
    """

    factory: Callable[[str, Any], RuntimeStack]
    approvals: Any = None
    registry: ChildRunRegistry = field(default_factory=ChildRunRegistry)
    #: 起一条子 Run 时要不要立刻 `start()`。
    #: 默认 True —— 不起步的 Run 在存储里是一条"还没开始的 SUSPENDED"，
    #: 与"派生失败"无法区分，排障时是死路。
    auto_start: bool = True
    #: child_run_id → 那条子 Run 的 stack。只有进程内派生才拿得到 ——
    #: 跨进程时子 Run 在别的机器上，这里自然是空的，
    #: `drive()` 便会如实说"我驱动不了它"。
    _stacks: dict[str, Any] = field(default_factory=dict, repr=False)

    def spawn(self, request: ChildRunRequest) -> ChildRunHandle:
        # D-1：先查再派。顺序反了就会在每次重试都多开一条。
        existing = self.registry.for_execution(request.parent_execution_id)
        if existing is not None:
            return existing

        if self.factory is None:
            raise ChildRunUnavailable(
                f"no child-run factory configured; cannot spawn "
                f"{request.kind.value} {request.target!r} for run {request.parent_run_id!r}"
            )

        target = request.target
        if request.kind is ChildRunKind.SKILL:
            target = f"skill:{request.target}"

        stack = self.factory(target, self.approvals)
        if self.auto_start:
            stack.start(request.instruction or request.target)
        handle = ChildRunHandle(
            child_run_id=stack.run_id,
            kind=request.kind,
            parent_run_id=request.parent_run_id,
            parent_execution_id=request.parent_execution_id,
            target=request.target,
            action=request.action,
            parent_task_id=request.parent_task_id,
            status="created",
            # D-31：把这次派生的声明带到冻结点（`bind()`）。
            # 若 D-1 让 `bind()` 交回**第一条**子 Run，这个值就自然作废了 ——
            # 那条子 Run 的上限在它自己第一次登记时就冻好了（D-18）。
            wait_timeout=request.wait_timeout,
            # D-31：把这次派生的声明带到冻结点（`bind()`）。
            # 若 D-1 让 `bind()` 交回**第一条**子 Run，这个值就自然作废了 ——
            # 那条子 Run 的上限在它自己第一次登记时就冻好了（D-18）。
        )
        bound = self.registry.bind(handle)
        # 只在**第一次**派生时记 stack：重复派生拿回的是第一次那条，
        # 把第二次的 stack 记进去会把"正在跑的那条"悄悄换掉（D-1 的另一半）。
        self._stacks.setdefault(bound.child_run_id, stack)

        # M30 / 空洞 209：告诉这条子 Run **它自己是子 Run**。
        #
        # 没有这一步，子 Run 跑到终态时无从知道自己该向谁汇报 ——
        # 之前的设计是让子 Run 拿自己的 run_id 去登记处反查，
        # 而"子 Run 的栈里有没有接 spawner"由**每份 provider 工厂**决定。
        # 漏接了不报错，只是 `child_run.completed` 永远不发，
        # 父 Run 永远等一个不会来的结果 —— 正是空洞 212~214 那个形状。
        #
        # 只有 `spawn()` 同时握着 stack、handle 和登记处，所以写在这里是唯一的落点。
        if getattr(stack, "loop", None) is not None:
            stack.loop.child_identity = ChildRunIdentity(bound, self.registry)
        return bound

    def stack_for(self, child_run_id: str) -> Any | None:
        return self._stacks.get(child_run_id)

    def cancel_child(
        self, child_run_id: str, *, reason: str, by: str = "system"
    ) -> ChildRunHandle:
        """B-9：叫停一条我派出去的子 Run。

        ------------------------------------------------------------------
        D-14：我**不替它宣告终态**，只登记"我要它停"

        旧实现在跨进程那条路上调 `mark_finished(..., 'cancelled')`。
        那是父 Run 替一条它看不见的 Run 作证：
        父 Run 不知道那条子 Run 跑到第几步，却宣布它"已经停了"。
        于是真相与记录分岔 —— 而分岔的代价由**子 Run** 付：
        它跑完的那一刻调 `mark_finished('completed')`，撞 B-3 抛异常，
        真实结果（含 S-1 的撤销参数）随之丢失（空洞 224 的脸 A）。

        所以终态只有一个作者：**那条子 Run 自己**
        （`AgentLoop._emit_child_run_outcome`）。

        ------------------------------------------------------------------
        两条路，按能力走

        1. 进程内（`_stacks` 里有它的 stack）→ 调它自己的 `cancel()`。
           这样它是**自己**被叫停的：关自己的闸门、记自己的账本、
           发自己的 `child_run.cancelled`（于是它的孙 Run 也一起停 —— 递归）。
           它写下的终态由它自己负责，这里只是**读回来**。
        2. 跨进程（stack 不在内存里）→ 只能登记请求（D-14）。
           "让那条 Run 真的停下来"的通道是 011 的 `run_cancellations`，
           由 `AgentLoop._cancel_pending_child` 落库 ——
           意图只有一处落点，这里**不**再写第二份（B-7）。

        ------------------------------------------------------------------
        已经终态的叫不停（B-3 / D-15）

        一条已经 COMPLETED 的子 Run 不可能变成 CANCELLED ——
        强行记一笔会让父 Run 拿到**第二种说法**。
        所以这里如实把现状交回去。调用方**必须看返回值**：

            `is_finished` 且 `status == 'cancelled'` → 它停了
            `is_finished` 且 `status != 'cancelled'` → 它跑完了（脸 B）
            未终态                                    → 它还在跑（脸 A 之前）
        """
        existing = self.registry.for_child(child_run_id)
        if existing is not None and existing.is_finished:
            return existing

        stack = self._stacks.get(child_run_id)
        if stack is not None:
            loop = getattr(stack, "loop", None)
            if loop is not None:
                run = getattr(loop, "agent_run", None)
                if run is None or not run.is_terminal:
                    loop.cancel(reason=reason, by=by)
            # D-14：读回**它自己**写下的终态。写过了就交回去，
            # 没写过（这条子 Run 没有自报终态的能力）→ 落到下面登记请求。
            after = self.registry.for_child(child_run_id)
            if after is not None and after.is_finished:
                return after

        return self.registry.request_cancel(child_run_id, reason=reason, by=by)

    def drive(self, child_run_id: str) -> Any:
        """把子 Run 跑到终态，返回它的终态 State。

        只有进程内派生做得到 —— 跨进程时子 Run 在**别的进程**里，
        驱动它是那个进程的事，这里如实说"我驱动不了"，绝不假装跑过。

        刻意**不**把结果直接交回父 Loop：那是唤醒路径的职责
        （子 Run 完成事件 → Wake-up → `AgentLoop.child_completed()`）。
        合并进来会让"子 Run 在另一个进程跑完"这条真路径失去对应物。
        """
        stack = self._stacks.get(child_run_id)
        if stack is None:
            raise ChildRunUnavailable(
                f"child run {child_run_id!r} is not driven by this process; "
                f"it has no local stack — whatever runs it must emit the completion "
                f"event that wakes the parent run"
            )
        return stack.run()


__all__ = [
    "CHILD_RUN_ACTIONS",
    "DEFAULT_CHILD_WAIT_TIMEOUT",
    "MAX_CHILD_WAIT_TIMEOUT",
    "ChildRunHandle",
    "ChildRunIdentity",
    "ChildRunKind",
    "ChildRunRegistry",
    "ChildRunRegistryPort",
    "ChildRunRequest",
    "ChildRunSpawner",
    "ChildRunUnavailable",
    "InProcessChildRunSpawner",
    "child_run_kind_of",
    "freeze_wait_deadline",
]
