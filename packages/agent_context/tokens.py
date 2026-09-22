"""Tokenizer 端口（M17）。

**为什么"数 token"也要是个端口：**

Context 的取舍完全由 token 预算决定，而 token 数**没有廉价又准确的算法** ——
真实系统要调模型厂商的 tokenizer（tiktoken / anthropic 的 count_tokens）。
把它硬编码进分配逻辑，等于把 Context 工程钉在某个具体模型上：
换一个模型，分词粒度变了，预算判断就整体失真。

**低估比高估危险得多：**

    · 高估  → 少放一点内容，浪费一点窗口
    · 低估  → 请求真的超长 → `CONTEXT_LENGTH_EXCEEDED` → 这次调用彻底失败

所以默认实现（`HeuristicTokenizer`）刻意**向上取整**，并且给每个非空串至少 1 个 token
（空串 0 个）—— 保守方向是"少放"，不是"放多了炸掉"。
"""
from __future__ import annotations

from math import ceil
from typing import Protocol


class Tokenizer(Protocol):
    """数一段文本占多少 token。真实实现接厂商 tokenizer。"""

    def count(self, text: str) -> int: ...


class HeuristicTokenizer:
    """按字符数估算。只在没有真实 tokenizer 时兜底。

    中文的实际粒度远小于 4 字符/token，所以 `chars_per_token` 默认取 **3**（偏保守）。
    要准确就换真实实现 —— 这里不假装精确。
    """

    def __init__(self, chars_per_token: int = 3) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be > 0")
        self.chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, ceil(len(text) / self.chars_per_token))
