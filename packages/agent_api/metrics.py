"""业务指标 → Prometheus 文本（M7 / M9，基线 §M7）。

### 它补的是什么

总览 §M7 写着"HPA 不能只看 CPU → 业务指标（Queue Depth / Run Latency）"。
但在此前，AgentOS **一个指标都吐不出来** —— 于是"按队列深度扩 worker"
这句话没有落点：集群侧没有一个数可以读。

这一层只做**格式**：把几个业务计数渲染成 Prometheus 文本（`# HELP` / `# TYPE` /
`名字 值`）。**取数**在组合根（它才有连接），**暴露**在 `apps/api/app.py` 的
`GET /metrics`。三件事分开，是因为取数要连库、渲染是纯函数、暴露要框架 ——
混在一起会让"指标对不对"没法脱离进程去测。

### 一条不变量

| # | 内容 |
|---|---|
| M-1 | 指标值必须是**有限**的。`NaN` / `±Inf` 不是"一个很大的数"，是"没有数"—— 让它们进指标等于让扩缩容基于一个非数 |
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

#: Prometheus 的指标名文法。拼错的名字会被采集端**静默丢弃**，
#: 于是"指标没上报"排起来像指标不存在。
_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_TYPES = frozenset({"gauge", "counter"})

# ── 指标名集中在这一处（B-7：一个事实一处定义）────────────────
PENDING_EXECUTIONS = "agentos_pending_executions"
RUNNING_EXECUTIONS = "agentos_running_executions"
SUSPENDED_EXECUTIONS = "agentos_suspended_executions"
PENDING_APPROVALS = "agentos_pending_approvals"


@dataclass(frozen=True)
class Metric:
    """一条指标。`type` 只收 Prometheus 认识的两种。"""

    name: str
    help: str
    value: float
    type: str = "gauge"

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"invalid Prometheus metric name: {self.name!r}")
        if self.type not in _TYPES:
            raise ValueError(f"metric type must be one of {sorted(_TYPES)}, got {self.type!r}")
        # M-1：有限值
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError(f"metric value must be a number, got {self.value!r}")
        if not math.isfinite(float(self.value)):
            raise ValueError(f"M-1: metric {self.name!r} value must be finite, got {self.value!r}")

    def render(self) -> str:
        # HELP 里换行会破坏文本格式；Prometheus 的转义是 `\n`，这里直接摊平成空格。
        help_text = self.help.replace("\\", "\\\\").replace("\n", " ")
        value = _format_value(self.value)
        return (
            f"# HELP {self.name} {help_text}\n"
            f"# TYPE {self.name} {self.type}\n"
            f"{self.name} {value}\n"
        )


def _format_value(value: float) -> str:
    """整数就按整数写（`5` 而不是 `5.0`）—— 队列深度不该读成小数。"""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def render_prometheus(metrics: Sequence[Metric]) -> str:
    """把一批指标渲染成 Prometheus 文本格式。"""
    return "".join(m.render() for m in metrics)


def business_metrics(
    *,
    pending_executions: int = 0,
    running_executions: int = 0,
    suspended_executions: int = 0,
    pending_approvals: int = 0,
) -> tuple[Metric, ...]:
    """AgentOS 的**业务指标**。

    `pending_executions`（队列深度）是 KEDA 用来扩 worker 的那一个：
    它比 CPU 更接近"还有多少活等着干"—— CPU 高可能只是在算一件长任务，
    而队列深度高才意味着"需要更多 worker"。
    """
    return (
        Metric(
            PENDING_EXECUTIONS,
            "Runnable executions (queue depth); KEDA scales workers on this",
            pending_executions,
        ),
        Metric(RUNNING_EXECUTIONS, "Executions currently RUNNING", running_executions),
        Metric(
            SUSPENDED_EXECUTIONS,
            "Executions SUSPENDED (waiting for a human or a child run)",
            suspended_executions,
        ),
        Metric(PENDING_APPROVALS, "Approvals waiting for a human decision", pending_approvals),
    )


__all__ = [
    "PENDING_APPROVALS",
    "PENDING_EXECUTIONS",
    "RUNNING_EXECUTIONS",
    "SUSPENDED_EXECUTIONS",
    "Metric",
    "business_metrics",
    "render_prometheus",
]
