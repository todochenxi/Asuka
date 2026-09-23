"""Plan / PlanNode。

Plan 是**静态**产物（Planner 产出），Step 是它在运行时的实例。
两者不是一回事：

    Plan Node（静态）  ──实例化──►  Step（运行时）  ──1:N──►  Task
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ..errors import InvariantViolation
from ..ids import new_id


class PlanNodeKind(str, Enum):
    """一个计划节点声明的**工作类型**（I-18）。

    ⚠️ 这五个值此前只活在 `kind: str = "task"` 的**行尾注释**里：
    类型是 `str`，`PlanNode.__post_init__` 完全不看它。于是（探针实测）：

        kind='banana'  ->  构造通过
        kind=''        ->  构造通过
        kind='TASK'    ->  构造通过

    同项目里 `ObservationSource` / `ChildRunKind` / `RiskLevel` / `ActionType`
    全是 `str, Enum` + `__post_init__` 校验 —— 这是**唯一的例外**。

    **一句「只有这五种」如果写在注释里而不是写在类型里，那它就是一种愿望，
    不是一种约束。**（M88）

    这五个值不是编出来的：它们各自对应系统里**真实存在**的一条路 ——
    `task` 由 DecisionEngine 正常决策，`tool` 对应 `ActionType.TOOL_CALL`，
    `agent` 对应 `ActionType.AGENT_DELEGATION`（派生子 Run），
    `human` 对应 Harness 的 `REQUIRE_APPROVAL` / `ActionType.HUMAN_APPROVAL`，
    `decision` 对应一次纯决策步。

    但**声明了**不等于**做得到**：运行时至今没有按 kind 分派的机制
    （见 `loop.SUPPORTED_PLAN_NODE_KINDS`）。声明与能力之间的差，
    必须由运行时**说出来**，不能靠静默当 task 跑掉。
    """

    TASK = "task"
    TOOL = "tool"
    AGENT = "agent"
    HUMAN = "human"
    DECISION = "decision"


@dataclass(frozen=True)
class PlanNode:
    """计划里的一个**静态**节点（Step 是它的运行时实例）。

    ⚠️ `expected_output` 是一段**给人看的注释**：它能被序列化（`_plain`
    是通用序列化器）、能被还原（`snapshot.py` 显式读它），
    但**没有任何一处读它做判断**（M88 实证，登记为空洞 244）。

    它和 `depends_on` 曾经同族（被定义、被持久化、不被消费），
    但**处置不同**：`depends_on` 是**可执行**的约束（I-16 之后运行时真的读它），
    而 `expected_output` 是自由文本（"一个整数"），拿它做判据需要另一个
    裁判模型 —— 那是 Evaluation 的活，不是 Runtime 的。
    所以它是**登记不治**，不是"待办"：它的定位就是注释，
    把定位写清楚，比假装它有约束力要诚实。
    """

    node_id: str
    name: str
    kind: PlanNodeKind = PlanNodeKind.TASK
    depends_on: tuple[str, ...] = ()
    #: 这个节点**打算调用哪个工具**（M98 / 空洞 250）。
    #:
    #: §32 的 `Plan Validator` 列了 "Tool Exists" 一项，而此前 `PlanNode`
    #: **连表达它的字段都没有**（"这项连表达都表达不了"）。有了它，计划期
    #: 才可能回答"这份计划要调的工具，这个运行时有没有"。
    #:
    #: ⚠️ 留空 = 不声明（合法）：`kind='task'` 的通用节点本来就不事先知道
    #: 自己要调什么（那由 DecisionEngine 决定）。只有 `kind='tool'` **必须**
    #: 声明 `tool` —— 它的语义就是"这一步调这个工具"。
    tool: str = ""
    #: 这个节点要求的**资源标签**（M99 / 空洞 250 的 "Resource"）。
    #:
    #: §32 的 `Plan Validator` 列了 "Resource" 一项，此前全仓**没有任何
    #: Resource 概念**（登记为"连表达都表达不了"）。但 Kernel 其实**早就**
    #: 有执行期的资源匹配：`Task.resource_requirement.labels` ←→
    #: `WorkerCapability.labels`（`scheduler._matches`）。缺的只是**计划期的
    #: 声明**：让一份计划能事先说"这一步要 GPU / 要某个标签的 worker"。
    #:
    #: 留空 = 不声明要求（合法，走 Kernel 默认的 `ResourceReq()`）。
    #: 它**不**在这里做匹配校验 —— 那是计划门（`plan_defects`）拿
    #: "这个集群有哪些 worker 标签"去比的事，领域层不持有集群的拓扑。
    resource_labels: tuple[str, ...] = ()
    expected_output: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id or not self.name:
            raise InvariantViolation("PlanNode.node_id / name are required")
        if self.node_id in self.depends_on:
            raise InvariantViolation(f"PlanNode {self.node_id} depends on itself")

        # I-18：`kind` 是一个**闭集**。
        #
        # 字符串能匹配成员就收下（历史调用点与快照里的值都是字符串，
        # 而 `PlanNodeKind` 是 `str, Enum`，收下之后 `node.kind == "task"`
        # 依然成立）—— 匹配不上就**拒绝**。
        #
        # 刻意**不**做"未知值兜底成 task"：那正是 M88 要消灭的行为。
        # 一个 Planner 把 `kind` 拼错，运行时却当 task 跑了 ——
        # 这不是宽容，是**系统替一份它没读懂的计划做了主**。
        if not isinstance(self.kind, PlanNodeKind):
            try:
                object.__setattr__(self, "kind", PlanNodeKind(self.kind))
            except ValueError as exc:
                known = [k.value for k in PlanNodeKind]
                raise InvariantViolation(
                    f"PlanNode {self.node_id} has unknown kind {self.kind!r}; "
                    f"expected one of {known}"
                ) from exc

        # M98：`kind='tool'` 是一个**声明**——它说"这一步调 `tool`"。
        # 没有 `tool` 的 tool 节点是一句没有宾语的声明：运行时无从校验，
        # 只能等到执行期撞上 `BAD_PAYLOAD: payload.tool is required`。
        # 在构造处拒绝，是"声明必须完整"的最早落点。
        if self.kind is PlanNodeKind.TOOL and not self.tool:
            raise InvariantViolation(
                f"PlanNode {self.node_id} declares kind 'tool' but names no tool; "
                f"a tool node must say which tool (otherwise the declaration has "
                f"no referent and cannot be validated at plan time)"
            )


@dataclass(frozen=True)
class Plan:
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    run_id: str = ""
    nodes: tuple[PlanNode, ...] = ()
    constraints: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise InvariantViolation("Plan.run_id is required")
        ids = [n.node_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise InvariantViolation("Plan has duplicate node_id")
        known = set(ids)
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in known:
                    raise InvariantViolation(f"PlanNode {n.node_id} depends on unknown node {dep}")
        self.assert_acyclic()

    # ------------------------------------------------------------------ 校验
    def assert_acyclic(self) -> None:
        """拒绝带环的依赖图。

        ⚠️ 这句话此前写着"Planner 的 Plan Validator 会检查 DAG 环；这里做
        领域级兜底" —— 而**全仓没有 Plan Validator**（M87 实证）。于是那句
        "兜底"其实把唯一的检查说成了备份，读的人会以为另有一道。

        它也不只是措辞问题：那段时间里 `depends_on` **根本没被执行过** ——
        运行时按下标取节点（`plan.nodes[已跑步数]`），所以一张合法的依赖图
        和一个随手排的列表在运行时的差别是零。校验一张没人读的图，
        和没有校验过它，对系统行为来说是一样的。

        M87 / I-16 之后依赖图才真的可执行：`_ensure_step()` 只挑
        "依赖都已完成且没做过"的节点。这里仍然是**领域级**的守卫
        （所有构造路径的汇合处，见 M85），但现在它守的东西有人读了。
        """
        indegree = {n.node_id: 0 for n in self.nodes}
        adj: dict[str, list[str]] = {n.node_id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                adj[dep].append(n.node_id)
                indegree[n.node_id] += 1

        queue = [nid for nid, d in indegree.items() if d == 0]
        visited = 0
        while queue:
            nid = queue.pop()
            visited += 1
            for nxt in adj[nid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(self.nodes):
            raise InvariantViolation("Plan contains a cycle")

    def node(self, node_id: str) -> PlanNode:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)

    def root_nodes(self) -> tuple[PlanNode, ...]:
        return tuple(n for n in self.nodes if not n.depends_on)
