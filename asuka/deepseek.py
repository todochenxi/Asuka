"""真模型答案器：DeepSeek（OpenAI 兼容接口）。

--------------------------------------------------------------------------
为什么这是 #116 的另一半

`OracleAnswerer` / `NullAnswerer` 是**校准**（上下界），`FabricatingAnswerer`
是**编造探测器**。它们把"判据对不对"验过了，但**模型侧**这一栏一直是空的：

    `Answer.prompt_tokens` / `completion_tokens` / `cost_usd` / `latency_ms`

`oracle` / `null` 填 0 —— 那不是"免费"，是"没测"（见 `answers.py` 的纪律：
『没测』不许印成 0）。真模型接上后，这几个数**才有模型侧含义**：

    一个 Agent 的"答对率"脱离了"它花了多少 token / 多少钱 / 多少毫秒"，
    等于默认成本为零 —— 而企业里成本恰恰是上不上线的判据。

--------------------------------------------------------------------------
两条纪律（和答案级判据同源）

1. **引用必须是模型自己说的，不是我们推出来的。**
   `Answer.citations` 是答案器自述"用了哪几个 chunk_id"。一个靠参数记忆答对的
   模型和一个真读了语料的模型，在要点召回上**一模一样**——企业知识库里这是
   两件事。所以提示词里把每个上下文的 `key`（chunk_id）显式标出来，模型必须回
   **它实际引用的那些 key**。我们**不**反过来去答案里猜它引了谁——那会篡改"自述"。

2. **代价必须真测，不许填 0 糊弄。**
   `prompt_tokens` / `completion_tokens` 取自 DeepSeek 返回的 `usage`；
   `cost_usd` 按**声明的单价**算（单价会变动，见 `PRICING` 注释，可经环境变量覆盖）；
   `latency_ms` 是这一次的端到端往返。三者都进 `Answer`，报告里和正确率并排报。

--------------------------------------------------------------------------
零第三方依赖

核心层顶层 import 全是标准库。DeepSeek 的 HTTP 调用走**标准库 `urllib`**，
不引入 `requests` —— 这样单测能在零依赖解释器上跑（见 `test_asuka_deepseek_contract.py`）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from packages.agent_context.items import ContextItem

from .answers import Answerer, Answer

# DeepSeek 的 OpenAI 兼容接口。base_url 可经环境变量覆盖（自建网关 / 代理）。
DEFAULT_BASE_URL = "https://api.deepseek.com"

# ⚠️ 单价是**会漂移**的外部事实，不是实现细节。这里写的是发布时 deepseek-chat 的
# 公开价（非缓存输入 / 输出，单位 USD / 百万 token）。**以官方文档为准**；
# 两个值都能经环境变量覆盖，别把"写死的价格"读成"永远正确"。
DEFAULT_INPUT_COST_PER_MILLION = 0.27
DEFAULT_OUTPUT_COST_PER_MILLION = 1.10

# 系统提示词：把"只依据片段 / 同语言 / 必须引 [[key]]"这三件钉死。
# 它不参与评分，但决定了模型**会不会**自述引用——引用判据的前提是模型肯说。
_SYSTEM_PROMPT = (
    "你是一个技术文档助手。**只能**依据用户提供的文档片段回答问题；"
    "片段没有覆盖的内容，明确说『文档未提及』。用与问题相同的语言回答。"
    "你**必须**在回答里引用你实际用到的片段，引用格式是片段里的 `[[key]]` 原文，"
    "不要改写、不要编造。最终严格按 JSON 格式输出。"
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class DeepSeekAnswerer:
    """接 DeepSeek 的真答案器。**实现 `Answerer` 协议**。

    `name` / `is_calibration` 是两个**必须自述**的属性（见 `answers.Answerer`）：
    前者是这条记录在报告里叫什么，后者告诉读者"这是模型成绩，不是判据校准"。
    一个不说的 answerer 会被 `evaluate_answers` 拒绝。
    """

    name: str = "deepseek"
    is_calibration: bool = False

    model: str = "deepseek-chat"
    base_url: str = DEFAULT_BASE_URL
    temperature: float = 0.0

    # 单价（USD / 百万 token）。优先读环境变量，其次用上面的默认。
    input_cost_per_million: float = field(
        default_factory=lambda: _env_float(
            "DEEPSEEK_INPUT_COST_PER_MILLION", DEFAULT_INPUT_COST_PER_MILLION
        )
    )
    output_cost_per_million: float = field(
        default_factory=lambda: _env_float(
            "DEEPSEEK_OUTPUT_COST_PER_MILLION", DEFAULT_OUTPUT_COST_PER_MILLION
        )
    )

    # 注入点：测试用它替换真实 HTTP（`None` 时走 `urllib`）。
    # 类型写成 `Callable[..., Any]` 是为了不把这个"测试替身"写进对外契约。
    _post_chat: Callable[..., Any] | None = field(default=None, repr=False)

    def answer(self, item: Any, contexts: Sequence[ContextItem]) -> Answer:
        """把问题 + 装配后的上下文发给 DeepSeek，解析回答案与自述引用。

        ⚠️ `contexts` 是**装配之后**的那一份（`assemble` 过了 C-1/C-3/C-4），
        不是 `result.kept`。预算装不下的片模型没看见，也**不该**出现在提示词里——
        否则它"引"了一个它没真正读到的片，引用判据会把那记成真依据。
        """
        t0 = time.perf_counter()
        user_prompt = build_prompt(item.question, contexts)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            content, prompt_tokens, completion_tokens = self._call(messages)
        except _ChatError as exc:
            # ⚠️ 生成失败写 `error`，**不**用空文本冒充"答了但答错"——
            # 那是两件事：一个要修管线（这里），一个要改 prompt。
            return Answer(
                text="",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"deepseek: {exc}",
            )

        answer_text, citations = _parse_response(content, contexts)

        cost_usd = (
            prompt_tokens / 1_000_000 * self.input_cost_per_million
            + completion_tokens / 1_000_000 * self.output_cost_per_million
        )
        return Answer(
            text=answer_text,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            citations=citations,
        )

    # -- 真实 HTTP（或注入替身） ---------------------------------------

    def _call(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[str, int, int]:
        """返回 (内容文本, prompt_tokens, completion_tokens)。

        真实路径用标准库 `urllib` 打 DeepSeek 的 chat/completions。
        测试把 `self._post_chat` 设成替身，就走替身、不发网络请求。
        """
        if self._post_chat is not None:
            payload = self._post_chat(self, messages)
            return _unwrap(payload)

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise _ChatError("DEEPSEEK_API_KEY 未设置（真模型必须有 key）")

        body = json.dumps(
            {
                "model": self.model,
                "messages": list(messages),
                "temperature": self.temperature,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        url = self.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise _ChatError(f"HTTP {exc.code}: {detail[:200]}") from exc
        except urllib.error.URLError as exc:
            raise _ChatError(f"网络错误：{exc.reason}") from exc
        return _unwrap(data)


class _ChatError(Exception):
    """DeepSeek 调用层面的失败（网络 / 鉴权 / 解析不到内容）。"""


def _unwrap(data: dict[str, Any]) -> tuple[str, int, int]:
    """从 DeepSeek 的响应 JSON 里取出内容 + 用量。拿不到就报错。"""
    try:
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {}) or {}
        return (
            content,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise _ChatError(f"响应里没有内容/用量：{str(data)[:200]}") from exc


def build_prompt(question: str, contexts: Sequence[ContextItem]) -> str:
    """把问题 + 装配后的上下文拼成提示词。

    每个上下文用 `[[key]]` 显式标出自己的 chunk_id——模型**必须**回它实际引用的
    那些 key（纪律 1：引用是模型自述的，不是我们推的）。编号 `[n]` 只是人读方便，
    真正的 id 是 `[[...]]` 里的那段。
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


def _parse_response(
    content: str, contexts: Sequence[ContextItem]
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
    # 试 ```json ... ``` 围栏
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
    # 试第一个 { 到最后一个 }
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
    raw: Sequence[Any], contexts: Sequence[ContextItem]
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
        # 序号回退：必须是 1..len(contexts) 的正整数
        if s.isdigit():
            idx = int(s) - 1
            if 0 <= idx < len(keys):
                out.append(keys[idx])
                continue
        # 既不是合法 key 也不是有效序号：保留，交给引用判据判
        out.append(s)
    # 去重保序
    seen: set[str] = set()
    deduped: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return tuple(deduped)
