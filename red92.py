"""变红验证：Context 装配（C-1 / C-3 / C-4 + 取舍顺序）。

把 `asuka/context.py` 里那条"受预算约束的装配"逐个**改回旧写法**，看哪些用例会红。

这一轮守的是四件事：

  · C-1  `Chunk → ContextItem` 这条边（`Chunk` 有 `.chunk_id`，`ContextItem` 有 `.key`）
  · C-3  预算是**硬约束**：装不下就丢，`reserved_for_output` 不许被吃掉
  · C-4  **静默截断是 bug**：每条被丢的都要留 `(id, tokens, 原因)`
  · 取舍顺序按**相关性**，不按 `chunk_id` 的字母序

骨架在 `redkit.py`（red92 起共用）—— 锚点唯一性、残留防护、信号处理都在那里。
"""
from __future__ import annotations

from pathlib import Path

from redkit import Mutation, run_red

ROOT = Path(__file__).resolve().parent
CTX = ROOT / "asuka" / "context.py"
TU = ROOT / "asuka" / "textutil.py"
ANS = ROOT / "asuka" / "answers.py"
TRC = ROOT / "asuka" / "trace.py"

BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red92.orig"

# ---------------------------------------------------------------- 锚点

# 「把检索名次写进 priority」—— N1 用
_RANKED = (
    "    ranked = tuple(\n"
    "        dataclasses.replace(item, priority=_RANK_BASE - rank)\n"
    "        for rank, item in enumerate(items)\n"
    "    )"
)
# 取舍的两个出口 —— N2 / N3 用
_DROPPED = "        dropped=plan.dropped,"
_TOTAL_TOKENS = "        total_tokens=plan.total_tokens,"
# 预算构造 —— N4 用
_BUDGET = "    tb = TokenBudget(total=budget, reserved_for_output=reserved_for_output)"
# tokenizer 传参 —— N5 用
_TOKENIZER = "    plan = allocate(ranked, tb, tokenizer=HeuristicTokenizer(chars_per_token))"
# `chunk_ids` 读的是**留下的**那些 —— N6 用
_CHUNK_IDS = "        return tuple(i.key for i in self.items)"
# 丢弃原因 —— N7 用
_DROPPED_REASONS = "        return tuple((d.key, d.tokens, d.reason) for d in self.dropped)"
# 记下用的是哪个假设 —— N8 用
_ASSEMBLED_CPT = "        chars_per_token=chars_per_token,"
# 委托给内核的 tokenizer —— N9 用
_ESTIMATE = "    return HeuristicTokenizer(chars_per_token).count(text)"

# trace：`context` 而不是 `kept` —— N10 用
_CTX_LOOKUP = '                out[e.task_id] = tuple(str(x) for x in e.data.get("context", ()))'
# 装配参数对账 —— N11 用
_BUDGET_DRIFT = (
    '            ("context_budget", report.context_budget, self.identity.context_budget),'
)
# `run.started` 的必填字段 —— N12 用
_STARTED_REQUIRED = (
    '    "run.started": (\n'
    '        "topic", "retriever", "top_k",\n'
    '        "context_budget", "reserved_for_output", "chars_per_token",\n'
    "    ),"
)
# `retrieval` 的必填字段 —— N13 用
_RETRIEVAL_REQUIRED = (
    '    "retrieval": (\n'
    '        "task_id", "query", "kept", "context", "dropped_budget", "context_tokens", "latency_ms",\n'
    "    ),"
)
# 审计视图里"模型没看见"的那个标记 —— N14 用
_EXPLAIN_MARK = (
    '                mark = "" if str(cid) in ctx_set else "  ← **被预算丢掉，模型没看见**"'
)
# 审计视图里的丢弃明细 —— N15 用
_EXPLAIN_DROPPED = "            if dropped:"

# answers：留痕进报告 / trace —— N16~N18 用
_SCORE_DROPPED = "                    context_dropped=assembled.dropped_reasons,"
_EVENT_CONTEXT = "                context=list(available),"
_SCORE_CTX_SIZE = "                    context_size=len(contexts),"

# (名字, 说明, 文件, 旧串, 新串)
MUTATIONS: list[Mutation] = [
    Mutation(
        "N1",
        "**不把名次写进 `priority`** ⇒ 取舍退化成按 `chunk_id` 字母序丢（丢掉第一名、留下最后一名）",
        CTX,
        _RANKED,
        "    ranked = tuple(items)",
    ),
    Mutation(
        "N2",
        "装配结果**不留被丢的片** ⇒ C-4 的留痕整段消失（模型没看见什么，事后答不出）",
        CTX,
        _DROPPED,
        "        dropped=(),",
    ),
    Mutation(
        "N3",
        "`total_tokens` 把**被丢的**也算进占用 ⇒ 虚报窗口使用（预算看起来永远超）",
        CTX,
        _TOTAL_TOKENS,
        "        total_tokens=plan.total_tokens + plan.dropped_tokens,",
    ),
    Mutation(
        "N4",
        "`reserved_for_output` 不扣 ⇒ 留给输出的位置被上下文吃掉（模型一个字吐不出来）",
        CTX,
        _BUDGET,
        "    tb = TokenBudget(total=budget, reserved_for_output=0)",
    ),
    Mutation(
        "N5",
        "装配层**不传** `chars_per_token`（退回内核默认 3）⇒ 与语料层的 4 分叉 33%",
        CTX,
        _TOKENIZER,
        "    plan = allocate(ranked, tb, tokenizer=HeuristicTokenizer())",
    ),
    Mutation(
        "N6",
        "`chunk_ids` 读**被丢的**那一半 ⇒ 下游拿到的上下文和实际喂进去的正相反",
        CTX,
        _CHUNK_IDS,
        "        return tuple(d.key for d in self.dropped)",
    ),
    Mutation(
        "N7",
        "丢弃原因**不留** ⇒ 只剩「丢了谁」，答不出「为什么丢的」",
        CTX,
        _DROPPED_REASONS,
        '        return tuple((d.key, d.tokens, "") for d in self.dropped)',
    ),
    Mutation(
        "N8",
        "装配结果**不记**用的是哪个 tokenizer 假设 ⇒ 「这个分母是谁划的」读不出来",
        CTX,
        _ASSEMBLED_CPT,
        "        chars_per_token=CHARS_PER_TOKEN,",
    ),
    Mutation(
        "N9",
        "`estimate_tokens` **自己再写一遍**（忽略入参）⇒ 同族算法两处实现，改一处就分叉",
        TU,
        _ESTIMATE,
        "    return max(1, (len(text) + 3) // 4)",
    ),
    Mutation(
        "N10",
        "trace 的 `retrieval_context()` 改读 `kept` ⇒ 「检到了」被读成「模型看见了」（编造读成有依据）",
        TRC,
        _CTX_LOOKUP,
        '                out[e.task_id] = tuple(str(x) for x in e.data.get("kept", ()))',
    ),
    Mutation(
        "N11",
        "装配参数**不对账**（报告和自己比）⇒ 「丢了 3 片」和「窗口 8192」可能不是同一次跑出来的",
        TRC,
        _BUDGET_DRIFT,
        '            ("context_budget", report.context_budget, report.context_budget),',
    ),
    Mutation(
        "N12",
        "`run.started` 不再要求装配字段 ⇒ 旧格式 trace 静默通过（说不出自己是在多大窗口下跑的）",
        TRC,
        _STARTED_REQUIRED,
        '    "run.started": ("topic", "retriever", "top_k"),',
    ),
    Mutation(
        "N13",
        "`retrieval` 不再要求 `context` / `dropped_budget` / `context_tokens` ⇒ 缺字段读成「没丢东西」",
        TRC,
        _RETRIEVAL_REQUIRED,
        '    "retrieval": ("task_id", "query", "kept", "latency_ms"),',
    ),
    Mutation(
        "N14",
        "审计视图**不标**「被预算丢掉，模型没看见」⇒ 丢掉的那些读起来像「给它了」",
        TRC,
        _EXPLAIN_MARK,
        '                mark = ""',
    ),
    Mutation(
        "N15",
        "审计视图**不列**丢弃明细与原因 ⇒ 看不到丢的是谁、为什么",
        TRC,
        _EXPLAIN_DROPPED,
        "            if False:",
    ),
    Mutation(
        "N16",
        "报告里**不写** `context_dropped` ⇒ 留痕只活在这一次调用里，两个产物都查不到",
        ANS,
        _SCORE_DROPPED,
        "                    context_dropped=(),",
    ),
    Mutation(
        "N17",
        "trace 的 `context` 写成 `retrieved` ⇒ 两个集合混成一个，「检到」读成「看见」",
        ANS,
        _EVENT_CONTEXT,
        "                context=list(retrieved),",
    ),
    Mutation(
        "N18",
        "报告里的 `context_size` 用**检索条数**而不是装配后的片数 ⇒ 占用看着正常，其实多报了",
        ANS,
        _SCORE_CTX_SIZE,
        "                    context_size=len(result.kept),",
    ),
]


if __name__ == "__main__":
    raise SystemExit(run_red("Context 装配", MUTATIONS, backup_dir=BACKUP))
