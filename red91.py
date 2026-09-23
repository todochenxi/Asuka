"""变红验证：引用（Citation）指标。

把这道「引用的东西给它了吗 / 该引的依据引到没有」的判据逐个**改回旧写法**，
看哪些用例会红。

纪律（都是踩过的坑，与 red87~red90 同）：
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
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # 脚本就在仓库根
PY = sys.executable
RUN_TIMEOUT = 180                                # 单次跑套件的上限（秒）

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red91.orig"

#: 已经落盘的原始内容。`finally` 与信号处理都读它 —— 见 `_restore_all`。
_ORIGINALS: dict[Path, str] = {}


def _restore_all(reason: str) -> None:
    """把 `_ORIGINALS` 里的文件还原。**幂等**，`finally` 和信号处理都调它。"""
    for p, text in list(_ORIGINALS.items()):
        try:
            if p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")
                print(f"⚠️ [{reason}] {p.name} 有残留变异，已还原", flush=True)
        except OSError:
            pass


def _install_signal_guard() -> None:
    """⚠️ `finally` **挡不住 SIGTERM** —— 被信号杀掉时 Python 不会跑 `finally`，
    变异会**留在源码里**，而下一次跑的"基线"就把残留当成了正常代码。
    （这不是假设：本轮真被 Bash 的 120s 超时杀过一次，`answers.py` 里
    `grounded_rate` 的漂移检查被换成了"和自己比"，基线因此报红。）
    """

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


def _check_residue(targets: set[Path]) -> list[str]:
    """启动时比对备份与源码，返回不一致的那些。

    ⚠️ **不自动还原**。"备份 ≠ 源码"有两种原因：① 上一次被中断，留下残留变异；
    ② 有人在上次跑完之后改过源码（备份过期）。两者在文件上**长得一模一样** ——
    自动挑一种就是替操作者猜，而猜错的代价是**静默改掉别人的代码**。
    所以点名两边路径，让人显式处理。
    """
    dirty: list[str] = []
    for p in sorted(targets):
        bak = BACKUP / p.name
        if not bak.exists():
            continue
        if bak.read_text(encoding="utf-8") != p.read_text(encoding="utf-8"):
            dirty.append(f"{p}  ≠  {bak}")
    return dirty

ANS = ROOT / "asuka" / "answers.py"
TRC = ROOT / "asuka" / "trace.py"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

# ---------------------------------------------------------------- 锚点

# 「引了、给了、而且确实属于 ground truth」—— M1 用
_EVIDENCE_CITED = (
    "        evidence_cited=tuple(x for x in cited_u if x in avail and x in ev),"
)
# 编造 —— M2 用
_FABRICATED = "        fabricated=tuple(x for x in cited_u if x not in avail),"

# `None` 一值三义里的那个 `None` —— M3 用
_GROUNDED_GUARD = (
    "        if self.cited is None or not self.cited:\n"
    "            return None"
)
# 自述与否 —— M4 用
_REPORTED = (
    '        """这一题有没有自述引用。**没自述 ⇒ 引用指标全部不可测。**"""\n'
    "        return self.cited is not None"
)
# 聚合口径 —— M5 用
_RECALL_EACH = (
    "    recall_each = [\n"
    "        len(s.citation.evidence_cited) / s.citation.evidence_total\n"
    "        for s in reported\n"
    "        if s.citation.evidence_total > 0\n"
    "    ]"
)
# 分母只有自述过的样本 —— M6 用
_REPORTED_FILTER = "    reported = [s for s in scores if s.citation.reported]"
# 旧格式的门 —— M7 用
_OLD_FORMAT_GATE = '        if "citation" not in d:'
# `None` 落成 `null`（**存下来的那一层**）—— M8 用
# ⚠️ 锚点原来指向 `Answer.as_dict()` —— 那一对方法**零调用者**，
# 改坏了什么都不红。它已被删掉（见 `answers.py` 里那段注释），
# 现在锚在真正落盘的那一层 `CitationVerdict.as_dict`。
_ANSWER_DICT_CITES = '            "cited": None if self.cited is None else list(self.cited),'
# trace 的 `generation` 事件里 `None` 落成 `null` —— M21 用（在 answers.py，不是 trace.py）
_SCORING_CITES = "                    citations=None if ans.citations is None else list(ans.citations),"
# oracle 引它拿到的 —— M9 用
# ⚠️ 装配之后 `contexts` 是 `ContextItem`（不是 `Chunk`），所以是 `i.key` 不是 `c.chunk_id`。
_ORACLE_CITES = "            citations=tuple(i.key for i in contexts),"
# null 明确自述"没引用" —— M10 用
_NULL_CITES = '        return Answer(text="", citations=())'
# 两条聚合漂移检查 —— M11 / M12 用
_DRIFT_FLOAT = '            ("overall.grounded_rate", self.overall.grounded_rate, fresh.grounded_rate),'
_DRIFT_INT = '            ("overall.fabricated_total", self.overall.fabricated_total, fresh.fabricated_total),'
# `None` 与数字的区分 —— M13 用
_NONE_VS_NUMBER = (
    "            if (stored is None) != (again is None):\n"
    '                drift.append(f"{name}：文件里 {stored}，按单题重算是 {again}")\n'
    "            elif stored is not None and again is not None and abs(stored - again) > 1e-3:\n"
    '                drift.append(f"{name}：文件里 {stored:.4f}，按单题重算是 {again:.4f}")'
)
# 不可测印成 `—` —— M14 用
_FMT_OPT = '    return "—" if v is None else f"{v:.{digits}f}"'
# available 只算装配后真正进 prompt 的 —— M15 用
# ⚠️ 锚点是**装配之后**那一行。改回 `retrieved` 就绕过了 C-3 预算：
# 被预算丢掉的片会被读成"给过它" ⇒ 编造读成有依据（分数只会变好看）。
_AVAILABLE = "        available = list(assembled.chunk_ids)"
# 归因拆三行 —— M16 用
_ATTRIBUTION_ROWS = (
    '    lines.append(f"| 检索**根本没检到** | {not_retrieved} | 换检索器 / 扩语料 |")\n'
    '    lines.append(f"| 检到了但**装不进预算** | {dropped_ev} | 加窗口 / 降 top_k |")\n'
    '    lines.append(f"| 给了它却**没引** | {ignored} | 改 prompt / 换模型 |")'
)
# trace 的 generation 必填 —— M17 用
_GEN_REQUIRED = '    "generation": ("task_id", "answerer", "chars", "citations"),'
# trace 侧独立重算编造 —— M18 用
_FAB_CROSSCHECK = "        if o.fabricated_total != fab_from_trace:"
# explain 里的编造段 —— M19 用
_EXPLAIN_FAB = "            if fab:"
# explain 里的"没自述"段 —— M20 用
_EXPLAIN_SILENT = (
    "            if cites is None:\n"
    "                # 缺字段由 `verify()` 挡掉；走到这里 `None` 只可能是**答案器没自述**。\n"
    "                lines.append(\n"
    '                    "- ⚠️ **没有自述引用** —— 引用指标对这条样本**不可测**"\n'
    '                    "（不是『没引用』）"\n'
    "                )"
)

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "M1",
        "「依据命中」不再要求**给过它**（靠记忆背出答案的模型也能拿满依据召回）",
        ANS,
        _EVIDENCE_CITED,
        "        evidence_cited=tuple(x for x in cited_u if x in ev),",
    ),
    (
        "M2",
        "编造集合恒为空（探测器成了**装饰**，报告永远显示『没有编造』）",
        ANS,
        _FABRICATED,
        "        fabricated=(),",
    ),
    (
        "M3",
        "`None`（没自述）的比例算成 `0.0`（『没测』变成『测了，结果是 0』）",
        ANS,
        _GROUNDED_GUARD,
        "        if self.cited is None or not self.cited:\n            return 0.0",
    ),
    (
        "M4",
        "`reported` 恒真（没自述的样本也算进了引用分母）",
        ANS,
        _REPORTED,
        '        """这一题有没有自述引用。**没自述 ⇒ 引用指标全部不可测。**"""\n'
        "        return True",
    ),
    (
        "M5",
        "聚合改回 Σ/Σ（与检索报告 `context_recall` 的口径分叉）",
        ANS,
        _RECALL_EACH,
        "    recall_each = [\n"
        "        sum(len(s.citation.evidence_cited) for s in reported)\n"
        "        / max(1, sum(s.citation.evidence_total for s in reported))\n"
        "    ]",
    ),
    (
        "M6",
        "`_group` 里分母退化成全部样本（没自述的被算成『没引』）",
        ANS,
        _REPORTED_FILTER,
        "    reported = list(scores)",
    ),
    (
        "M7",
        "旧格式报告的门拿掉（缺键被读成『没引用任何来源』）",
        ANS,
        _OLD_FORMAT_GATE,
        "        if False:",
    ),
    (
        "M8",
        "`CitationVerdict.cited` 的 `None` 落成 `[]`（往返一次就变成『明确说没引用』）",
        ANS,
        _ANSWER_DICT_CITES,
        '            "cited": list(self.cited or ()),',
    ),
    (
        "M9",
        "oracle 不再引它拿到的（判据上界塌掉）",
        ANS,
        _ORACLE_CITES,
        "            citations=(),",
    ),
    (
        "M10",
        "`null` 返回 `citations=None`（下界从『可测的 0』退化成『不可测』）",
        ANS,
        _NULL_CITES,
        '        return Answer(text="", citations=None)',
    ),
    (
        "M11",
        "`grounded_rate` 的漂移检查拿掉",
        ANS,
        _DRIFT_FLOAT,
        '            ("overall.grounded_rate", self.overall.grounded_rate, self.overall.grounded_rate),',
    ),
    (
        "M12",
        "`fabricated_total` 的漂移检查拿掉",
        ANS,
        _DRIFT_INT,
        '            ("overall.fabricated_total", self.overall.fabricated_total, self.overall.fabricated_total),',
    ),
    (
        "M13",
        "不再区分 `None` 与数字（`None` 与 0.6 之间漂移被放过）",
        ANS,
        _NONE_VS_NUMBER,
        "            if stored is not None and again is not None and abs(stored - again) > 1e-3:\n"
        '                drift.append(f"{name}：文件里 {stored:.4f}，按单题重算是 {again:.4f}")',
    ),
    (
        "M14",
        "不可测印成 `0.0000` 而不是 `—`",
        ANS,
        _FMT_OPT,
        '    return f"{(v or 0.0):.{digits}f}"',
    ),
    (
        "M15",
        "`available` 退回**检索结果**（绕过 C-3 预算：被丢掉的片也算『给过它』）",
        ANS,
        _AVAILABLE,
        "        available = list(retrieved)",
    ),
    (
        "M16",
        "缺的依据归因**合成一行**（『检索没检到』被读成『模型没用依据』）",
        ANS,
        _ATTRIBUTION_ROWS,
        '    lines.append(f"| 缺的依据 | {not_retrieved + dropped_ev + ignored} |")',
    ),
    (
        "M17",
        "trace 的 `generation` 不再要求 `citations`（缺字段与『没自述』混成一个）",
        TRC,
        _GEN_REQUIRED,
        '    "generation": ("task_id", "answerer", "chars"),',
    ),
    (
        "M18",
        "trace 侧不再从 `citations − kept` 独立重算编造（两条路径变成同一条）",
        TRC,
        _FAB_CROSSCHECK,
        "        if False:",
    ),
    (
        "M19",
        "审计视图不再点出编造",
        TRC,
        _EXPLAIN_FAB,
        "            if False:",
    ),
    (
        "M20",
        "审计视图把『没自述』印成『自述 0 条』（沉默的差读起来像没有差）",
        TRC,
        _EXPLAIN_SILENT,
        "            if cites is None:\n"
        '                lines.append("- 自述引用 0 条：（无）")',
    ),
    (
        "M21",
        "trace 落盘时 `citations` 的 `None` 写成 `[]`（答案器没自述 ⇒ 变成『明确说没引用』）",
        ANS,
        _SCORING_CITES,
        "                    citations=list(ans.citations or ()),",
    ),
    (
        "M22",
        "`available` 把**被权限拒绝**的也算进去（看不到的东西也算它拿过）",
        ANS,
        _AVAILABLE,
        "        available = list(retrieved) + [c.chunk_id for c in result.denied]",
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
    targets = {ANS, TRC}

    dirty = _check_residue(targets)
    if dirty:
        print("⚠️ 备份与源码**不一致** —— 上一次很可能被中断，留下了残留变异：", flush=True)
        for d in dirty:
            print(f"    {d}", flush=True)
        print("  两种可能：① 上次被杀在变异中间；② 备份过期（源码后来改过）。", flush=True)
        print("  确认源码是你想要的那一份之后，删掉备份目录再跑：", flush=True)
        print(f"    rm -rf {BACKUP}", flush=True)
        return 3

    _install_signal_guard()

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

    BACKUP.mkdir(parents=True, exist_ok=True)
    originals: dict[Path, str] = {}
    for p in targets:
        text = p.read_text(encoding="utf-8")
        originals[p] = text
        (BACKUP / p.name).write_text(text, encoding="utf-8")   # 落盘备份
    _ORIGINALS.update(originals)

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
        _restore_all("finally")

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
