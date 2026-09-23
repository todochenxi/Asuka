"""诊断：丢掉的要点，是**语料里根本没有**，还是**语料里有、模型没写出来**？

--------------------------------------------------------------------------
为什么必须查这一刀

第十轮之后有两个互相打架的说法：

- 说法 A：「瓶颈是**语料覆盖** —— hard 题的要点 redis.io 里没有，补文档有用。」
  （依据：报告里 r-hard-05 / r-hard-08 显式声明了语料缺口。）
- 说法 B：「13 道零要点题里**只有 2 道**声明了缺口 ⇒ 另外 11 道的材料**应该**在，
  所以是**生成侧**问题，补文档没用。」

两个都用同一份报告当依据，结论相反 ⇒ **别用直觉选，去查**。

--------------------------------------------------------------------------
怎么判

对每个「真丢」的要点，拿它的 `any_of` 原文去语料里搜，落到三类中**恰好一类**：

| 类别 | 含义 | 该动什么 |
|---|---|---|
| `in_evidence` | 原文就在**这题声明的依据**里 | **生成侧** —— 补文档没用 |
| `in_corpus_only` | 语料里有，但不在声明的依据里 | 标注 / 检索（evidence 标窄了，或检索没捞到） |
| `not_in_corpus` | 全语料都搜不到 | **语料缺口** —— 补文档才有用 |

⚠️ 「真丢」= 该题**全部采样**都丢（第十轮实测：单次采样噪声 0.023，
和待判效应同量级 ⇒ 只拿一次采样的 missed 会掺进噪声）。

⚠️ 匹配用**词边界**（同 `RequiredPoint.matched_by` 的纪律）：
不然 `set` 会命中 `subset`，把"语料里有"读成有 ⇒ 高估语料覆盖、低估生成问题。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import defaultdict
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
CORPUS = ROOT / "asuka" / "corpus" / "redis" / "chunks.jsonl"
DATASET = ROOT / "asuka" / "datasets" / "redis.jsonl"


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _word_boundary(phrase: str) -> re.Pattern[str]:
    """词边界匹配。首尾是单词字符时才加 `\\b`（标点开头/结尾不能加，会永远不匹配）。"""
    p = re.escape(_norm(phrase))
    if re.match(r"\w", phrase[:1] or " "):
        p = r"\b" + p
    if re.match(r"\w", phrase[-1:] or " "):
        p = p + r"\b"
    return re.compile(p)


# ⚠️⚠️ **严格匹配会把"材料在"误判成"材料不在" —— 实测踩到**
# `redis:expire:001` 原文是 "set a timeout on key ... will automatically be deleted"，
# 而要点写的是 `sets a timeout` / `automatically deleted` ⇒ 严格子串**两条都不中**，
# 于是 77 条里 66 条被判"语料缺口"（85.7%）。那个数是**匹配器的 artifact，不是发现**。
# ⇒ 必须再给一档**宽松**匹配，并把两档都印出来 —— 结论对匹配器的敏感度要可见。
_STOP = frozenset(
    "a an the on of in to for with is are be been being will would can "
    "and or at by from as that this it its".split()
)


def _stem(word: str) -> str:
    """极轻量的词干化：只够消掉屈折变化，不做真词法分析。"""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    return w


def _content_words(phrase: str) -> frozenset[str]:
    """去掉停用词后的**词干集合**（顺序信息丢了 —— 这是宽松的代价）。"""
    return frozenset(
        _stem(w) for w in re.findall(r"[a-z0-9]+", _norm(phrase)) if w not in _STOP
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in re.findall(r"[a-z0-9]+", _norm(text)))


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: probe94.py <答案报告.json> [...]", file=sys.stderr)
        return 2

    chunks = _load_jsonl(CORPUS)
    by_id = {c["chunk_id"]: c for c in chunks}
    # 语料全文（归一化后拼起来，用于 not_in_corpus 判定）
    corpus_text = "\n".join(_norm(c["text"]) for c in chunks)

    tasks = [t for t in _load_jsonl(DATASET) if "task_id" in t]
    points_by_task: dict[str, dict[str, list[str]]] = {
        t["task_id"]: {p["label"]: list(p["any_of"]) for p in t["required_points"]}
        for t in tasks
    }

    # unit_id+section → chunk 列表（这题**声明的依据**）
    def evidence_chunks(evidences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wanted = {(e["unit_id"], e.get("section") or "") for e in evidences}
        out = []
        for c in chunks:
            a = c.get("attributes") or {}
            key = (a.get("unit_id"), a.get("section") or "")
            if key in wanted:
                out.append(c)
        return out

    evid_chunks_by_task = {
        t["task_id"]: evidence_chunks(t.get("evidence") or []) for t in tasks
    }

    _tok_cache: dict[str, frozenset[str]] = {}

    def _tok(cid: str) -> frozenset[str]:
        if cid not in _tok_cache:
            _tok_cache[cid] = _tokens(by_id[cid]["text"])
        return _tok_cache[cid]
    diff_by_task = {t["task_id"]: t.get("difficulty", "?") for t in tasks}
    gap_by_task = {t["task_id"]: bool(t.get("out_of_corpus")) for t in tasks}

    for report_path in argv:
        rep = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))

        # 按题聚合：只算**全部采样都丢**的要点
        hits_per_task: dict[str, list[set[str]]] = defaultdict(list)
        total_per_task: dict[str, int] = {}
        for it in rep["items"]:
            tid = it["task_id"]
            hits_per_task[tid].append(set(it.get("hits") or ()))
            total_per_task[tid] = int(it.get("points_total") or 0)

        # 两档匹配：strict = 精确词边界；loose = 词干化后"内容词同块"
        buckets: dict[str, dict[str, list[tuple[str, str]]]] = {
            m: defaultdict(list) for m in ("strict", "loose")
        }
        robust: list[tuple[str, str, list[str]]] = []
        for tid, sample_hits in hits_per_task.items():
            all_hits = set().union(*sample_hits) if sample_hits else set()
            labels = points_by_task.get(tid, {})
            for label in labels:
                if label in all_hits:
                    continue  # 至少有一次采样答到 ⇒ 不算"真丢"
                robust.append((tid, label, labels[label]))

        for tid, label, phrases in robust:
            ev_chunks = evid_chunks_by_task.get(tid, [])
            for mode in ("strict", "loose"):
                if mode == "strict":
                    hit_ev = any(
                        _word_boundary(p).search(_norm(c["text"]))
                        for c in ev_chunks
                        for p in phrases
                    )
                    hit_any = any(
                        _word_boundary(p).search(_norm(c["text"]))
                        for c in chunks
                        for p in phrases
                    )
                else:
                    cw = [_content_words(p) for p in phrases]
                    hit_ev = any(
                        any(w <= _tok(c["chunk_id"]) for w in cw) for c in ev_chunks
                    )
                    hit_any = any(any(w <= _tok(c["chunk_id"]) for w in cw) for c in chunks)
                if hit_ev:
                    buckets[mode]["in_evidence"].append((tid, label))
                elif hit_any:
                    buckets[mode]["in_corpus_only"].append((tid, label))
                else:
                    buckets[mode]["not_in_corpus"].append((tid, label))

        name = pathlib.Path(report_path).name
        total = len(robust)
        print(f"\n{'=' * 74}\n{name}\n{'=' * 74}")
        print(f"真丢的要点（**全部采样都丢**）：{total} 条\n")

        order = [
            ("in_evidence", "① 原文就在**声明的依据**里 ⇒ 生成侧（补文档没用）"),
            ("in_corpus_only", "② 语料里有、不在声明依据里 ⇒ 标注 / 检索"),
            ("not_in_corpus", "③ 搜不到 ⇒ 语料缺口（补文档才有用）"),
        ]
        for mode, title in (
            ("strict", "【严格档】精确词边界子串"),
            ("loose", "【宽松档】词干化 + 内容词落在同一片"),
        ):
            print(title)
            for key, desc in order:
                rows = buckets[mode].get(key, ())
                pct = (len(rows) / total * 100) if total else 0.0
                print(f"   {desc}: {len(rows):3d} 条 ({pct:5.1f}%)")
            print()

        print("⚠️ **两档的差距本身就是结论**：严格档会把『材料在、只是措辞不同』"
              "误判成缺口")
        print("   （实测：语料写 `set a timeout`，要点写 `sets a timeout` ⇒ "
              "严格档判『不在』）。")
        print("   ⇒ 结论读**宽松档**；严格档只用来看这个偏差有多大。\n")

        # ⚠️ 还有一层混淆必须拆开：宽松档要求内容词落在**同一片**，
        # 而 hard 题恰恰需要**跨片综合**（"选型：hash vs string"要对比两篇文档）。
        # 这类会被算进 ③，但材料其实**在**，只是分散 ⇒ 不是缺口，是综合 / 检索问题。
        corpus_union: frozenset[str] = frozenset().union(
            *(_tok(c["chunk_id"]) for c in chunks)
        )
        not_found = buckets["loose"].get("not_in_corpus", ())
        spreadable = [
            (tid, label)
            for tid, label in not_found
            if any(
                _content_words(p) <= corpus_union
                for p in points_by_task.get(tid, {}).get(label, ())
            )
        ]
        if not_found:
            real_gap = len(not_found) - len(spreadable)
            print(
                f"⚠️ ③（宽松档 {len(not_found)} 条）里还有 {len(spreadable)} 条"
                f"能在语料**不同片之间拼齐** —— 材料在，只是要跨片综合"
            )
            print(
                f"   ⇒ **真正『材料缺失』≈ {real_gap} 条 "
                f"({real_gap / total * 100:.1f}%)**；其余是综合 / 检索问题。\n"
            )

        # ③ 的点名（宽松档）—— 唯一"补文档有用"的一类，必须落到具体题
        if buckets["loose"].get("not_in_corpus"):
            print("  ③（宽松档）逐条点名 —— 补这些材料才会涨分：")
            for tid, label in buckets["loose"]["not_in_corpus"]:
                flag = " [已声明缺口]" if gap_by_task.get(tid) else " [**未声明**]"
                print(f"   - {tid} ({diff_by_task.get(tid)}): {label}{flag}")

        # 按难度：生成侧（依据里就有却没写出来）占多少
        print("\n按难度拆「真丢」（宽松档）：")
        per_diff: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for tid, label, phrases in robust:
            d = diff_by_task.get(tid, "?")
            per_diff[d][1] += 1
            cw = [_content_words(p) for p in phrases]
            if any(
                any(w <= _tok(c["chunk_id"]) for w in cw)
                for c in evid_chunks_by_task.get(tid, [])
            ):
                per_diff[d][0] += 1
        for d in ("simple", "medium", "hard"):
            gen, tot = per_diff.get(d, [0, 0])
            if tot:
                print(
                    f"   {d:6s}: 真丢 {tot:3d} 条，其中**依据里就有** "
                    f"{gen:3d} 条 ({gen / tot * 100:.0f}%)"
                )

        _ = by_id  # 保留：后续若要按 chunk_id 反查
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
