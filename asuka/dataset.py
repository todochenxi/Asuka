"""Task Dataset：问题 / 参考答案 / 出处 / 难度，**外加检索的 ground truth**。

--------------------------------------------------------------------------
为什么必须有 `evidence`

只有 `question` + `reference_answer` 的话，你只能评"答案对不对"。
但 Agent 答错时，你**说不出它错在哪一步**：

    是没检到该检的？（retrieval 的锅）
    还是检到了却没用上？（generation 的锅）

这两个原因的修法完全不同 —— 一个改切分/embedding，一个改 prompt/模型。
分不开，评测报告就只能说"这题错了"，说不出"该改哪里"。

所以每条任务额外声明**它依赖哪些文档位置**：

    evidence = [(unit_id, section), ...]        ← 人可读的 ground truth

加载时把它解析成 chunk_id，**解析不出来就报错**（不是跳过）。
这样"检索指标"才有分母：`context_recall` = 检到的 evidence chunk / 全部 evidence chunk。

⚠️ 为什么不直接写 chunk_id：chunk_id 是**切分参数的函数**。
改一次 `chunk_size`，所有手写的 chunk_id 全失效，而失效是**静默的** ——
数据集还在、跑得通、只是 ground truth 全指向了别的地方。
写 `(unit_id, section)` 则跨参数稳定，且解析失败会当场报错。

--------------------------------------------------------------------------
校验：一次报**全部**问题，不报第一个

这是这个项目的一条既有纪律（M88）：拒绝"整体做不到"时，**查整份，不查"下一个"**。
一个数据集有 5 处坏，就一次说完 5 处 ——
否则修一处跑一次，5 轮才收敛，而每一轮都像是"又发现一个新问题"。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from packages.agent_context.retrieval import Chunk

#: 难度分级（用户给的语义：简单=定义 / 中等=对比 / 困难=设计场景）
DIFFICULTIES: tuple[str, ...] = ("simple", "medium", "hard")

#: 从 `out_of_corpus` 的自由文本里挑出"被点名的符号"（大写命令名之类）。
#: 只用来做**一致性核对**，不用来发现缺口 —— 见 `TaskItem.out_of_corpus` 的说明。
_NAMED_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,15}\b")

DIFFICULTY_LABEL: dict[str, str] = {
    "simple": "定义（是什么 / 语法 / 复杂度 / 返回值）",
    "medium": "对比与选择（A 与 B 的差别、该用哪个）",
    "hard": "设计场景（用这些命令拼出一个方案）",
}


class DatasetError(RuntimeError):
    """数据集不合法。消息里带**全部**问题。"""


#: markdown 里的强调 / 代码标记。参考答案是**带 markdown 的**（`**head**`、`` `SET key value` ``），
#: 答案里通常没有 —— 不剥掉的话，声明得再对也会"匹配不上"，而失败**静默**。
#: 剥的是标记不是内容，所以这是机械去噪，不是猜。
_MARKDOWN_NOISE = str.maketrans("", "", "*`")


def _norm_text(s: str) -> str:
    """判据用的归一化：剥 markdown 标记 + 小写 + 折叠空白。**只此三项**，不多做。

    不做词干化 / 同义词扩展 —— 那是**猜**，而猜错会让判据静默变松。
    同一概念的多种说法由声明方用 `any_of` 显式列出。
    """
    return " ".join(s.translate(_MARKDOWN_NOISE).lower().split())


def _mentions(haystack: str, needle: str) -> bool:
    """`needle` 是否出现在 `haystack` 里（两边都须已 `_norm_text`）。

    ⚠️ 单词短语必须**词边界**匹配：否则要点 `set` 会在 `subset` 里"命中"，
    而这类假命中会让答案级分数**虚高** —— 比漏判更危险，因为它看起来是分数。
    多词短语按子串匹配（`time to live` 出现在 `the time to live value` 里）。
    """
    if " " not in needle:
        pat = rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])"
        return re.search(pat, haystack) is not None
    return needle in haystack


@dataclass(frozen=True)
class RequiredPoint:
    """一条**必答要点** —— 答案级判据的 ground truth。

    ------------------------------------------------------------------
    为什么用 `any_of`（多个可选说法），而不是一个字符串

    同一个概念有很多说法（`time to live` / `TTL`；`O(1)` / `常数时间`）。
    写死一个字符串，会把"**答对了但换了措辞**"判成错 ——
    于是分数反映的是"措辞像不像我"，不是"答对没有"。

    所以每条要点声明**一组**说法，命中任一即算答到。
    多写几个说法是声明方的工作量，**换来了判据的诚实**。

    ------------------------------------------------------------------
    为什么这仍然可能被"刷"

    判据是**要点召回**（答到几条），不是精确匹配 ——
    所以把整篇文档抄进答案，召回率会很高。
    这是已知的、**故意的**取舍：本模块只管"答到没有"，
    "答得冗不冗"由报告里的 `answer_chars` / `chars_per_point` **另行报出**，
    不塞进同一个数字里（一个数字混两种含义，读者无法归因）。

    试过加一个 `must_not_include`（"答案里不该出现的说法"）来补精确度，
    **又撤掉了**：自由文本里否定句会让子串匹配**反向命中** ——
    正确答案写"并不返回 -1 …"会被判成答错。
    一个会误判的字段比没有更糟，而且它**声明了却没有可靠的 producer**。
    """

    label: str
    any_of: tuple[str, ...]

    def matched_by(self, text: str) -> str:
        """返回**命中的那个说法**（空串 = 没命中）。

        返回"凭什么算答到了"而不是布尔值 —— 报告里要能回答
        "这题为什么算过了"，否则分数不可复核。
        """
        hay = _norm_text(text)
        for alt in self.any_of:
            n = _norm_text(alt)
            if n and _mentions(hay, n):
                return alt
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "any_of": list(self.any_of)}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "RequiredPoint":
        return RequiredPoint(
            label=str(d["label"]),
            any_of=tuple(str(x) for x in d.get("any_of", ())),
        )


@dataclass(frozen=True)
class Evidence:
    """一条 ground truth：某个单元的某一节。`section=""` 表示该单元任意节都算。"""

    unit_id: str
    section: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"unit_id": self.unit_id, "section": self.section}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "Evidence":
        return Evidence(unit_id=str(d["unit_id"]), section=str(d.get("section", "")))

    def __str__(self) -> str:
        return f"{self.unit_id}#{self.section}" if self.section else self.unit_id


@dataclass(frozen=True)
class TaskItem:
    task_id: str
    question: str
    reference_answer: str
    source_document: str
    difficulty: str
    evidence: tuple[Evidence, ...] = ()
    notes: str = ""
    #: 参考答案里**语料支撑不了**的部分 —— 每条一句话，说清缺什么。
    #:
    #: 非空 ⇒ "**即使检索完美也答不全**"。这题的**答案级**低分不该记在检索器头上；
    #: 它在报告里被点名，避免"分数低 = 检索差"这种默认读法。
    #:
    #: ⚠️ 这是**人工声明**，不是启发式推断。试过自动扫"参考答案里的命令名是否在语料里"：
    #: 24 题只抓到 2 个，**漏掉了 `r-hard-05`** —— 它缺的是 `HyperLogLog`，
    #: 不是一个大写命令 token。**一个会漏的检查器看起来权威，比没有更危险。**
    out_of_corpus: tuple[str, ...] = ()
    #: **必答要点** —— 答案级判据（`asuka.answers`）的 ground truth。
    #:
    #: 空元组 ⇒ 这题的答案级分数**不可测**，报告里记作 `—` 而不是 1.0。
    #: "没声明"和"答对了"是两件事，混在一起会让覆盖率看起来比实际高。
    required_points: tuple[RequiredPoint, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "reference_answer": self.reference_answer,
            "source_document": self.source_document,
            "difficulty": self.difficulty,
            "evidence": [e.as_dict() for e in self.evidence],
            "notes": self.notes,
            "out_of_corpus": list(self.out_of_corpus),
            "required_points": [p.as_dict() for p in self.required_points],
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "TaskItem":
        return TaskItem(
            task_id=str(d["task_id"]),
            question=str(d["question"]),
            reference_answer=str(d["reference_answer"]),
            source_document=str(d["source_document"]),
            difficulty=str(d["difficulty"]),
            evidence=tuple(Evidence.from_dict(e) for e in d.get("evidence", ())),
            notes=str(d.get("notes", "")),
            out_of_corpus=tuple(str(x) for x in d.get("out_of_corpus", ())),
            required_points=tuple(
                RequiredPoint.from_dict(p) for p in d.get("required_points", ())
            ),
        )


@dataclass
class Dataset:
    topic: str
    items: tuple[TaskItem, ...] = ()
    version: str = "0.1"
    notes: str = ""
    #: task_id → 解析出来的 chunk_id（`resolve` 之后才有）
    resolved: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ 读写

    @staticmethod
    def load(path: Path) -> "Dataset":
        meta: dict[str, Any] = {}
        items: list[TaskItem] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    meta = row["_meta"]
                    continue
                items.append(TaskItem.from_dict(row))
        return Dataset(
            topic=str(meta.get("topic", "")),
            items=tuple(items),
            version=str(meta.get("version", "0.1")),
            notes=str(meta.get("notes", "")),
        )

    def save(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps(
                    {
                        "_meta": {
                            "topic": self.topic,
                            "version": self.version,
                            "notes": self.notes,
                            "items": len(self.items),
                            "difficulties": dict(Counter(i.difficulty for i in self.items)),
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for item in self.items:
                fh.write(json.dumps(item.as_dict(), ensure_ascii=False) + "\n")
        return len(self.items)

    # ------------------------------------------------------------ 校验

    def validate(self, chunks: Sequence[Chunk]) -> None:
        """**一次报全部问题**（见模块 docstring）。"""
        problems: list[str] = []

        by_unit: dict[str, set[str]] = {}
        for c in chunks:
            by_unit.setdefault(str(c.attributes.get("unit_id", "")), set()).add(
                str(c.attributes.get("section", ""))
            )
        # 语料全文（小写）—— 只用于核对 `out_of_corpus` 的声明，不用于发现缺口。
        corpus_text = " ".join(c.text for c in chunks).lower()

        seen: set[str] = set()
        for item in self.items:
            where = f"[{item.task_id}]"
            if item.task_id in seen:
                problems.append(f"{where} task_id 重复")
            seen.add(item.task_id)

            if item.difficulty not in DIFFICULTIES:
                problems.append(
                    f"{where} difficulty={item.difficulty!r} 不在 {list(DIFFICULTIES)} 里"
                )
            if not item.question.strip():
                problems.append(f"{where} question 为空")
            if not item.reference_answer.strip():
                problems.append(f"{where} reference_answer 为空")
            if not item.source_document:
                problems.append(f"{where} source_document 为空")
            if not item.evidence:
                problems.append(f"{where} 没有 evidence —— 检索指标会没有分母")

            for ev in item.evidence:
                if ev.unit_id not in by_unit:
                    problems.append(f"{where} evidence 指向不存在的单元 {ev.unit_id!r}")
                elif ev.section and ev.section not in by_unit[ev.unit_id]:
                    have = ", ".join(sorted(s for s in by_unit[ev.unit_id] if s))
                    problems.append(
                        f"{where} evidence 指向 {ev.unit_id} 里不存在的 section "
                        f"{ev.section!r}（它有：{have}）"
                    )

            # `out_of_corpus` 是**声明**，所以它可以被证伪：
            # 声明里点名的符号如果语料里**其实有**，这条声明就是错的。
            for entry in item.out_of_corpus:
                named = _NAMED_TOKEN_RE.findall(entry)
                if named and all(t.lower() in corpus_text for t in named):
                    problems.append(
                        f"{where} out_of_corpus 声明 {entry!r}，"
                        f"但它点名的 {named} 在语料里**存在** —— 声明不成立"
                    )

            # 必答要点也是**声明**，同样可以被证伪：
            #
            #   一条要点如果**参考答案自己都答不到**，它是错的声明。
            #   这不是"这题难"，是标注写错了，且它会**永久压低**这题的分数，
            #   而读者会把它读成"模型不行"。
            #
            # 注意这条只查**声明内部一致**，不查"要点是否覆盖了参考答案"——
            # 后者需要判断"这句话算不算一个要点"，那是裁判模型的活（见 M12）。
            seen_labels: set[str] = set()
            for p in item.required_points:
                if not p.label.strip():
                    problems.append(f"{where} 有一条 required_point 缺 label")
                if p.label in seen_labels:
                    problems.append(f"{where} required_point 的 label {p.label!r} 重复")
                seen_labels.add(p.label)
                if not p.any_of:
                    problems.append(f"{where} required_point {p.label!r} 的 any_of 为空")
                elif not p.matched_by(item.reference_answer):
                    problems.append(
                        f"{where} required_point {p.label!r} 的说法 {list(p.any_of)} "
                        f"**没有一个**出现在参考答案里 —— 这条要点无人能答，声明不成立"
                    )

        if problems:
            raise DatasetError(
                f"数据集有 {len(problems)} 处问题：\n  " + "\n  ".join(problems)
            )

    # ------------------------------------------------------------ 解析

    def resolve(self, chunks: Sequence[Chunk]) -> dict[str, tuple[str, ...]]:
        """`(unit_id, section)` → chunk_id。解析不出来 → 报错（不跳过）。

        这一步让检索指标可算：`context_recall` 的分母就是这里的条数。
        """
        self.validate(chunks)
        index: dict[tuple[str, str], list[str]] = {}
        for c in chunks:
            unit = str(c.attributes.get("unit_id", ""))
            section = str(c.attributes.get("section", ""))
            index.setdefault((unit, section), []).append(c.chunk_id)

        out: dict[str, tuple[str, ...]] = {}
        for item in self.items:
            ids: list[str] = []
            for ev in item.evidence:
                if ev.section:
                    ids.extend(index.get((ev.unit_id, ev.section), []))
                else:
                    for (unit, _sec), chunk_ids in index.items():
                        if unit == ev.unit_id:
                            ids.extend(chunk_ids)
            out[item.task_id] = tuple(dict.fromkeys(ids))
        self.resolved = out
        return out

    # ------------------------------------------------------------ 视图

    def by_difficulty(self, difficulty: str) -> tuple[TaskItem, ...]:
        return tuple(i for i in self.items if i.difficulty == difficulty)

    def stats(self) -> dict[str, Any]:
        by_unit = Counter(i.source_document for i in self.items)
        return {
            "topic": self.topic,
            "items": len(self.items),
            "by_difficulty": dict(Counter(i.difficulty for i in self.items)),
            "units_covered": len(by_unit),
            "by_unit": dict(by_unit.most_common()),
            "evidence_total": sum(len(i.evidence) for i in self.items),
            "items_out_of_corpus": sum(1 for i in self.items if i.out_of_corpus),
            "required_points_total": sum(len(i.required_points) for i in self.items),
            #: ⚠️ 答案级指标的分母**只有**这个数 —— 没声明要点的题算不进去。
            #: 报出来，读者才知道"答案级 0.6"是在几道题上算的。
            "items_with_required_points": sum(1 for i in self.items if i.required_points),
            "resolved_tasks": len(self.resolved),
            "resolved_chunks": sum(len(v) for v in self.resolved.values()),
        }


def dataset_path(datasets_dir: Path, topic: str) -> Path:
    return datasets_dir / f"{topic}.jsonl"


def load_dataset(datasets_dir: Path, topic: str) -> Dataset:
    path = dataset_path(datasets_dir, topic)
    if not path.exists():
        raise DatasetError(f"没有任务集：{path}")
    return Dataset.load(path)
