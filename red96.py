"""M92 变红验证：AgentOS -> Asuka 评测交接契约。"""
from __future__ import annotations

from pathlib import Path

from redkit import Mutation, run_red

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / "asuka" / "agentos_adapter.py"
TEST = ROOT / "tests" / "unit" / "test_asuka_agentos_adapter.py"
BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red96.orig"

MUTATIONS = [
    Mutation(
        "M1",
        "把 retrieved 与 available 混成同一个集合，预算丢弃会被读成已提供",
        ADAPTER,
        "        available=sample.available,\n        retrieved=sample.retrieved,\n        evidence=resolved,",
        "        available=sample.retrieved,\n        retrieved=sample.retrieved,\n        evidence=resolved,",
    ),
    Mutation(
        "M2",
        "删除 retrieved 缺失门，None 会静默变成可迭代数据",
        ADAPTER,
        "        if self.retrieved is None:\n            raise AgentOSAdapterError(\n                f\"sample {self.execution_id!r} 没有 retrieved 集合；\"\n                \"ContextSnapshot 只能证明 available，不能反推检索器留下了什么。\"\n            )\n",
        "        if False:\n            raise AgentOSAdapterError(\"unreachable\")\n",
    ),
    Mutation(
        "M3",
        "删除 task_id 不在 Dataset 时的拒绝，不能靠题目文本猜身份",
        ADAPTER,
        "        if item is None:\n            raise AgentOSAdapterError(\n                f\"AgentOS sample {sample.execution_id!r} 的 task_id {sample.task_id!r} \"\n                \"不在 Asuka Dataset；不允许用 question 文本猜题目身份\"\n            )\n",
        "        if False:\n            raise AgentOSAdapterError(\"unreachable\")\n",
    ),
    Mutation(
        "M4",
        "删除 question 同一性门，题目漂移会被错误评分",
        ADAPTER,
        "        if sample.question != item.question:\n            raise AgentOSAdapterError(\n                f\"task {sample.task_id!r} 的 question 与 Dataset 不一致；\"\n                \"这会让答案分数挂到错误的题上\"\n            )\n",
        "        if False:\n            raise AgentOSAdapterError(\"unreachable\")\n",
    ),
]


if __name__ == "__main__":
    raise SystemExit(run_red("AgentOS 到 Asuka 评测适配", MUTATIONS, backup_dir=BACKUP))
