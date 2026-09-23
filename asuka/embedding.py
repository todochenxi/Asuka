"""Embedding：把 chunk 变成向量。默认 **BAAI/bge-m3**。

--------------------------------------------------------------------------
为什么是 bge-m3

    · 1024 维，中英双语（我们的语料是英文技术文档，但问题是中文/英文混着问的）
    · 8k 上下文 —— 比我们的 chunk（≤1000 字符）宽裕得多
    · 社区 API 普遍以 OpenAI 兼容形式提供（`/v1/embeddings`，model=`BAAI/bge-m3`）
    · 它原生是 dense + sparse + colbert 三头；**本 MVP 只用 dense 那一头**，
      sparse 那一头留给后面的 BM25/混合检索（见 `vectorstore` 的说明）

--------------------------------------------------------------------------
一条硬规矩：embedding 必须**自述身份**，且不允许静默换人

向量库里躺着一批 1024 维的向量，此刻换一个模型来查询 ——
**维度一样，语义完全不同，检索结果全是垃圾，而且一声不响。**
这比"维度不匹配直接报错"危险得多：后者至少会炸。

所以每个 embedder 必须说出 `name` / `dim` / `semantic`，
`signature()` 把前两者拼成一个字符串；向量库把它写进集合的元数据，
查询时对不上就**拒绝**，而不是返回一堆看着像结果的噪声。

    `HashingEmbedder` 的 `semantic=False` 就是这个意思：
    它只保证"同样的输入给同样的输出"，**不保证语义**。
    它唯一的用途是让测试和离线冒烟能跑通 ——
    一旦有人拿它做真检索，`semantic=False` 就是那句必须被读到的告警。

--------------------------------------------------------------------------
配置（环境变量）

    ASUKA_EMBED_BASE_URL   OpenAI 兼容的 base（默认 https://api.siliconflow.cn/v1）
    ASUKA_EMBED_API_KEY    密钥（没配 → APIEmbedder 直接拒绝构造）
    ASUKA_EMBED_MODEL      默认 BAAI/bge-m3
    ASUKA_EMBED_DIM        默认 1024
    ASUKA_EMBED_BATCH      每批条数，默认 32
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------- 常量

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DIM = 1024
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_BATCH = 32

#: 重试这些 HTTP 状态（瞬时）；4xx 里只有 429 值得重试
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------- 接口


@dataclass(frozen=True)
class EmbedderInfo:
    """embedder 的身份。`semantic=False` 表示它**不承载语义**。"""

    name: str
    dim: int
    kind: str = "api"          # api / local / test
    semantic: bool = True

    @property
    def signature(self) -> str:
        """进向量库元数据的那个串。查询时必须对得上。"""
        return f"{self.name}@{self.dim}"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "dim": self.dim,
            "kind": self.kind,
            "semantic": self.semantic,
            "signature": self.signature,
        }


@runtime_checkable
class Embedder(Protocol):
    """embedder 的协议。"""

    @property
    def info(self) -> EmbedderInfo: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """embedding 调用失败。**不吞** —— 失败必须让上层看见。"""


# ---------------------------------------------------------------- API


@dataclass
class APIEmbedder:
    """OpenAI 兼容的 `/v1/embeddings`（默认 bge-m3）。

    · 分批（默认 32 条/批），保持**输入顺序**
    · 瞬时错误（429/5xx/网络）指数退避重试；永久错误（400/401/404）立即失败
    · 每条向量校验维度 —— 服务端悄悄换了模型这件事，必须在这里被抓住
    """

    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    batch_size: int = DEFAULT_BATCH
    timeout: float = 60.0
    max_retries: int = 4
    kind: str = "api"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.api_key:
            raise EmbeddingError(
                "APIEmbedder 需要 api_key：设 ASUKA_EMBED_API_KEY（或用 --offline 走本地/测试 embedder）"
            )

    @property
    def info(self) -> EmbedderInfo:
        return EmbedderInfo(name=self.model, dim=self.dim, kind=self.kind, semantic=True)

    # ------------------------------------------------------------ 单批

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        body = json.dumps(
            {"model": self.model, "input": list(texts), "encoding_format": "float"},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                if exc.code not in _RETRY_STATUS:
                    raise EmbeddingError(
                        f"embeddings HTTP {exc.code}（不重试）: {detail}"
                    ) from exc
                last = EmbeddingError(f"embeddings HTTP {exc.code}: {detail}")
            except Exception as exc:  # noqa: BLE001 - 网络类，值得重试
                last = exc
            if attempt < self.max_retries:
                time.sleep(min(2.0**attempt, 8.0))
        else:
            raise EmbeddingError(f"embeddings 重试 {self.max_retries} 次仍失败: {last}")

        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingError(
                f"embeddings 返回条数不对：期望 {len(texts)}，得到 "
                f"{len(rows) if isinstance(rows, list) else type(rows).__name__}"
            )
        # 服务端**不保证**顺序 —— 按 index 排回去，别信它的数组位置
        ordered = sorted(rows, key=lambda r: int(r.get("index", 0)))
        out: list[list[float]] = []
        for i, row in enumerate(ordered):
            vec = row.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise EmbeddingError(f"第 {i} 条没有 embedding")
            if len(vec) != self.dim:
                raise EmbeddingError(
                    f"维度不符：模型 {self.model} 声称 {self.dim} 维，返回 {len(vec)} 维。"
                    f"（服务端换了模型？请核对 ASUKA_EMBED_DIM 与 ASUKA_EMBED_MODEL）"
                )
            out.append([float(x) for x in vec])
        return out

    # ------------------------------------------------------------ 批量

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        out: list[list[float]] = []
        for start in range(0, len(items), self.batch_size):
            out.extend(self._post(items[start : start + self.batch_size]))
        return out


# ---------------------------------------------------------------- 本地


@dataclass
class LocalBgeM3Embedder:
    """本地跑 bge-m3（需要 `sentence-transformers` + 模型权重）。

    存在的意义是**可复现**：API 会变（限流、下线、悄悄换模型），
    而基准实验要能重跑。

    `model_path` 指向本地权重目录（`ASUKA_EMBED_MODEL_PATH`）。
    ⚠️ **它不影响 `signature`** —— 身份是 `BAAI/bge-m3@1024`，不是"权重放在哪"。
    否则把权重从 A 目录挪到 B 目录，索引就会被判成"换过模型"而拒绝查询，
    这是把**部署细节**混进了**语义身份**。
    """

    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    device: str = ""
    batch_size: int = DEFAULT_BATCH
    kind: str = "local"
    #: 本地权重目录；空则交给 sentence-transformers 去 HF 上找
    model_path: str = ""
    #: 模型缓存。**不进比较、不进 repr** —— 它是实现细节，不是身份的一部分。
    _model: object | None = field(default=None, repr=False, compare=False)

    @property
    def info(self) -> EmbedderInfo:
        return EmbedderInfo(name=self.model, dim=self.dim, kind=self.kind, semantic=True)

    @property
    def source(self) -> str:
        return self.model_path or self.model

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(
                    "本地 bge-m3 需要 sentence-transformers：\n"
                    "  pip install sentence-transformers\n"
                    "  （第一次运行会下载 BAAI/bge-m3 权重，约 2.2GB）"
                ) from exc
            kwargs: dict[str, Any] = {"device": self.device} if self.device else {}
            path = self.model_path
            if path and not Path(path).exists():
                raise EmbeddingError(
                    f"ASUKA_EMBED_MODEL_PATH 指向的目录不存在：{path}\n"
                    f"  需要里面有 config.json / tokenizer.json / pytorch_model.bin 等"
                )
            self._model = SentenceTransformer(path or self.model, **kwargs)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        model = self._load()
        vecs = model.encode(  # type: ignore[attr-defined]
            items,
            normalize_embeddings=True,  # 单位向量 ⇒ Cosine 与 Dot 等价，分数可解释
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        out = [[float(x) for x in v] for v in vecs]
        for v in out:
            if len(v) != self.dim:
                raise EmbeddingError(
                    f"维度不符：{self.model} 声称 {self.dim} 维，得到 {len(v)} 维"
                    f"（请核对 ASUKA_EMBED_DIM）"
                )
        return out


# ---------------------------------------------------------------- 测试


@dataclass
class HashingEmbedder:
    """**不承载语义**的确定性 embedder —— 只给测试和离线冒烟用。

    `semantic=False` 不是注释，是**被机器读的**：向量库会把它写进集合元数据，
    检索结果里也会带出来。任何拿它当"真检索"的人都会看到这句话。
    """

    dim: int = 256
    name: str = "asuka-hashing-test"
    kind: str = "test"

    @property
    def info(self) -> EmbedderInfo:
        return EmbedderInfo(name=self.name, dim=self.dim, kind=self.kind, semantic=False)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            buckets = [0.0] * self.dim
            for token in _tokens(text):
                h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                buckets[idx] += sign
            norm = math.sqrt(sum(x * x for x in buckets)) or 1.0
            out.append([x / norm for x in buckets])
        return out


def _tokens(text: str) -> list[str]:
    buf: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            buf.append("".join(cur))
            cur = []
    if cur:
        buf.append("".join(cur))
    return buf or ["∅"]


# ---------------------------------------------------------------- 工厂


def build_embedder(
    kind: str = "auto", *, model: str = "", dim: int = 0, model_path: str = ""
) -> Embedder:
    """按环境变量/显式参数造一个 embedder。

    `auto`：有 API key → API；没有 → 报错（**不静默降级到 hashing**）。
    """
    env = os.environ
    model = model or env.get("ASUKA_EMBED_MODEL", DEFAULT_MODEL)
    dim = dim or int(env.get("ASUKA_EMBED_DIM", DEFAULT_DIM))
    base_url = env.get("ASUKA_EMBED_BASE_URL", DEFAULT_BASE_URL)
    api_key = env.get("ASUKA_EMBED_API_KEY", "")
    batch = int(env.get("ASUKA_EMBED_BATCH", DEFAULT_BATCH))
    model_path = model_path or env.get("ASUKA_EMBED_MODEL_PATH", "")

    if kind == "hashing":
        return HashingEmbedder()
    if kind == "local":
        return LocalBgeM3Embedder(
            model=model, dim=dim, batch_size=batch, model_path=model_path
        )
    if kind == "api":
        return APIEmbedder(
            model=model, dim=dim, base_url=base_url, api_key=api_key, batch_size=batch
        )
    if kind != "auto":
        raise ValueError(f"unknown embedder kind: {kind!r} (api/local/hashing/auto)")
    if not api_key:
        raise EmbeddingError(
            "没有 ASUKA_EMBED_API_KEY，无法用 API embedder。\n"
            "  设它：set ASUKA_EMBED_API_KEY=sk-...   （Windows）\n"
            "  或显式选本地权重：--embedder local"
            "（配 ASUKA_EMBED_MODEL_PATH，见 asuka/models/README.md）\n"
            "  或显式选测试：--embedder hashing\n"
            "  ⚠️ 不会静默降级到 hashing —— 那会让检索结果看着像真的。"
        )
    return APIEmbedder(
        model=model, dim=dim, base_url=base_url, api_key=api_key, batch_size=batch
    )


# ---------------------------------------------------------------- 自检


#: 自检用的四句话：前两句**说的是同一件事**（不同措辞），后两句完全无关。
SELFTEST_SENTENCES: tuple[str, ...] = (
    "Redis EXPIRE sets a timeout on a key.",
    "How do I make a key expire in Redis?",
    "Kubernetes schedules pods onto nodes.",
    "What is the capital of France?",
)

#: 相关句对与无关句对的余弦差下限。
SELFTEST_MIN_GAP = 0.05


def selftest(embedder: Embedder, *, min_gap: float = SELFTEST_MIN_GAP) -> list[str]:
    """返回**问题清单**（空列表 = 通过）。**不抛异常** —— 它要能一次报出全部问题。

    两道判据，性质不同，都要：

    **一、自述**（确定性）：`info.semantic` 必须为 `True`。
    `HashingEmbedder` 这类东西自己就说了"我不承载语义"，不必实测就知道不能用。
    实测它对哈希噪声做判断，结果是**时红时绿** —— 那种判据等于没有。

    **二、实测**（抓说谎的）：相关句对的相似度必须比无关句对高出 `min_gap`。
    自述是**声明**，这里是**事实**。一个把一切都映射到同一点的 embedder
    同样能产出"1024 维单位向量"，维度检查完全通得过，但相似度是常数。
    ⇒ **维度对 ≠ 语义对。**

    ⚠️ 判据二不是万能的：一个"声明 semantic=True 但实际是哈希"的 embedder
    有相当概率蒙混过关（单次余弦差是随机量）。真正的硬门是
    `vectorstore.assert_embedder_matches` —— 它读的是**建索引时落盘的声明**，
    不依赖查询方是否诚实。
    """
    problems: list[str] = []

    if not embedder.info.semantic:
        problems.append(
            f"该 embedder 自述 semantic=False（kind={embedder.info.kind}）—— "
            "它建出来的索引只能冒烟，不能用来评检索质量"
        )

    try:
        vecs = embedder.embed(list(SELFTEST_SENTENCES))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"embed 失败：{type(exc).__name__}: {exc}")
        return problems

    if len(vecs) != len(SELFTEST_SENTENCES):
        problems.append(f"要了 {len(SELFTEST_SENTENCES)} 条，回来 {len(vecs)} 条")
        return problems

    bad_dim = [
        f"第 {i} 条维度 {len(v)} ≠ 自述 {embedder.info.dim}"
        for i, v in enumerate(vecs)
        if len(v) != embedder.info.dim
    ]
    if bad_dim:
        return problems + bad_dim

    def _cos(x: Sequence[float], y: Sequence[float]) -> float:
        dot = sum(a * b for a, b in zip(x, y))
        nx = math.sqrt(sum(a * a for a in x)) or 1.0
        ny = math.sqrt(sum(b * b for b in y)) or 1.0
        return dot / (nx * ny)

    # 相关：0↔1（同义不同措辞）。无关：0/1 各自与 2、3 配对后取均值 ——
    # 取均值是为了压方差，单对余弦差容易靠运气过线。
    related = _cos(vecs[0], vecs[1])
    unrelated = (
        _cos(vecs[0], vecs[2])
        + _cos(vecs[0], vecs[3])
        + _cos(vecs[1], vecs[2])
        + _cos(vecs[1], vecs[3])
    ) / 4.0

    if related - unrelated < min_gap:
        problems.append(
            f"相似度不分辨语义：相关句对 cos={related:.4f}，"
            f"无关句对 cos={unrelated:.4f}，差 {related - unrelated:.4f} < {min_gap}"
            f"（该 embedder 自述 semantic={embedder.info.semantic}）"
        )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m asuka.embedding --embedder local --model-path ...`

    在**建索引之前**跑它。理由：428 条 chunk 的索引要跑几分钟，
    而"权重坏了/加载成了别的模型"这件事，自检 3 秒就能说出来。
    """
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="embedder 自检（不需要 Qdrant）")
    parser.add_argument(
        "--embedder",
        default="auto",
        choices=["auto", "api", "local", "hashing"],
        help="`auto` 需要 ASUKA_EMBED_API_KEY；本地权重用 `local`",
    )
    parser.add_argument(
        "--model-path",
        default="",
        help="本地模型权重目录（不填则读 ASUKA_EMBED_MODEL_PATH）",
    )
    parser.add_argument(
        "--min-gap", type=float, default=SELFTEST_MIN_GAP, help="相关/无关句对的余弦差下限"
    )
    args = parser.parse_args(argv)

    try:
        embedder = build_embedder(args.embedder, model_path=args.model_path)
    except EmbeddingError as exc:
        print(f"\n! {exc}", file=sys.stderr)
        return 2

    source = getattr(embedder, "source", "")
    print(f"embedder = {embedder.info.signature}")
    print(f"  kind={embedder.info.kind}  semantic={embedder.info.semantic}")
    if source:
        print(f"  source={source}")

    problems = selftest(embedder, min_gap=args.min_gap)
    if not problems:
        print("✓ 自检通过：维度一致，且相关句对显著高于无关句对")
        return 0

    print("✗ 自检未通过：")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

