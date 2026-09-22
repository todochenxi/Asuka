"""变红验证 M88 / I-18。

把每个守点逐个**改回旧写法**，看哪些用例会红。

纪律（都是踩过的坑）：
  * 脚本就在仓库根 -> `ROOT = Path(__file__).parent`，**不是** `.parent.parent`
    （写错的话所有子进程跑在仓库外，会报"基线红了 N 条"）
  * 每条变异之后**立刻还原**，否则后一条的计数被前一条的残留污染
  * 断言"替换命中次数 == 1" —— 锚点不唯一时静默 SKIP，看起来像"没红"
  * 子进程输出按 utf-8 解，否则 GBK 解码崩在一堆 threading 栈里
  * 计数正则要允许两段各自单独出现（`FAILED (failures=3)` 没有 `errors=`）
  * 原文件先**落盘**备份 + 每次子进程加 `timeout=` + `finally` 比对残留
    （M87 栽得最惨的一次：脚本被 kill，`finally` 没跑，变异留在源文件里）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # 脚本就在仓库根
PY = sys.executable
RUN_TIMEOUT = 120                                # 单次跑套件的上限（秒）

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red88.orig"

LOOP = ROOT / "packages" / "agent_runtime" / "loop.py"
PLAN = ROOT / "packages" / "agent_domain" / "intelligence" / "plan.py"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

# 领域层那段校验的原文 —— M1 / M2 共用
_KIND_GUARD = (
    "        if not isinstance(self.kind, PlanNodeKind):\n"
    "            try:\n"
    '                object.__setattr__(self, "kind", PlanNodeKind(self.kind))\n'
    "            except ValueError as exc:\n"
    "                known = [k.value for k in PlanNodeKind]\n"
    "                raise InvariantViolation(\n"
    '                    f"PlanNode {self.node_id} has unknown kind {self.kind!r}; "\n'
    '                    f"expected one of {known}"\n'
    "                ) from exc"
)

# 运行时那道判据的原文 —— M3 用
_STEP_GUARD = (
    "        if state.current_plan is not None:\n"
    "            offenders = _unsupported_plan_nodes(state.current_plan)\n"
    "            if offenders:\n"
    "                self._declare_terminal(\n"
    "                    AgentRunStatus.FAILED,\n"
    "                    reason=_unsupported_kinds_reason(offenders),\n"
    "                )\n"
    "                return self._record(StepOutcome.FAILED)"
)

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "M1",
        "领域层不校验 kind（回到「类型是 str，五个值只在注释里」）",
        PLAN,
        _KIND_GUARD,
        "        pass",
    ),
    (
        "M2",
        "未知 kind **兜底成 task**（M88 要消灭的那条路本身）",
        PLAN,
        _KIND_GUARD,
        "        if not isinstance(self.kind, PlanNodeKind):\n"
        "            try:\n"
        '                object.__setattr__(self, "kind", PlanNodeKind(self.kind))\n'
        "            except ValueError:\n"
        '                object.__setattr__(self, "kind", PlanNodeKind.TASK)',
    ),
    (
        "M3",
        "运行时那道判据整个拿掉（回到「当普通 task 跑」）",
        LOOP,
        _STEP_GUARD,
        "        pass",
    ),
    (
        "M4",
        "只查「第一个节点」，不查整份计划（先跑几个再失败）",
        LOOP,
        "    return [n for n in plan.nodes if n.kind not in SUPPORTED_PLAN_NODE_KINDS]",
        "    return [n for n in plan.nodes[:1] if n.kind not in SUPPORTED_PLAN_NODE_KINDS]",
    ),
    (
        "M5",
        "判据只在「刚规划出计划」时生效 —— 快照恢复那条路绕过它",
        LOOP,
        "        if state.current_plan is not None:\n"
        "            offenders = _unsupported_plan_nodes(state.current_plan)",
        "        if (\n"
        "            state.current_plan is not None\n"
        "            and state.observations\n"
        "            and state.observations[-1].kind == PLAN_CREATED\n"
        "        ):\n"
        "            offenders = _unsupported_plan_nodes(state.current_plan)",
    ),
    (
        "M6",
        "理由不说「不能凑合」（读的人会以为当 task 跑是个降级选项）",
        LOOP,
        '        f"supported kinds are {supported} — this runtime has no per-kind "\n'
        '        f"dispatch, so running such a node as an ordinary task would fabricate "\n'
        '        f"the behaviour its kind declares (I-18)"',
        '        f"supported kinds are {supported} (I-18)"',
    ),
    (
        "M7",
        "理由不点名**哪个节点**（运维只知道失败了，不知道该扩什么能力）",
        LOOP,
        '    declared = ", ".join(f"{n.node_id}={n.kind.value!r}" for n in offenders)',
        '    declared = f"{len(offenders)} node(s)"',
    ),
    (
        "M8",
        "把声明的能力集合扩成「全都支持」（能力与声明假装对齐）",
        LOOP,
        "SUPPORTED_PLAN_NODE_KINDS: frozenset[PlanNodeKind] = "
        "frozenset({PlanNodeKind.TASK})",
        "SUPPORTED_PLAN_NODE_KINDS: frozenset[PlanNodeKind] = frozenset(PlanNodeKind)",
    ),
]


def run_suite() -> tuple[int, int, list[str]]:
    try:
        proc = subprocess.run(
            [PY, "-u", "-m", "unittest", "discover", "-s", "tests/unit", "-t", "."],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONPATH": "."},
            timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # 某条变异让某个用例转不完 —— 那本身是个信号，但不能拖垮整轮。
        return -2, -2, ["<超时 —— 这条变异让用例转不完>"]
    out = proc.stdout + proc.stderr
    m = COUNT.search(out)
    if m:
        return int(m.group(2) or 0), int(m.group(4) or 0), sorted(set(TEST_ID.findall(out)))
    # 全绿的时候**没有** FAILED 行 —— 只有一行 `OK`。
    if re.search(r"^OK$", out, re.MULTILINE):
        return 0, 0, []
    return -1, -1, ["<看不出来 —— 没有 OK 也没有 FAILED>"]


def main() -> int:
    print("=" * 74, flush=True)
    print("基线（未变异）", flush=True)
    print("=" * 74, flush=True)
    f, e, ids = run_suite()
    print(f"   failures={f}  errors={e}", flush=True)
    if f != 0 or e != 0:
        print("  ⚠️ 基线就不绿 —— 先查脚本，不要先查代码。", flush=True)
        for i in ids[:10]:
            print("   ", i, flush=True)
        return 1
    print("   基线全绿 ✓", flush=True)
    print(flush=True)

    targets = {LOOP, PLAN}
    BACKUP.mkdir(parents=True, exist_ok=True)
    originals: dict[Path, str] = {}
    for p in targets:
        text = p.read_text(encoding="utf-8")
        originals[p] = text
        (BACKUP / p.name).write_text(text, encoding="utf-8")   # 落盘备份

    results: list[tuple[str, str, int, int, list[str]]] = []

    try:
        for name, desc, path, old, new in MUTATIONS:
            text = path.read_text(encoding="utf-8")
            hits = text.count(old)
            if hits != 1:
                print(f"{name}: ⚠️ SKIP —— 锚点命中 {hits} 次（应为 1）：{desc}", flush=True)
                results.append((name, desc, -1, -1, []))
                continue

            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            try:
                f, e, ids = run_suite()
            finally:
                path.write_text(originals[path], encoding="utf-8")   # 立刻还原

            if f == -2:
                print(f"{name}: ⏱ 超时（{RUN_TIMEOUT}s）—— {desc}", flush=True)
                results.append((name, desc, -2, -2, ids))
                continue

            total = f + e
            flag = "★ 红" if total > 0 else "⚠️ 没红"
            print(f"{name}: {flag}  failures={f} errors={e}   ({desc})", flush=True)
            for i in ids[:4]:
                print("        ", i, flush=True)
            if len(ids) > 4:
                print(f"         … 另 {len(ids) - 4} 条", flush=True)
            results.append((name, desc, f, e, ids))
    finally:
        # 无论怎么退出，都把源文件恢复到备份那份。
        for p, text in originals.items():
            if p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")
                print(f"⚠️ {p.name} 有残留变异，已从备份恢复", flush=True)

    print(flush=True)
    print("=" * 74, flush=True)
    print("汇总", flush=True)
    print("=" * 74, flush=True)
    red = [r for r in results if r[2] + r[3] > 0]
    silent = [r for r in results if r[2] + r[3] == 0]
    skipped = [r for r in results if r[2] < 0]
    print(
        f"  变异 {len(MUTATIONS)} 条：红了 {len(red)} / 没红 {len(silent)} / 超时或跳过 {len(skipped)}",
        flush=True,
    )
    for name, desc, f, e, _ in silent:
        print(f"  ⚠️ {name} 没红 —— {desc}（先怀疑测试，再怀疑变异本身）", flush=True)
    for name, desc, f, _, _ in skipped:
        tag = "超时" if f == -2 else "跳过"
        print(f"  ⚠️ {name} {tag} —— {desc}", flush=True)
    print(flush=True)
    print("  ⚠️ 「没红」不等于「这条守点不重要」：先问变异是不是真的改了行为", flush=True)
    print("     （给字段加注释不算变异 —— 字段还在，什么都没变）。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
