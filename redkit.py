"""red 脚本的公共骨架：锚点替换 + 基线校验 + **残留防护**。

--------------------------------------------------------------------------
为什么抽出来（而不是每轮再抄一遍）

`red87`~`red91` 各自带一份同样的骨架：跑基线、逐条变异、立刻还原、计数。
抄到第六遍时它已经开始**漂移** —— 而且漂移的方式正好是这个脚本要防的那一类：

  · red91 的锚点在重构后**命中 0 次**，静默 SKIP，读起来像"这条不重要"
  · red91 被 SIGTERM 杀掉时 `finally` **没跑**，变异**留在了源码里**，
    下一次跑的"基线"把残留当成了正常代码（真发生过：`answers.py` 里
    `grounded_rate` 的漂移检查被换成"和自己比"，基线因此报红）

这两件事都不是某一轮的 bug，是**骨架本身**的缺口。所以收进一处：
以后修一次，所有轮都受益。`red87`~`red91` 保持原样（它们是已完成轮次的
**记录**，重写它们等于改历史）；`red92` 起用这里。

--------------------------------------------------------------------------
四条纪律（前三条从 red87 起就有，第四条是本轮加的）

**一、锚点命中次数必须 == 1**
    不唯一时静默 SKIP。SKIP 读起来像"这条守点不重要"，其实是**锚点过期了**。

**二、每条变异之后立刻还原**
    否则后一条的计数被前一条的残留污染。

**三、子进程输出按 utf-8 解**
    否则 GBK 解码会崩在一堆 threading 栈里，看不到真正的失败。

**四、`finally` 挡不住信号**
    被 SIGTERM / SIGINT 杀掉时 Python 不跑 `finally`。所以除了 `finally`，
    还要装信号处理；并且**启动时**比对备份与源码 —— 不一致就拒绝启动。
    ⚠️ 不自动还原："备份 ≠ 源码"有两种原因（上次被杀 / 有人改过源码），
    两者在文件上长得一模一样，自动挑一种就是**替操作者猜**。

--------------------------------------------------------------------------
用法

    from redkit import Mutation, run_red

    MUTATIONS = [
        Mutation("M1", "说明", ANS, _OLD, _NEW),
    ]

    if __name__ == "__main__":
        raise SystemExit(run_red("Context 装配", MUTATIONS, backup_dir=BACKUP))
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
PY = sys.executable
#: 单次跑套件的上限。1643 条用例约 11s；留足余量。
RUN_TIMEOUT = 300

COUNT = re.compile(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

#: 已经落盘的原始内容。`finally` 与信号处理都读它 —— 见 `_restore_all`。
_ORIGINALS: dict[Path, str] = {}


@dataclass(frozen=True)
class Mutation:
    """把 `old` 换回旧写法，看哪些用例会红。

    `old` 必须在 `path` 里**恰好出现一次** —— 否则 SKIP（纪律一）。
    """

    name: str
    desc: str
    path: Path
    old: str
    new: str


def _restore_all(reason: str) -> None:
    """把 `_ORIGINALS` 里的文件还原。**幂等** —— `finally` 和信号处理都调它。"""
    for p, text in list(_ORIGINALS.items()):
        try:
            if p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")
                print(f"⚠️ [{reason}] {p.name} 有残留变异，已还原", flush=True)
        except OSError:
            pass


def _install_signal_guard() -> None:
    """`finally` 挡不住 SIGTERM —— 见模块 docstring 纪律四。"""

    def _handler(signum: int, _frame: object) -> None:
        _restore_all(f"signal {signum}")
        raise SystemExit(1)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _check_residue(targets: set[Path], backup_dir: Path) -> list[str]:
    """比对备份与源码，返回不一致的那些（**不自动还原**，见模块 docstring）。"""
    dirty: list[str] = []
    for p in sorted(targets):
        bak = backup_dir / p.name
        if not bak.exists():
            continue
        if bak.read_text(encoding="utf-8") != p.read_text(encoding="utf-8"):
            dirty.append(f"{p}  ≠  {bak}")
    return dirty


def run_suite(timeout: int = RUN_TIMEOUT) -> tuple[int, int, list[str]]:
    """跑全量单测，返回 `(failures, errors, 失败的用例 id)`。

    看不出来时返回 `(-1, -1, ...)`，**不猜** —— "没红"和"没跑起来"是两件事。
    """
    try:
        proc = subprocess.run(
            [PY, "-u", "-m", "unittest", "discover", "-s", "tests/unit", "-t", "."],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONPATH": "."},
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -2, -2, ["<超时 —— 这条变异让用例转不完>"]
    out = proc.stdout + proc.stderr
    m = COUNT.search(out)
    if m:
        return int(m.group(1) or 0), int(m.group(2) or 0), sorted(set(TEST_ID.findall(out)))
    if re.search(r"^OK$", out, re.MULTILINE):
        return 0, 0, []
    return -1, -1, ["<看不出来 —— 没有 OK 也没有 FAILED>"]


def run_red(label: str, mutations: Sequence[Mutation], *, backup_dir: Path) -> int:
    """跑一轮变红验证。返回进程退出码。"""
    targets = {m.path for m in mutations}

    dirty = _check_residue(targets, backup_dir)
    if dirty:
        print("⚠️ 备份与源码**不一致** —— 上一次很可能被中断，留下了残留变异：", flush=True)
        for d in dirty:
            print(f"    {d}", flush=True)
        print("  两种可能：① 上次被杀在变异中间；② 备份过期（源码后来改过）。", flush=True)
        print("  确认源码是你想要的那一份之后，删掉备份目录再跑：", flush=True)
        print(f"    rm -rf {backup_dir}", flush=True)
        return 3

    _install_signal_guard()

    print("=" * 74, flush=True)
    print(f"{label} · 基线（未变异）", flush=True)
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

    backup_dir.mkdir(parents=True, exist_ok=True)
    originals: dict[Path, str] = {}
    for p in targets:
        text = p.read_text(encoding="utf-8")
        originals[p] = text
        (backup_dir / p.name).write_text(text, encoding="utf-8")   # 落盘备份
    _ORIGINALS.update(originals)

    results: list[tuple[str, str, int, int, list[str]]] = []

    try:
        for mut in mutations:
            text = mut.path.read_text(encoding="utf-8")
            hits = text.count(mut.old)
            if hits != 1:
                print(
                    f"{mut.name}: ⚠️ SKIP —— 锚点命中 {hits} 次（应为 1）：{mut.desc}",
                    flush=True,
                )
                results.append((mut.name, mut.desc, -1, -1, []))
                continue

            mut.path.write_text(text.replace(mut.old, mut.new, 1), encoding="utf-8")
            try:
                f, e, ids = run_suite()
            finally:
                mut.path.write_text(originals[mut.path], encoding="utf-8")   # 立刻还原

            if f == -2:
                print(f"{mut.name}: ⏱ 超时（{RUN_TIMEOUT}s）—— {mut.desc}", flush=True)
                results.append((mut.name, mut.desc, -2, -2, ids))
                continue

            total = f + e
            flag = "★ 红" if total > 0 else "⚠️ 没红"
            print(f"{mut.name}: {flag}  failures={f} errors={e}   ({mut.desc})", flush=True)
            for i in ids[:4]:
                print("        ", i, flush=True)
            if len(ids) > 4:
                print(f"         … 另 {len(ids) - 4} 条", flush=True)
            results.append((mut.name, mut.desc, f, e, ids))
    finally:
        _restore_all("finally")

    print(flush=True)
    print("=" * 74, flush=True)
    print("汇总", flush=True)
    print("=" * 74, flush=True)
    red = [r for r in results if r[2] + r[3] > 0]
    silent = [r for r in results if r[2] + r[3] == 0]
    skipped = [r for r in results if r[2] < 0]
    print(
        f"  变异 {len(mutations)} 条：红了 {len(red)} / 没红 {len(silent)} / 超时或跳过 {len(skipped)}",
        flush=True,
    )
    for name, desc, f, _, _ in silent:
        print(f"  ⚠️ {name} 没红 —— {desc}（先怀疑测试，再怀疑变异本身）", flush=True)
    for name, desc, f, _, _ in skipped:
        tag = "超时" if f == -2 else "跳过"
        print(f"  ⚠️ {name} {tag} —— {desc}", flush=True)
    print(flush=True)
    print("  ⚠️ 「没红」不等于「这条守点不重要」：先问变异是不是真的改了行为", flush=True)
    print("     （给字段加注释不算变异 —— 字段还在，什么都没变）。", flush=True)
    return 0
