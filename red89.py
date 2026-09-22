"""变红验证 M89 / I-19。

把这道「计划必须属于这条 Run」的门逐个**改回旧写法**，看哪些用例会红。

纪律（都是踩过的坑）：
  * 脚本就在仓库根 -> `ROOT = Path(__file__).parent`，**不是** `.parent.parent`
    （写错的话所有子进程跑在仓库外，会报"基线红了 N 条"）
  * 每条变异之后**立刻还原**，否则后一条的计数被前一条的残留污染
  * 断言"替换命中次数 == 1" —— 锚点不唯一时静默 SKIP，看起来像"没红"
  * 子进程输出按 utf-8 解，否则 GBK 解码崩在一堆 threading 栈里
  * 计数正则要允许两段各自单独出现（`FAILED (failures=3)` 没有 `errors=`）
  * 原文件先**落盘**备份 + 每次子进程加 `timeout=` + `finally` 比对残留
    （M87 栽得最惨的一次：脚本被 kill，`finally` 没跑，变异留在源文件里）
  * ⚠️ M89 重构了 M88 的判据位置，`red88.py` 的两处锚点随之更新 ——
    **重构会让老变红脚本静默 SKIP**，那是"看起来没红"的另一种长相。
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

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red89.orig"

LOOP = ROOT / "packages" / "agent_runtime" / "loop.py"
SNAPSHOT = ROOT / "packages" / "agent_domain" / "business" / "snapshot.py"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

# 判据所在的那两行 —— M5 用
_GATE_HEAD = (
    "        if state.current_plan is not None:\n"
    "            defects = plan_defects(state.current_plan, run_id=state.run_id)"
)

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "M1",
        "归属判据整个拿掉（回到「别人的计划照单全收」）",
        LOOP,
        "    if plan.run_id != run_id:",
        "    if False:  # M1",
    ),
    (
        "M2",
        "拒绝理由不点名两个 run_id（运维不知道该去比对谁）",
        LOOP,
        '                f"plan says it belongs to run {plan.run_id!r}, "\n'
        '                f"but this run is {run_id!r}",',
        '                "plan belongs to another run",',
    ),
    (
        "M3",
        "只报第一条缺陷就返回（两条同时成立时第二条被吞掉）",
        LOOP,
        "    # ② I-18：计划里有没有运行时执行不了的 kind？\n"
        "    offenders = _unsupported_plan_nodes(plan)",
        "    return defects  # M3\n"
        "    # ② I-18：计划里有没有运行时执行不了的 kind？\n"
        "    offenders = _unsupported_plan_nodes(plan)",
    ),
    (
        "M4",
        "理由里丢掉机器可读的码（调用方没法按 code 分支）",
        LOOP,
        '    joined = "; ".join(f"{d.code} ({d.detail})" for d in defects)',
        '    joined = "; ".join(d.detail for d in defects)',
    ),
    (
        "M5",
        "判据只在「刚规划出计划」时生效 —— 快照恢复那条路绕过它",
        LOOP,
        _GATE_HEAD,
        "        if (\n"
        "            state.current_plan is not None\n"
        "            and state.observations\n"
        "            and state.observations[-1].kind == PLAN_CREATED\n"
        "        ):\n"
        "            defects = plan_defects(state.current_plan, run_id=state.run_id)",
    ),
    (
        "M6",
        "拒绝之后**不返回**（判死了还继续往下执行）",
        LOOP,
        "                    reason=_plan_defects_reason(defects),\n"
        "                )\n"
        "                return self._record(StepOutcome.FAILED)",
        "                    reason=_plan_defects_reason(defects),\n"
        "                )",
    ),
    (
        "M7",
        "理由不带判据编号（读的人不知道该去查哪条不变量）",
        LOOP,
        '        f"executed nor skipped; fix the plan (I-18/I-19)"',
        '        f"executed nor skipped; fix the plan"',
    ),
    (
        "M8",
        "快照恢复**不回填**计划的 run_id（老快照会被当成外来计划误伤）",
        SNAPSHOT,
        '            run_id=plan_raw.get("run_id") or data.get("run_id", ""),',
        '            run_id=plan_raw.get("run_id") or "",',
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

    targets = {LOOP, SNAPSHOT}
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
