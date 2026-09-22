"""幂等键的**唯一一处**定义（A-3 / B-7 · 空洞 223）。

--------------------------------------------------------------------------
为什么它要单独成一个模块

`start_run` 从 M18 起就吃 `Idempotency-Key`，`cancel_run` 到 M44 才有。
两条写路径、一套规矩：命名空间、请求指纹、"同一个键却换了请求体"怎么判。

分在两处写就是两份实现，而"幂等到底是什么意思"这种规矩，
两份实现的产物一定是**两份不同的答案**（B-7）——
例如一边拒绝复用、一边静默回放，于是"这个键能不能重发"
取决于客户端碰巧打在哪个端点上。

--------------------------------------------------------------------------
命名空间为什么必须分开

客户端的重试逻辑通常是一个键对应一次用户动作。而一个用户动作里
"开一条 Run"与"叫停一条 Run"是两次**不同**的写操作。

共用一个命名空间的后果不是报错，是**静默**：
`cancel` 会命中 `start` 留下的记录，于是它返回一份 `RunView`
而**根本没叫停** —— 界面上显示"停止成功"，Run 还在跑。
那是空洞 221 的形状，而且更难发现：报错至少有人来看一眼。

--------------------------------------------------------------------------
请求指纹为什么不能用内置 `hash()`

Python 对 `str` 的 `hash()` 带随机盐（`PYTHONHASHSEED`），
**重启之后同一个字符串的 hash 不一样**。

用它做指纹，产物是一个只在重启后才出现的假 422：
"同一个键 + 同样的请求体"被判成"同一个键 + 不同的请求体"。
这种 bug 的坏处在于它测不出来 —— 单测跑在同一个进程里，
hash 是稳定的，全绿；上了线一重启就开始拒绝合法的重试。

指纹必须**跨进程、跨重启稳定**，所以用 sha256。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .errors import Unprocessable

#: 两类写操作各占一个命名空间。见模块 docstring 第二段。
NAMESPACE_RUN = "run"
NAMESPACE_CANCEL = "cancel"

#: 信封的版本。将来改了信封的形状，老记录要能被认出来，而不是被当成新记录。
ENVELOPE_VERSION = 1


def scoped(key: str, *, namespace: str) -> str:
    """把客户端给的裸键放进命名空间。"""
    return f"{namespace}:{key}"


def fingerprint(parts: Mapping[str, Any]) -> str:
    """一次写请求的**稳定**指纹。

    `sort_keys` 是必须的：dict 的插入顺序不参与"这两次请求一样不一样"
    的判断，而 JSON 序列化默认按插入顺序输出。
    """
    blob = json.dumps(
        dict(parts),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,          # 非 JSON 类型（datetime / Enum）退成它的 str，不炸
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def envelope(*, fingerprint: str, answer: Mapping[str, Any]) -> dict[str, Any]:
    """存进 `IdempotencyStore` 的那个值。

    为什么存的是**答案**而不是"一个指针"：见 `service.py` 里 D-36 那段。
    """
    return {
        "v": ENVELOPE_VERSION,
        "fingerprint": fingerprint,
        "answer": dict(answer),
    }


def answer_of(cached: Mapping[str, Any]) -> Mapping[str, Any]:
    """从信封里取出第一次给出的答案。"""
    answer = cached.get("answer")
    if not isinstance(answer, Mapping):
        raise Unprocessable(
            "the idempotency record for this key does not carry an answer; "
            "it was written by a different version of the service, so the "
            "result of the first call cannot be replayed — refusing rather "
            "than running the write a second time",
            code="IDEMPOTENCY_RECORD_UNREADABLE",
        )
    return dict(answer)


def refuse_if_reused(
    cached: Mapping[str, Any],
    *,
    key: str,
    namespace: str,
    fingerprint: str,
) -> None:
    """同一个键 + **不同的请求体** ⟹ 点名拒绝。

    --------------------------------------------------------------------------
    为什么不能"静默回放第一次的答案"

    客户端拿着键 K 重发了一次**内容不同的**请求。两种处理：

      · 静默回放第一次的答案 ⟹ 客户端以为自己的第二次请求生效了，
        而它请求的那件事**从来没发生**。这是一个谎言，而且没有报错。
      · 拒绝 ⟹ 客户端立刻知道"这个键已经用过了，换个键"。

    A-3 的承诺是"重发不会做两遍"，不是"重发什么都能用同一个键"。

    --------------------------------------------------------------------------
    ️ 这一条 `start_run` 原本是缺的（M44 顺手补上）

    它只存了 `{"run_id": ...}`，于是同一个键换一个 `user_request`
    会静默返回第一条 Run —— 用户改了需求，拿到的还是旧 Run 的视图。
    """
    if cached.get("fingerprint") == fingerprint:
        return
    raise Unprocessable(
        f"idempotency key {key!r} was already used for a different request in "
        f"the {namespace!r} namespace; replaying the first answer would make "
        f"this request look like it happened when it did not — send a new key",
        code="IDEMPOTENCY_KEY_REUSED",
        idempotency_key=key,
        namespace=namespace,
    )


__all__ = [
    "ENVELOPE_VERSION",
    "NAMESPACE_CANCEL",
    "NAMESPACE_RUN",
    "answer_of",
    "envelope",
    "fingerprint",
    "refuse_if_reused",
    "scoped",
]
