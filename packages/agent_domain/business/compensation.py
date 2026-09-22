"""补偿（Saga / Compensation）—— M10。

基线 §42 把 M10 定义为「建立在 M15 Kernel 之上」的 Advanced Execution。
这一层**不新增执行通道**：撤销动作本身就是一条普通的 Task → Execution（S-1），
Retry / Lease / Attempt / Idempotency 全部是现成的。

这里只定义三件事：

    CompensationSpec    逆操作的**声明**（随正向 Action 一起提出）
    CompensationRecord  一条已经发生的副作用，及其撤销状态
    CompensationStatus  撤销状态机

**S-8：补偿必须在正向动作提出时就声明，不允许事后发明。**

    事后发明的补偿有两个致命问题：
      · 失败时正向动作的 result 可能已经拿不到（撤销需要它：撤销"创建订单"要知道订单号）
      · Harness 没见过这个逆操作 —— 于是撤销要么绕过策略，要么撞上审批闸门卡死

**S-13：只有可能产生外部副作用的动作类型才允许声明补偿。**
给 `LLM_CALL` 声明补偿是句谎话（模型调用没有可撤销的外部状态），
它会让"这个 Run 有副作用"这件事被高估，进而让运维对一堆空撤销动作麻木。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Mapping

from ..errors import InvariantViolation
from ..ids import new_id

# ⚠️ 不要在本模块 import `..intelligence.action` —— 那边要 import 本模块的
# `CompensationSpec`，会形成循环导入。
#
# S-13 的"哪些动作类型允许声明补偿"记在 `intelligence/action.py`
# （`COMPENSABLE_ACTION_TYPES`），因为那是关于**动作类型**的知识。
# 本模块只负责"声明长什么样"和"撤销状态机"。


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CompensationSpec:
    """逆操作的声明。挂在正向 Action 上，跟着它一起过策略、一起被批准。

    `result_keys`：撤销参数里哪些值要**从正向执行的结果里取**，以及从哪儿取。

        创建订单 → 撤销需要 order_id，而这个 id 只有在正向跑完之后才存在。

    值支持 `.` 分隔的路径，因为执行结果**带信封**：

        {"tool": "create_order", "result": {"order_id": "o-1"}, ...}
                                  ↑ 工具真正的输出在这里

    所以这里写 `("result.order_id",)`，而不是 `("order_id",)`。
    用路径而不是"智能地猜哪层是输出"：猜错了就是带着 `None` 去撤销，
    而"撤销一个 id 为空的东西"在多数接口上不是报错，是撤销了别的东西。
    """

    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    result_keys: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.tool:
            raise InvariantViolation(
                "S-8: CompensationSpec.tool is required "
                "(a compensation that names no tool cannot be executed)"
            )
        if not self.description:
            raise InvariantViolation(
                "S-8: CompensationSpec.description is required "
                "(audit must be able to say what is being undone, in words)"
            )
        object.__setattr__(self, "args", dict(self.args))
        object.__setattr__(self, "result_keys", tuple(self.result_keys))

    def materialize(self, result: Mapping[str, Any] | None) -> dict[str, Any]:
        """把声明 + 正向结果变成可以真正执行的撤销参数。

        如果声明要的键在结果里没有 —— **直接失败，绝不带着缺失的参数去撤销**。
        带着空 id 去调撤销接口，最可能的后果是撤销了别的东西，或者静默成功。
        """
        args = dict(self.args)
        result = result or {}
        for key in self.result_keys:
            args[key.rsplit(".", 1)[-1]] = _dig(result, key)
        return args

    # ------------------------------------------------------------------
    # M26：这两个方法以前**缩进错了** —— 它们被写在模块级 `_dig` 的体内、
    # 且位于 `return` 之后，于是从来不可达：
    #
    #     >>> hasattr(CompensationSpec, "to_dict")
    #     False
    #
    # 代价不是"少两个工具方法"。S-8 的撤销声明**从来没有序列化通道**，
    # 于是任何"把 Action 落库再读回来"的路径都会静默丢掉 `compensation`；
    # `SagaCoordinator` 看到 `action.compensation is None` 就静默跳过，
    # 补偿账本因此**缺一条** —— 而缺的那一条恰恰是子 Run 留在外部世界的副作用。
    #
    # 这是又一处"冻结了 S-8，却没人断言它有没有被实现"。
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "result_keys": list(self.result_keys),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompensationSpec":
        return cls(
            tool=str(data.get("tool", "")),
            args=dict(data.get("args") or {}),
            result_keys=tuple(data.get("result_keys") or ()),
            description=str(data.get("description", "")),
        )


def _dig(data: Any, path: str) -> Any:
    """按 `.` 分隔的路径取值。取不到就抛 —— 绝不返回 None 让调用方"将就用"。"""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise InvariantViolation(
                f"S-8: compensation requires result key {path!r} but the "
                f"forward execution did not return it"
            )
        cur = cur[part]
    return cur


class CompensationStatus(str, Enum):
    """一条副作用记录的撤销状态。

    `UNRESOLVED` 是这里最重要的一个值：**撤销不了**和**不需要撤销**是两回事，
    而它们又都和"已经撤销"不同。把它并进 FAILED 会让运维以为"失败了就没人管了"，
    并进 SKIPPED 会让它从待办列表里消失 —— 两种都是把一件事说成另一件事。
    """

    PENDING = "pending"          # 已产生副作用，等待撤销
    RUNNING = "running"          # 正在撤销（已被认领）
    COMPENSATED = "compensated"  # 已撤销
    UNRESOLVED = "unresolved"    # 撤销不了：永久失败 / 缺参数 / 副作用存疑 —— 必须有人看见
    #: S-16：Run **成功**了 —— 副作用按预期保留，不需要撤销。
    #:
    #: 没有这个值的话，每一次成功的运行都会在账本里留下一堆 PENDING，
    #: 而 PENDING 的含义是"待撤销"。于是运维看板上永远挂着一串假待办，
    #: 真正需要撤销的那几条会淹没在里面 —— 这跟"静默失败"是同一种伤害。
    #: 也不能把它记成 COMPENSATED：那是在说"撤销过了"，是另一句谎话。
    NOT_NEEDED = "not_needed"


TERMINAL_COMPENSATION_STATUSES = frozenset(
    {
        CompensationStatus.COMPENSATED,
        CompensationStatus.UNRESOLVED,
        CompensationStatus.NOT_NEEDED,
    }
)

#: 允许的状态迁移（S-14：撤销状态不许倒流 —— 已撤销的东西不能重新变成待撤销）
_ALLOWED_TRANSITIONS: Mapping[CompensationStatus, frozenset[CompensationStatus]] = {
    CompensationStatus.PENDING: frozenset(
        {
            CompensationStatus.RUNNING,
            CompensationStatus.UNRESOLVED,
            CompensationStatus.NOT_NEEDED,
        }
    ),
    CompensationStatus.RUNNING: frozenset(
        {CompensationStatus.COMPENSATED, CompensationStatus.UNRESOLVED}
    ),
    CompensationStatus.COMPENSATED: frozenset(),
    CompensationStatus.NOT_NEEDED: frozenset(),
    CompensationStatus.UNRESOLVED: frozenset(
        {CompensationStatus.PENDING}  # 唯一例外：人工把它重新拉回待办（重试）
    ),
}


@dataclass
class CompensationRecord:
    """一条已经发生的副作用，以及它的撤销状态。

    **S-2：一个 Execution 最多一条补偿记录。** 这不是靠约定，是 DB 上的
    `UNIQUE(execution_id)` —— 否则两个 Coordinator 各自记一条，
    同一笔副作用会被撤销两次（撤销的撤销，多数情况下是另一笔真实副作用）。
    """

    compensation_id: str = field(default_factory=lambda: new_id("cmp"))
    run_id: str = ""
    step_id: str = ""                    # 原 Step（溯源：这个副作用是在哪一步产生的）
    task_id: str = ""
    execution_id: str = ""               # 被撤销的那条 Execution（UNIQUE）
    action_type: str = ""
    tool: str = ""                       # 撤销用的工具
    args: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    status: CompensationStatus = CompensationStatus.PENDING
    reason: str = ""                     # UNRESOLVED 时必须说清为什么
    attempts: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    version: int = 1

    # E-25 的同款记账：乐观锁比的是**上一次存储边界**上的版本，
    # 只能由 Repository（add / get / save）改写，内存自增不许碰。
    _store_version: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("S-2: CompensationRecord.run_id is required")
        if not self.execution_id:
            raise InvariantViolation("S-2: CompensationRecord.execution_id is required")
        if self.status is CompensationStatus.UNRESOLVED and not self.reason:
            raise InvariantViolation(
                "S-5: an UNRESOLVED compensation must carry a reason "
                "(otherwise nobody can tell why it could not be undone)"
            )
        object.__setattr__(self, "args", dict(self.args))
        object.__setattr__(self, "_store_version", self.version)

    @property
    def store_version(self) -> int:
        """上一次存储边界上的版本（E-25）—— Repository 的 `WHERE version = ?` 用它。"""
        return self._store_version

    # ------------------------------------------------------------ S-14
    def transition(
        self,
        to: CompensationStatus,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> None:
        """状态迁移。非法迁移直接抛 —— 撤销状态倒流等于把已撤销的副作用又记成待撤销。"""
        if to is self.status:
            return
        if to not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvariantViolation(
                f"S-14: compensation {self.compensation_id} cannot go "
                f"{self.status.value} → {to.value}"
            )
        if to is CompensationStatus.UNRESOLVED and not reason:
            raise InvariantViolation(
                "S-5: an UNRESOLVED compensation must carry a reason"
            )
        with self._writing():
            self.status = to
            # reason 只给 UNRESOLVED 留 —— 其它状态留着旧 reason 会让人以为有事
            self.reason = reason if to is CompensationStatus.UNRESOLVED else ""
            self.updated_at = now or _utcnow()
            self.version += 1

    def touch(self, *, now: datetime | None = None) -> None:
        """只更新 attempts / updated_at，不改状态（重试计数）。"""
        with self._writing():
            self.attempts += 1
            self.updated_at = now or _utcnow()
            self.version += 1

    def amend_reason(self, reason: str, *, now: datetime | None = None) -> None:
        """D-23：只换理由，**不动状态**。

        ------------------------------------------------------------------
        为什么要有这么一个"改口"的动作

        空洞 231：等到上限也没有结果时，账本上记的是"我们不知道它还在不在跑"。
        那是一句**实话** —— 但它只在"还没有真相"的时候成立。

        真相到了（那条子 Run 其实跑完了），账本上那句话就变成了**错的**。
        而"错一格"比"缺一格"坏得多：缺一格会让运维去看一眼，
        错一格会让运维**不去看**（PR-19）。

        ------------------------------------------------------------------
        为什么不动状态

        收回的是"不知道"这三个字，**不是**那笔副作用。
        它仍然 UNRESOLVED：副作用还在外部世界，还没有人撤销它（S-15）。
        把它一并结掉，就是把"我们终于知道它干了什么"
        说成"它干的事已经处理好了" —— 与 S-14 同族：账不许倒流。
        """
        if self.status is not CompensationStatus.UNRESOLVED:
            raise InvariantViolation(
                f"D-23: compensation {self.compensation_id} is "
                f"{self.status.value}; only an UNRESOLVED record may have its "
                f"reason amended — a record that has already been disposed of is "
                f"history, not a draft"
            )
        if not reason:
            raise InvariantViolation(
                "S-5: an UNRESOLVED compensation must carry a reason"
            )
        with self._writing():
            self.reason = reason
            self.updated_at = now or _utcnow()
            self.version += 1

    def become_compensable(
        self,
        args: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        """D-25：撤销参数补齐了 —— 这笔账从"**撤销不了**"变成"**待撤销**"。

        ------------------------------------------------------------------
        为什么这是一次状态迁移，而不只是"填个字段"

        空洞 233：迟到的结果到达时，D-23 只把那句"不知道"换成了真相，
        却**没有**换掉"撤销不了"这个结论。于是那一行仍然是 UNRESOLVED，
        而 UNRESOLVED 的字面意思是"没有人能撤销它" —— 一句已经失效的话。

        更糟的是 `args` 仍然空着。人工点"重试"把它拉回 PENDING 之后，

            build_action() → {"tool": "cancel_ticket", "args": {}}

        撤销工具被用**空参数**调用 —— 而 S-8 早就写明这个后果：
        "带着空 id 去调撤销接口，最可能的后果是撤销了别的东西，
        或者**静默成功**"。静默成功之后账本记成 `compensated`
        （"已经撤销完了"）—— 三句谎话叠在一起。

        ------------------------------------------------------------------
        为什么 `reason` 被清空

        与 `transition()` 同一个约定：`reason` 只给 UNRESOLVED 留 ——
        其它状态带着旧 reason 会让人以为"还有事"。
        这一笔的来龙去脉改由**事件**承载（`compensation.upgraded` 的
        payload 里带着完整理由）—— 那正是 M40 刚建好的那条通道。

        ------------------------------------------------------------------
        为什么要求原来的 `args` 是空的

        `args` 非空意味着"撤销参数**一直都在**"，那这笔账当初不是因为
        缺参数才记成 UNRESOLVED 的（它是撤销动作跑失败了，S-5/S-6）。
        那种情形要不要重试是**人**的决定（S-14 唯一例外），
        真相到达不构成自动重试的理由。
        """
        if self.status is not CompensationStatus.UNRESOLVED:
            raise InvariantViolation(
                f"D-25: compensation {self.compensation_id} is "
                f"{self.status.value}; only an UNRESOLVED record can become "
                f"compensable — anything else is either already compensable or "
                f"already disposed of"
            )
        if dict(self.args):
            raise InvariantViolation(
                f"D-25: compensation {self.compensation_id} already has undo args; "
                f"it was not UNRESOLVED for lack of undo args, so a late result "
                f"does not entitle it to an automatic retry (S-14)"
            )
        with self._writing():
            self.args = dict(args)
            self.status = CompensationStatus.PENDING
            self.reason = ""
            self.updated_at = now or _utcnow()
            self.version += 1

    @property
    def is_open(self) -> bool:
        return self.status in (CompensationStatus.PENDING, CompensationStatus.RUNNING)

    def summary(self) -> Mapping[str, Any]:
        return {
            "compensation_id": self.compensation_id,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "tool": self.tool,
            "status": self.status.value,
            "reason": self.reason,
            "attempts": self.attempts,
        }

    # ------------------------------------------------------------ 内部
    @contextmanager
    def _writing(self) -> Iterator[None]:
        """写入窗口（与 `Execution.mutating()` 同一个套路：状态只能从这儿改）。

        版本号是**自增**的，但"存储边界"是 Repository 的事（E-25）——
        这里只负责不让人绕开状态机直接赋值。
        """
        yield
