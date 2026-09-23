"""变红验证：回归对比（"这次 vs 上次"）。

把 `asuka/regression.py` 里那几层判定逐个**改回会骗人的写法**，看哪些用例会红。

这一轮守的是四件事：

  · **口径**：每题通过 = 这道题的**全部采样**都通过（不是"至少一次"）
  · **不可测**：`points_total == 0` 的题**不进对比**，且必须点名
  · **同源**：两侧的 `passed` 必须用同一套口径推出来
  · **门**：配置变了就不是"这次 vs 上次"；拒绝时**一个指标数字都不印**

外加渲染层的三条老规矩：`None` 不印成 0、backlog 不叫回归、题库少了要看见。

骨架在 `redkit.py`（red92 起共用）—— 锚点唯一性、残留防护、信号处理都在那里。

⚠️ **必须后台跑**（全量 1750 条 × 26 条变异）。被 SIGTERM 杀掉时 `finally` 不跑，
   变异会留在源码里；`redkit` 的信号处理 + 启动残留检查就是为这个加的。
"""
from __future__ import annotations

from pathlib import Path

from redkit import Mutation, run_red

ROOT = Path(__file__).resolve().parent
REG = ROOT / "asuka" / "regression.py"

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red93.orig"

# ---------------------------------------------------------------- 锚点

# 题级口径 —— N1 / N2 / N14 用
_VERDICT_ALL = "        passed = all(r.passed for r in scorable)"
_VERDICT_SCOR = "        scorable = [r for r in rows if r.points_total > 0]"
_BY_TASK = "        out.setdefault(s.task_id, []).append(s)"

# 归因 —— N3 / N4 用
_WHY_ORDER = (
    "    if any(r.error for r in rows):\n"
    "        return ATTR_ERROR\n"
    "    if any(r.passed for r in rows):\n"
    "        return ATTR_FLAKY\n"
    "    return ATTR_WRONG"
)
_WHY_WRONG = "    return ATTR_WRONG"

# 同源 / 点名 / 方向 —— N5 ~ N8 用
_BASELINE = "    return {v.case_id: v.passed for v in task_verdicts(report)}"
_UNMEAS = (
    "    return sorted(\n"
    "        tid for tid, rows in _by_task(report.items).items()\n"
    "        if all(r.points_total <= 0 for r in rows)\n"
    "    )"
)
_ONLY_BEFORE = "    return sorted(set(_by_task(before.items)) - set(_by_task(after.items)))"
_REVERSED = "    return bool(a) and bool(b) and b < a"

# 可比性这道门 —— N9 ~ N13 用
_ID_ANSWERER = (
    '    ("answerer", "答案器", "换了答案器 ⇒ 同上；『模型 A 换模型 B』不是回归"),\n'
)
_ID_CPT = (
    '    ("chars_per_token", "每 token 字符数",'
    ' "它是**语料的属性**，变了说明语料或假设变了"),\n'
)
_ID_LOOP = (
    "        if a != b:\n"
    "            problems.append(\n"
    '                f"{label}（`{field}`）不一致：之前 {a!r}，之后 {b!r} —— {why}"\n'
    "            )"
)
_Q_CHECK = "        if b_items[tid].question != a_items[tid].question:"
_P_CHECK = "        if b_items[tid].points_total != a_items[tid].points_total:"

# 渲染 —— N15 ~ N21 用
_REFUSE_RET = (
    '        return "\\n".join(lines) + "\\n"\n'
    "\n"
    "    if looks_reversed(before, after):"
)
_REG = "    reg = regressions(ds)"
_STUCK = '    stuck = [d for d in ds if d.kind == "unchanged" and not d.before]'
_NUM_NONE = '    if v is None:\n        return "—"'
_PASSK = (
    '        if field == "pass_at_k" and before.samples_per_task <= 1:\n'
    "            # `samples == 1` 时它和 pass@1 是同一个数"
    " —— 印两行同名指标只会让人以为看错了。\n"
    "            continue"
)
_GONE = "    if gone:"
_COST = "    if b_o.total_cost_usd or a_o.total_cost_usd:"

# JSON —— N22 / N23 用
_AD_REFUSE = "    if problems:\n        return out"
_AD_REG = '    out["regressions"] = [d.case_id for d in regressions(ds)]'

# CLI —— N24 ~ N27 用
_CLI_REFUSE = (
    "        _write_outputs(args, before, after, md, stream=sys.stderr)\n"
    "        return 2"
)
_CLI_NO_WRITE = (
    "        print(md, file=sys.stderr)\n"
    "        _write_outputs(args, before, after, md, stream=sys.stderr)\n"
    "        return 2"
)
_CLI_EXIT = "    return 2 if problems else 0"
_CLI_READ = (
    '        print(f"\\n! 报告读不回来：{exc}", file=sys.stderr)\n        return 3'
)


MUTATIONS = [
    # ---- 题级口径：通过 = 全部采样通过
    Mutation(
        "N1",
        "口径退化成**至少一次通过** ⇒ `3/3 → 1/3` 会被读成「没事」",
        REG,
        _VERDICT_ALL,
        "        passed = any(r.passed for r in scorable)",
    ),
    Mutation(
        "N2",
        "**不可测的题也进对比** ⇒ 一道没测的题以「两次都不通过」的形状落进 unchanged",
        REG,
        _VERDICT_SCOR,
        "        scorable = list(rows)",
    ),
    Mutation(
        "N14",
        "`_by_task` 只留**最后一条采样** ⇒ 一道题的判定被最后一次采样代表",
        REG,
        _BY_TASK,
        "        out[s.task_id] = [s]",
    ),
    # ---- 归因
    Mutation(
        "N3",
        "归因优先级反了（不稳定压过报错）⇒ 管线问题被报成「答得不够好」",
        REG,
        _WHY_ORDER,
        "    if any(r.passed for r in rows):\n"
        "        return ATTR_FLAKY\n"
        "    if any(r.error for r in rows):\n"
        "        return ATTR_ERROR\n"
        "    return ATTR_WRONG",
    ),
    Mutation(
        "N4",
        "「每次都答不到」报成「不稳定」⇒ 该改 prompt 的被指向查温度",
        REG,
        _WHY_WRONG,
        "    return ATTR_FLAKY",
    ),
    # ---- 同源 / 点名 / 方向
    Mutation(
        "N5",
        "`baseline` 自己另算一份（逐样本）⇒ 两侧口径不同，对减出来的差全是口径的差",
        REG,
        _BASELINE,
        "    return {s.task_id: s.passed for s in report.items}",
    ),
    Mutation(
        "N6",
        "不可测的判据写反 ⇒ **能测的**被点名成不可测，真正的不可测静默消失",
        REG,
        _UNMEAS,
        "    return sorted(\n"
        "        tid for tid, rows in _by_task(report.items).items()\n"
        "        if all(r.points_total > 0 for r in rows)\n"
        "    )",
    ),
    Mutation(
        "N7",
        "`only_in_before` 方向反了 ⇒ 「只在基线里」列成「新增的题」",
        REG,
        _ONLY_BEFORE,
        "    return sorted(set(_by_task(after.items)) - set(_by_task(before.items)))",
    ),
    Mutation(
        "N8",
        "传反的判据方向反了 ⇒ 真的传反了不报警，正常的先后顺序反而报警",
        REG,
        _REVERSED,
        "    return bool(a) and bool(b) and a < b",
    ),
    # ---- 可比性这道门
    Mutation(
        "N9",
        "门里漏掉 `answerer` ⇒ 「模型 A 换模型 B」被报成回归",
        REG,
        _ID_ANSWERER,
        "",
    ),
    Mutation(
        "N10",
        "门里漏掉 `chars_per_token` ⇒ 语料假设变了也照比",
        REG,
        _ID_CPT,
        "",
    ),
    Mutation(
        "N11",
        "不可比时**只报第一个**原因 ⇒ 把人训练成「多跑几次」而不是「看一次报告」",
        REG,
        _ID_LOOP,
        "        if a != b:\n"
        "            problems.append(\n"
        '                f"{label}（`{field}`）不一致：之前 {a!r}，之后 {b!r} —— {why}"\n'
        "            )\n"
        "            break",
    ),
    Mutation(
        "N12",
        "不查逐题问句 ⇒ 同一个题号下换了问句，照样给出 regressed",
        REG,
        _Q_CHECK,
        "        if not b_items[tid].question:",
    ),
    Mutation(
        "N13",
        "不查逐题要点条数 ⇒ 「通过」的含义变了也照比",
        REG,
        _P_CHECK,
        "        if not b_items[tid].points_total:",
    ),
    # ---- 渲染
    Mutation(
        "N15",
        "拒绝时**继续往下渲染** ⇒ 一个不可比的对照表被当成结论读",
        REG,
        _REFUSE_RET,
        "    if looks_reversed(before, after):",
    ),
    Mutation(
        "N16",
        "报警列表不用 `regressions()` ⇒ 把改善/新增也报成回归（报警疲劳）",
        REG,
        _REG,
        '    reg = [d for d in ds if d.kind != "improved"]',
    ),
    Mutation(
        "N17",
        "backlog 段装的是 `regressed` ⇒ 两次都不过的被叫成「这次退步」",
        REG,
        _STUCK,
        '    stuck = [d for d in ds if d.kind == "regressed"]',
    ),
    Mutation(
        "N18",
        "不可测印成 `0` ⇒ `— → 0.5941` 被读成「从 0 涨上来了」",
        REG,
        _NUM_NONE,
        '    if v is None:\n        return "0"',
    ),
    Mutation(
        "N19",
        "`pass@k` 无条件印 ⇒ `samples == 1` 时同名指标印两遍，读者以为看错了",
        REG,
        _PASSK,
        '        if field == "pass_at_k" and before.samples_per_task <= 1:\n'
        "            pass",
    ),
    Mutation(
        "N20",
        "「只在基线里」的条件写反 ⇒ 题库少了几道静默消失",
        REG,
        _GONE,
        "    if not gone:",
    ),
    Mutation(
        "N21",
        "成本行无条件印 ⇒ 两边都没测时印两个 0，被读成「这次没花钱」",
        REG,
        _COST,
        "    if b_o.total_cost_usd >= 0 or a_o.total_cost_usd >= 0:",
    ),
    # ---- JSON
    Mutation(
        "N22",
        "`as_dict` 不可比时**不早退** ⇒ 机器可读的那份带着一堆不可比的数字",
        REG,
        _AD_REFUSE,
        '    if problems:\n        out["problems_ignored"] = problems',
    ),
    Mutation(
        "N23",
        "JSON 的 `regressions` 换成别的列表 ⇒ markdown 与 JSON 两层归因不一致",
        REG,
        _AD_REG,
        '    out["regressions"] = [d.case_id for d in ds if d.kind == "unchanged"]',
    ),
    # ---- CLI
    Mutation(
        "N24",
        "不可比时退出码 0 ⇒ CI 把一份拒绝出表的报告当成通过",
        REG,
        _CLI_REFUSE,
        "        _write_outputs(args, before, after, md, stream=sys.stderr)\n"
        "        return 0",
    ),
    Mutation(
        "N27",
        "拒绝时**不落盘**（退回 `compare` 的老写法）⇒ 显式给的 `--out` 被静默忽略",
        REG,
        _CLI_NO_WRITE,
        "        print(md, file=sys.stderr)\n        return 2",
    ),
    Mutation(
        "N25",
        "`--allow-non-comparable` 把退出码也放行了 ⇒ 逃生门变成默认通过",
        REG,
        _CLI_EXIT,
        "    return 0",
    ),
    Mutation(
        "N26",
        "报告读不回来时返回 0 ⇒ 输入问题被读成「对比通过」",
        REG,
        _CLI_READ,
        '        print(f"\\n! 报告读不回来：{exc}", file=sys.stderr)\n        return 0',
    ),
]


if __name__ == "__main__":
    raise SystemExit(run_red("回归对比", MUTATIONS, backup_dir=BACKUP))
