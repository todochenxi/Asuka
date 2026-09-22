"""变红验证 M87 / I-16。

把每个守点逐个**改回旧写法**，看哪些用例会红。

纪律（都是踩过的坑）：
  * 脚本就在仓库根 -> `ROOT = Path(__file__).parent`，**不是** `.parent.parent`
    （写错的话所有子进程跑在仓库外，会报"基线红了 N 条"）
  * 每条变异之后**立刻还原**，否则后一条的计数被前一条的残留污染
  * 断言"替换命中次数 == 1" —— 锚点不唯一时静默 SKIP，看起来像"没红"
  * 子进程输出按 utf-8 解，否则 GBK 解码崩在一堆 threading 栈里
  * 计数正则要允许两段各自单独出现（`FAILED (failures=3)` 没有 `errors=`）
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

# ⚠️ 变异会**真的改磁盘上的源文件**。所以：
#   * 原文件先落一份到磁盘（进程被杀也能恢复）
#   * 主流程包在 try/finally 里
#
# 为什么非要这么做：第一版没有这些，脚本跑到一半被我 kill 掉 ——
# `finally` 没执行，**变异留在源文件里**。之后我从"备份"恢复，
# 而那份备份是在污染**之后**拷的，于是把变异又装了回去。
# 症状看起来像"我的改动弄红了 3 条既有测试"，查了很久才发现
# 根本不是代码问题，是工具自己把世界改坏了。
BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red87.orig"

LOOP = ROOT / "packages" / "agent_runtime" / "loop.py"
PLAN = ROOT / "packages" / "agent_domain" / "intelligence" / "plan.py"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "M1",
        "依赖被忽略 —— 回到「下标轮到谁就是谁」",
        LOOP,
        "            if all(dep in satisfied for dep in node.depends_on):\n"
        "                return node",
        "            if True:\n"
        "                return node",
    ),
    (
        "M2",
        "失败也算满足了依赖（只看终态，不看是不是 COMPLETED）",
        LOOP,
        "            if s.status is StepStatus.COMPLETED",
        "            if s.status in (StepStatus.COMPLETED, StepStatus.FAILED,"
        " StepStatus.CANCELLED)",
    ),
    (
        "M3",
        "计划卡住时静默退化成 ad-hoc 步（_plan_is_stuck 永远为假）",
        LOOP,
        "        return self._plan_has_unconsumed_nodes(plan)"
        " and self._next_plan_node(plan) is None",
        "        return False",
    ),
    (
        "M4",
        "计划用完也被判成「卡住」（把两条路混起来）",
        LOOP,
        "        return self._plan_has_unconsumed_nodes(plan)"
        " and self._next_plan_node(plan) is None",
        "        return self._plan_has_unconsumed_nodes(plan)"
        " or self._next_plan_node(plan) is None",
    ),
    (
        "M5",
        "已消费的节点集合永远为空（游标不走）",
        LOOP,
        "        return {s.plan_node_id for s in self.steps_of_run}",
        "        return set()",
    ),
    (
        "M6",
        "计划完全不参与 —— 一律走 ad-hoc 步",
        LOOP,
        "        if node is not None:",
        "        if False:",
    ),
    (
        "M7",
        "Plan 的环检测被拿掉（领域层守卫）",
        PLAN,
        "        self.assert_acyclic()",
        "        pass",
    ),
    (
        "M8",
        "run() 退回「只看 StepOutcome 白名单」（丢掉 Run 终态那一半）",
        LOOP,
        "            # ② 这条 Run 已经结束了 —— 无论这一步的结果叫什么名字。\n"
        "            # （`is_terminal` 是 property，不是方法。）\n"
        "            if self.agent_run is not None and self.agent_run.is_terminal:\n"
        "                return self.state\n",
        "",
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
    # 把"没有 FAILED 行"当成"看不出来"会让基线永远报"不绿"。
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
