"""red95 —— **提示词身份（prompt_id）** 的变红验证。

为什么单开一轮：提示词是**被测系统的一部分**，但它不像 `retriever` / `top_k`
那样天然带在报告里 —— 它是"看不见的系统变更"。这一轮锁的是三件事：

1. 换 prompt 会被对比门**拦住**（不是读成退步/进步）；
2. `prompt_id` 带的是**内容指纹**，不是人写的标签；
3. 选了 v2 就真的**发 v2 的文本**（否则整场 A/B 白跑，而 id 看起来是对的）。

⚠️ 锚点里凡涉及源码里的转义序列（`\\n` 等）要在 Python 字符串里再转义一次 ——
red94 的 N6 就栽在这上面（命中 0 次会**静默 SKIP**，看起来像"这条不重要"）。
"""
from __future__ import annotations

import pathlib

from redkit import Mutation, run_red

ANS = pathlib.Path("asuka/answers.py")
DS = pathlib.Path("asuka/deepseek.py")
REG = pathlib.Path("asuka/regression.py")

# 门 —— N1 / N2
_PROMPT_ENTRY = """    (
        "prompt_id",
        "提示词版本",
        "**提示词是被测系统的一部分**（它决定引用自述与详略）；换了它 ⇒ 同上。"
        "⚠️ 空字符串也可能是『老报告没记录』，无法确认相同 ⇒ 同样拒绝",
    ),
"""

_GATE_IF = """        a, b = getattr(before, field), getattr(after, field)
        if a != b:
"""

# 指纹 —— N3
_FINGERPRINT = (
    "    return f\"{version}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}\""
)

# 未知版本 —— N4
_UNKNOWN_RAISE = """    text = PROMPT_VERSIONS.get(version)
    if text is None:
        raise ValueError(
            f"unknown prompt version: {version!r} (可选：{sorted(PROMPT_VERSIONS)})"
        )
"""

# 报告记录 —— N5
_REPORT_FIELD = '        prompt_id=str(getattr(answerer, "prompt_id", "") or ""),'

# 真发出去的是哪一版 —— N6
_SYSPROMPT_LOOKUP = "        text = PROMPT_VERSIONS.get(self.prompt_version)"

MUTATIONS = [
    Mutation(
        "N1",
        "把 prompt_id 从同一性表里删掉 ⇒ 换 prompt 不再被拦，效果被读成退步/进步",
        REG,
        _PROMPT_ENTRY,
        "",
    ),
    Mutation(
        "N2",
        "两边有一边为空就跳过 ⇒ 老报告（没记录）被当成『没有差异』放行（又一次静默）",
        REG,
        _GATE_IF,
        """        a, b = getattr(before, field), getattr(after, field)
        if a != b and a and b:
""",
    ),
    Mutation(
        "N3",
        "prompt_id 只用版本号、不带内容指纹 ⇒ 改了提示词忘了改号就共用同一个 id",
        DS,
        _FINGERPRINT,
        "    return version",
    ),
    Mutation(
        "N4",
        "未知版本静默回退到 v1 ⇒ 『我选了 v2』变成『其实跑的是 v1』，而分数看起来正常",
        DS,
        _UNKNOWN_RAISE,
        '    text = PROMPT_VERSIONS.get(version) or PROMPT_VERSIONS["v1"]\n',
    ),
    Mutation(
        "N5",
        "报告不记录 prompt_id（写死空） ⇒ 身份只活在内存里，落盘后无从核对",
        ANS,
        _REPORT_FIELD,
        '        prompt_id="",',
    ),
    Mutation(
        "N6",
        "system_prompt 忽略选择、永远发 v1 ⇒ 整场 A/B 白跑而 prompt_id 仍报 v2",
        DS,
        _SYSPROMPT_LOOKUP,
        '        text = PROMPT_VERSIONS.get("v1")',
    ),
]

if __name__ == "__main__":
    raise SystemExit(
        run_red(
            "red95 · 提示词身份（prompt_id）",
            MUTATIONS,
            backup_dir=pathlib.Path(".workbuddy-ai/tmp/red95.orig"),
        )
    )
