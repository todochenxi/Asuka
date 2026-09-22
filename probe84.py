"""probe84 —— 撞 I-6 那句"唯一入口"。

I-6 的承诺（observation.py:87）：
    "I-6：来自执行的 Observation 只能由这个工厂构造，必须绑定真实 Execution。"

`from_execution_result` 校验了 `execution_id` 非空、`attempt_no >= 1`。

**但 `Observation` 是 `@dataclass(frozen=True)`，它的 `__init__` 是公开的。**
于是"只能由工厂构造"这句话到底是：
  (a) 一条**被机制保证**的事实（绕过它会被拦），还是
  (b) 一句**注释里的愿望**（绕过它没人管）？

判据：直接 `Observation(source=EXECUTION_RESULT, execution_id=None)` ——
如果它构造成功，那 (a) 不成立，`__post_init__` 有没有拦这条？

顺带撞第二句：`ObservationSource.EXECUTION_RESULT` 是唯一"必须绑执行"的来源，
那另外三个来源（EXTERNAL_EVENT / HUMAN_INPUT / SYSTEM）呢？
它们**能不能**带上一个 execution_id？（如果能，账本上就分不清
"这条是执行产出的"还是"这条是人给的、只是顺手挂了个 id"）
"""
from __future__ import annotations

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.observation import (
    Observation,
    ObservationSource,
)

print("=" * 72)
print("case 1：绕过工厂，直接构造 EXECUTION_RESULT 且不带 execution_id")
print("=" * 72)
try:
    obs = Observation(
        run_id="run_1",
        source=ObservationSource.EXECUTION_RESULT,
        kind="tool_result",
        summary="I claim this came from an execution",
        # execution_id 刻意不传
    )
    print("★ 构造成功了 —— I-6 的『只能由工厂构造』是一句愿望，不是机制")
    print(f"   execution_id = {obs.execution_id!r}")
    print(f"   attempt_no   = {obs.attempt_no!r}")
    print(f"   source       = {obs.source}")
except InvariantViolation as exc:
    print(f"  被拦住了（好）：{exc}")

print()
print("=" * 72)
print("case 2：EXECUTION_RESULT + execution_id=''（空串）")
print("=" * 72)
try:
    obs = Observation(
        run_id="run_1",
        source=ObservationSource.EXECUTION_RESULT,
        kind="tool_result",
        summary="empty string execution id",
        execution_id="",
    )
    print(f"★ 构造成功，execution_id={obs.execution_id!r}（空串 ≠ None，但同样没有绑定）")
except InvariantViolation as exc:
    print(f"  被拦住了（好）：{exc}")

print()
print("=" * 72)
print("case 3：非执行来源带上 execution_id —— 分得清吗？")
print("=" * 72)
for src in (
    ObservationSource.EXTERNAL_EVENT,
    ObservationSource.HUMAN_INPUT,
    ObservationSource.SYSTEM,
):
    try:
        obs = Observation(
            run_id="run_1",
            source=src,
            kind="whatever",
            summary="human said something",
            execution_id="exec_FAKE",   # 凭空挂一个 id
            attempt_no=7,
        )
        print(f"  {src.value:<16} 能挂 execution_id={obs.execution_id!r} attempt={obs.attempt_no}")
    except InvariantViolation as exc:
        print(f"  {src.value:<16} 被拦：{exc}")

print()
print("=" * 72)
print("case 4：attempt_no 的边界 —— 工厂要求 >=1，直接构造呢？")
print("=" * 72)
for bad in (0, -1):
    try:
        obs = Observation(
            run_id="run_1",
            source=ObservationSource.EXECUTION_RESULT,
            kind="tool_result",
            summary="bad attempt",
            execution_id="exec_1",
            attempt_no=bad,
        )
        print(f"  attempt_no={bad:<3} ★ 构造成功（工厂会拦，__init__ 不拦）")
    except InvariantViolation as exc:
        print(f"  attempt_no={bad:<3} 被拦：{exc}")
