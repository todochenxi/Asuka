"""red94：DeepSeek 真模型答案器（#116）的变红验证。

锚点守的是"模型侧那几个数**真的测了**，不是填 0 / 静默降级"：

  N1  生成失败时 `text` 必须是空串（**不**用错误文案冒充"答了但答错"）
  N2  cost 公式：输入/输出单价**别乘反**
  N3  序号引用 `[n]` 只在范围内才映射成真实 key（越界/乱写保留原样，交引用判据判）
  N4  解析不到 JSON ⇒ 引用 `None`（不可测、点名），**不**静默当成 `()`（召回 0）
  N5  归一化末尾 `return tuple(deduped)`（空列表 ⇒ `()`），**不**返回 `None`
  N6  提示词把每个上下文的 `[[key]]` 显式标出来（模型自述引用的前提）
  N7  CLI 缺 key 早退的提示必须含 `DEEPSEEK_API_KEY`（别静默当免费）

骨架见 `redkit.py`（锚点唯一性 / 残留防护 / 信号处理）。
"""
from pathlib import Path

from redkit import Mutation, run_red

DS = Path("asuka/deepseek.py")
AN = Path("asuka/answers.py")

MUTATIONS = [
    Mutation(
        "N1",
        "生成失败却用错误文案冒充'答了但答错'：一个要修管线，一个要改 prompt，是两件事",
        DS,
        '            return Answer(\n                text="",\n                latency_ms=(time.perf_counter() - t0) * 1000.0,',
        '            return Answer(\n                text="boom",\n                latency_ms=(time.perf_counter() - t0) * 1000.0,',
    ),
    Mutation(
        "N2",
        "cost 把输入单价乘到 output、输出单价乘到 input —— 单价一变，报告上的钱就是错的",
        DS,
        "        cost_usd = (\n"
        "            prompt_tokens / 1_000_000 * self.input_cost_per_million\n"
        "            + completion_tokens / 1_000_000 * self.output_cost_per_million\n"
        "        )",
        "        cost_usd = (\n"
        "            prompt_tokens / 1_000_000 * self.output_cost_per_million\n"
        "            + completion_tokens / 1_000_000 * self.input_cost_per_million\n"
        "        )",
    ),
    Mutation(
        "N3",
        "序号引用越界也当成有效 key：模型回 '[9]' 被当成第 9 片 → 编造引用被放过",
        DS,
        "            idx = int(s) - 1",
        "            idx = int(s) + 1",
    ),
    Mutation(
        "N4",
        "解析不到 JSON 却返回 ()：'没测'被读成'没引任何来源'，引用召回静默成了 0",
        DS,
        "        return text, None",
        "        return text, ()",
    ),
    Mutation(
        "N5",
        "归一化末尾返回 None 而非空元组：'明确没引'退化成'没自述'，引用分母被偷改",
        DS,
        "    return tuple(deduped)",
        "    return None",
    ),
    Mutation(
        "N6",
        "提示词不标 [[key]]：模型没法自述引用，引用判据的前提被悄悄拆掉",
        DS,
        '        blocks.append(f"[{n}] [[{c.key}]]\\n{c.text}")',
        '        blocks.append(f"[{n}] {c.text}")',
    ),
    Mutation(
        "N7",
        "缺 key 早退的提示不含 DEEPSEEK_API_KEY：读者不知道要设哪个环境变量",
        AN,
        '                "用 deepseek 作答需要 DEEPSEEK_API_KEY 环境变量（真模型必须有 key）"',
        '                "用 deepseek 作答需要 API key（真模型必须有 key）"',
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_red(
            "DeepSeek 真模型答案器（#116）",
            MUTATIONS,
            backup_dir=Path(".workbuddy-ai/tmp/red94.orig"),
        )
    )
