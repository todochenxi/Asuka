"""Plan / PlanNode。

Plan 是**静态**产物（Planner 产出），Step 是它在运行时的实例。
两者不是一回事：

    Plan Node（静态）  ──实例化──►  Step（运行时）  ──1:N──►  Task
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..errors import InvariantViolation
from ..ids import new_id


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    name: str
    kind: str = "task"                      # task / tool / agent / human / decision
    depends_on: tuple[str, ...] = ()
    expected_output: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id or not self.name:
            raise InvariantViolation("PlanNode.node_id / name are required")
        if self.node_id in self.depends_on:
            raise InvariantViolation(f"PlanNode {self.node_id} depends on itself")


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
