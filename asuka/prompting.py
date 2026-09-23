"""提示词与回答解析 —— 与"谁来调模型"解耦的纯函数。

--------------------------------------------------------------------------
为什么单独一个模块

这些函数原本住在 `asuka/deepseek.py` 里，和一份 `urllib` 直连 DeepSeek 的
HTTP 客户端混在一起。现在**模型调用属于 AgentOS**（Runtime 的 ModelGateway
+ `LLMCallExecutor`），Asuka 不再自带 LLM 客户端 —— 但"问什么、怎么解析回答"
仍是被测系统的一部分，必须留在 Asuka：

    `build_prompt`      把问题 + 装配后的上下文拼成提示词
    `parse_response`    从模型回的 JSON 里取出答案文本与**自述引用**
    `prompt_id_for`     提示词的**身份**（版本 + 内容指纹），进报告与对比门

所以它们搬到这里：**没有一行 HTTP，没有 key，没有单价** —— 于是
"提示词/解析"和"谁去执行这次调用"是两件事，各自只有一处定义。

⚠️ 提示词是被测系统的一部分：换 prompt 会被 `asuka.regression` 的同一性门
**拒绝**并排，而不是静默读成"系统退步/进步"。指纹取自**文本**（改一个字就变），
不是版本号（改了文本忘了改号会被绕过）。未知版本**拒绝**，不回退。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

# ---------------------------------------------------------------- 提示词
#
# v1：只钉"依据片段 / 同语言 / 必须引 [[key]]"。
#     实测（deepseek + bm25 + s3）：要点召回 0.2132、每要点字符 **495.6**（偏啰嗦）。
_SYSTEM_PROMPT_V1 = (
    "你是一个技术文档助手。**只能**依据用户提供的文档片段回答问题；"
    "片段没有覆盖的内容，明确说『文档未提及』。用与问题相同的语言回答。"
    "你**必须**在回答里引用你实际用到的片段，引用格式是片段里的 `[[key]]` 原文，"
    "不要改写、不要编造。最终严格按 JSON 格式输出。"
)

# v2：针对 v1 实测出的**两个病根**下药 ——
#   ① 啰嗦（495 字符/要点）：禁开场白、禁复述问题、禁客套；
#   ② 要点覆盖不足（召回 0.2132）：要求**逐条覆盖问题问到的具体事实**
#      （数值 / 返回值 / 边界条件 / 错误码），这正是 `RequiredPoint` 的写法。
# ⚠️ **绝不把 ground truth 喂进去** —— 那不是"提示词优化"，是泄漏答案键，
#    分数会变好看而系统一点没变好。下面这些要求是**通用的答题纪律**，与题目无关。
_SYSTEM_PROMPT_V2 = (
    "你是一个技术文档助手。**只能**依据用户提供的文档片段回答问题。\n"
    "要求：\n"
    "1. 直接回答：不要开场白、不要复述问题、不要客套、不要总结式收尾。\n"
    "2. 覆盖问题中问到的**每一个具体事实点** —— 数值、返回值、边界条件、"
    "错误码、前置条件，逐条说清，不要笼统带过。\n"
    "3. 片段没有覆盖的内容，明确说『文档未提及』；**不要推测、不要凭记忆补充**。\n"
    "4. 用与问题相同的语言回答。\n"
    "5. 你**必须**引用你实际用到的片段，引用格式是片段里的 `[[key]]` 原文，"
    "不要改写、不要编造。\n"
    "最终严格按 JSON 格式输出。"
)

PROMPT_VERSIONS: dict[str, str] = {
    "v1": _SYSTEM_PROMPT_V1,
    "v2": _SYSTEM_PROMPT_V2,
}
DEFAULT_PROMPT_VERSION = "v1"


def system_prompt(version: str) -> str:
    """这版提示词的**文本**。未知版本直接**拒绝**，不静默回退到 v1 ——
    回退会让"我选了 v2"变成"其实跑的是 v1"，而分数看起来正常。"""
    text = PROMPT_VERSIONS.get(version)
    if text is None:
        raise ValueError(
            f"unknown prompt version: {version!r} (可选：{sorted(PROMPT_VERSIONS)})"
        )
    return text


def prompt_id_for(version: str) -> str:
    """`版本-内容指纹`。

    ⚠️ **必须带内容指纹，不能只写版本号**：版本号是人写的标签，改了提示词却忘了
    改版本号，两个不同的 prompt 会共用一个 id —— 而对比门看见"相同"就放行，
    于是又一次静默。指纹由**文本**算出，改一个字就会变。
    """
    text = system_prompt(version)
    return f"{version}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def build_prompt(question: str, contexts: Sequence[Any]) -> str:
    """把问题 + 装配后的上下文拼成提示词。

    每个上下文用 `[[key]]` 显式标出自己的 chunk_id —— 模型**必须**回它实际引用的
    那些 key（纪律：引用是模型自述的，不是我们推的）。编号 `[n]` 只是人读方便，
    真正的 id 是 `[[...]]` 里的那段。

    `contexts` 只要求对象带 `.key` / `.text`（duck-typed），
    于是 AgentOS 侧把检索结果包一层就能复用，不必是 `ContextItem`。
    """
    blocks: list[str] = []
    for n, c in enumerate(contexts, 1):
        blocks.append(f"[{n}] [[{c.key}]]\n{c.text}")
    docs = "\n\n".join(blocks) if blocks else "（本次检索没有可用的上下文）"

    return (
        f"下面是技术文档的若干片段，**只能**依据这些片段回答问题。\n\n"
        f"==== 文档片段 ====\n{docs}\n==== 结束 ====\n\n"
        f"问题：{question}\n\n"
        f"请严格按下面的 JSON 格式回答（不要输出多余内容）：\n"
        f"{{\n"
        f'  "answer": "你的回答（用与问题相同的语言）",\n'
        f'  "citations": ["你实际引用了的片段 [[key]]，按顺序列出；没引任何片段就给空数组"]\n'
        f"}}\n\n"
        f"⚠️ 规则：\n"
        f"1. 答案**只能**来自上面的片段；片段没覆盖就说『文档未提及』。\n"
        f"2. `citations` 必须是片段里出现的 **`[[key]]` 原文**，不要改写、不要编造。\n"
        f"3. 只回 JSON。"
    )


def parse_response(
    content: str, contexts: Sequence[Any]
) -> tuple[str, tuple[str, ...] | None]:
    """从模型回的 JSON 里取出答案文本与自述引用。

    返回 `(answer_text, citations)`。`citations` 为 `None` 表示**没自述**
    （不可测，会在报告里被点名）；`()` 表示明确说"没引任何来源"（可测、召回 0）。

    解析不到 JSON 时退化为 `(原文, None)`：模型**确实生成了内容**（要点召回仍照常判），
    只是引用那一路测不了——和『没自述』是同一形状，不许静默当成『引了 0 条』。
    """
    text = content.strip()
    parsed = _extract_json(text)
    if parsed is None:
        return text, None

    answer_text = parsed.get("answer", "")
    if not isinstance(answer_text, str):
        answer_text = str(answer_text)

    raw_cites = parsed.get("citations", [])
    if not isinstance(raw_cites, list):
        return answer_text, None

    return answer_text, _normalize_citations(raw_cites, contexts)


def _extract_json(text: str) -> dict[str, Any] | None:
    """尽量从模型输出里抠出 JSON 对象。

    模型偶尔会在 JSON 外加 ```json 围栏或废话；能抠就抠，抠不到返回 None。
    """
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    fence = text.find("```")
    if fence != -1:
        end = text.find("```", fence + 3)
        inner = text[fence + 3 : end] if end != -1 else text[fence + 3 :]
        inner = inner.strip()
        if inner.startswith("json"):
            inner = inner[4:].strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    lo, hi = text.find("{"), text.rfind("}")
    if lo != -1 and hi != -1 and hi > lo:
        try:
            obj = json.loads(text[lo : hi + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _normalize_citations(
    raw: Sequence[Any], contexts: Sequence[Any]
) -> tuple[str, ...]:
    """把模型回的引用归一化成合法的 chunk_id 列表。

    模型可能回 `[[redis:expire:000]]`（正确），也可能回序号 `[1]`（人读方便）。
    序号**只在本次提供的范围内**才映射成真正的 key；越界或乱写的字符串保留原样，
    交由引用判据去记——它可能因此被记成"编造"（引了没给它的东西），那是**诚实**的。
    """
    keys = [c.key for c in contexts]
    key_set = set(keys)
    out: list[str] = []
    for r in raw:
        s = str(r).strip().strip("[]").strip()
        if not s:
            continue
        if s in key_set:
            out.append(s)
            continue
        if s.isdigit():
            idx = int(s) - 1
            if 0 <= idx < len(keys):
                out.append(keys[idx])
                continue
        out.append(s)
    seen: set[str] = set()
    deduped: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return tuple(deduped)


__all__ = [
    "DEFAULT_PROMPT_VERSION",
    "PROMPT_VERSIONS",
    "build_prompt",
    "parse_response",
    "prompt_id_for",
    "system_prompt",
]
