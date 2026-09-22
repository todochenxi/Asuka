"""变红验证 M90 / I-20。

把这道「这个动作运行时执行得了吗」的门逐个**改回旧写法**，看哪些用例会红。

纪律（都是踩过的坑）：
  * 脚本就在仓库根 -> `ROOT = Path(__file__).parent`，**不是** `.parent.parent`
  * 每条变异之后**立刻还原**，否则后一条的计数被前一条的残留污染
  * 断言"替换命中次数 == 1" —— 锚点不唯一时静默 SKIP，看起来像"没红"
  * 子进程输出按 utf-8 解，否则 GBK 解码崩在一堆 threading 栈里
  * 计数正则要允许两段各自单独出现（`FAILED (failures=3)` 没有 `errors=`）
  * 原文件先**落盘**备份 + 每次子进程加 `timeout=` + `finally` 比对残留
  * ⚠️ **锚点写在本文件里，不要走 shell heredoc** ——
    多行锚点里的 `\\n` 会被 heredoc 转成 `/n`，命中数变 0，看起来像"没红"。
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

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red90.orig"

LOOP = ROOT / "packages" / "agent_runtime" / "loop.py"
TF = ROOT / "packages" / "agent_runtime" / "task_factory.py"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

# 那道门 —— M1 / M2 / M3 共用
_GATE = (
    "        if action.action_type not in EXECUTABLE_ACTION_TYPES:\n"
    "            self._declare_terminal(\n"
    "                AgentRunStatus.FAILED,\n"
    "                reason=_unexecutable_action_reason(action),\n"
    "            )\n"
    "            return self._record(StepOutcome.FAILED)"
)

# 集合的推导 —— M4 / M5 用
_EXECUTABLE = (
    "EXECUTABLE_ACTION_TYPES: frozenset[ActionType] = (\n"
    "    TASK_PRODUCING_ACTION_TYPES | LOOP_HANDLED_ACTION_TYPES\n"
    ")"
)
_UNEXECUTABLE = (
    "UNEXECUTABLE_ACTION_TYPES: frozenset[ActionType] = (\n"
    "    frozenset(ActionType) - TASK_PRODUCING_ACTION_TYPES - LOOP_HANDLED_ACTION_TYPES\n"
    ")"
)

# `from_action()` 的两句报错 —— M9 用
_TWO_RAISES = (
    "            if action.action_type in LOOP_HANDLED_ACTION_TYPES:\n"
    "                raise InvariantViolation(\n"
    "                    f\"I-4: action type {action.action_type.value!r} produces no Task — \"\n"
    "                    f\"it is handled by AgentLoop._step() itself and must not reach \"\n"
    "                    f\"TaskFactory; calling from_action() with it is a caller bug\"\n"
    "                )\n"
    "            raise InvariantViolation(\n"
    "                f\"I-20: action type {action.action_type.value!r} produces no Task and \"\n"
    "                f\"nothing executes it — it is declared in ActionType but this runtime \"\n"
    "                f\"has no path for it (see UNEXECUTABLE_ACTION_TYPES); AgentLoop refuses \"\n"
    "                f\"it before any side effect instead of crashing here\"\n"
    "            )"
)
_OLD_RAISE = (
    "            raise InvariantViolation(\n"
    "                f\"I-4: action type {action.action_type.value} produces no Task\"\n"
    "            )"
)

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "M1",
        "那道门整个拿掉（回到「崩在一个没有账本记录的异常上」）",
        LOOP,
        _GATE,
        "        pass",
    ),
    (
        "M2",
        "拒绝但**不落账本**（判死却不声明终态）",
        LOOP,
        _GATE,
        "        if action.action_type not in EXECUTABLE_ACTION_TYPES:\n"
        "            return self._record(StepOutcome.FAILED)",
    ),
    (
        "M3",
        "拒绝之后**不返回**（判死了还继续往下执行）",
        LOOP,
        _GATE,
        "        if action.action_type not in EXECUTABLE_ACTION_TYPES:\n"
        "            self._declare_terminal(\n"
        "                AgentRunStatus.FAILED,\n"
        "                reason=_unexecutable_action_reason(action),\n"
        "            )",
    ),
    (
        "M4",
        "自述的能力集合扩成「全都支持」（能力与声明假装对齐）",
        TF,
        _EXECUTABLE,
        "EXECUTABLE_ACTION_TYPES: frozenset[ActionType] = frozenset(ActionType)",
    ),
    (
        "M5",
        "「执行不了」的集合手写成空集（回到「声明了却没人管」）",
        TF,
        _UNEXECUTABLE,
        "UNEXECUTABLE_ACTION_TYPES: frozenset[ActionType] = frozenset()",
    ),
    (
        "M6",
        "把 `wait` 也算进「由 Loop 自己处理」（把没归宿伪装成有归宿）",
        TF,
        "    {ActionType.FINISH, ActionType.REPLAN}",
        "    {ActionType.FINISH, ActionType.REPLAN, ActionType.WAIT}",
    ),
    (
        "M7",
        "理由不点名**运行时支持什么**（运维不知道该改用什么）",
        LOOP,
        "    supported = sorted(k.value for k in EXECUTABLE_ACTION_TYPES)",
        '    supported = ["<omitted>"]',
    ),
    (
        "M8",
        "理由不说**正确的替代路径**（读的人只知道失败了）",
        LOOP,
        '        f"or silently skipping it (I-20); express waiting as an approval gate or a "\n'
        '        f"child run until a TIMER producer exists."',
        '        f"or silently skipping it (I-20)."',
    ),
    (
        "M9",
        "两种「没有 Task」混成同一句话（分不出该改调用方还是该补实现）",
        TF,
        _TWO_RAISES,
        _OLD_RAISE,
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
        return -2, -2, ["<超时 —— 这条变异让用例转不完>"]
    out = proc.stdout + proc.stderr
    m = COUNT.search(out)
    if m:
        return int(m.group(2) or 0), int(m.group(4) or 0), sorted(set(TEST_ID.findall(out)))
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

    targets = {LOOP, TF}
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
