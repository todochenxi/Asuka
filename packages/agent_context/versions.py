"""Knowledge Versioning（M5，基线 §21 / §8.3）。

### 它补的是什么

§8.3 把 Knowledge 定义成"客观、**版本化**的资料"，并且明说
`PostgreSQL → Document Metadata / Version (durable truth)`。但到 M105 为止，
全仓没有任何"版本"这个概念：一次检索拿到的片**说不出自己属于哪一版**，
也拦不住旧版本的片混进一次"锁定在某版"的检索。

M5 此前落了 Hybrid Search 与 Rerank（`retrieval.py`），版本化是那一层剩下的空步。

### 三条不变量

| # | 内容 |
|---|---|
| K-1 | **没有版本声明的片不许被当成锁定版本里的**。`Chunk.attributes` 里没有版本 = 拒（不做"兜底成当前版本"）|
| K-2 | 锁定检索**绝不**返回别的版本的片 —— 一旦发现就抛 `VersionMismatch`，不静默混入 |
| K-3 | 未锁定的检索解析到**当前**版本；没有当前版本 → 拒（不返回"哪个版本都行"的结果） |

### 为什么版本过滤也要跑两遍（与 C-9 同构）

C-9 说权限过滤要 push-down（交给 Retriever）+ verify（pipeline 兜底），
理由是"只靠 push-down = 把安全边界外包给存储层"。版本过滤的理由**完全相同**：

    push-down   把 `knowledge_version` 放进 query.filters，让底层别去别的版本里找
    verify      底层回来之后逐条核对声明 —— 换一个 Retriever 就没了 push-down

⚠️ 与权限的一处**关键区别**：权限不匹配是**正常**的（用户本来就没权限），
所以被拒的片进 `RetrievalResult.denied`；版本不匹配是**不一致**（索引坏了 /
stamper 漏了），所以这里**抛异常**——一条静默混入的旧版本片，比一次响亮的失败危险。

### 版本从哪来

片在**入库时**由文档管线打上 `attributes["knowledge_version"]`（原始文档带版本，
见 §8.3）。本模块不做打标，只做"锁版本 → 检索 → 核对"。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Sequence

from .retrieval import KNOWLEDGE_VERSION_KEY, Chunk, RetrievalQuery, Retriever


class UnknownVersion(Exception):
    """登记处里没有这个版本。"""


class NoCurrentVersion(Exception):
    """登记处里一个版本都没有 —— 没有"当前版本"可以解析。"""


class VersionMismatch(Exception):
    """检索回来的片声明的版本与锁定的版本不一致（K-1 / K-2）。"""


@dataclass(frozen=True)
class KnowledgeVersion:
    """一个知识版本。

    `digest` 是内容指纹（例如全量片 id 的哈希）—— 版本号可以重打，
    指纹不能。两者都在，是为了回答"这个版本真的是我上次跑的那个吗"。
    """

    version_id: str
    created_at: datetime
    source: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.version_id:
            raise ValueError("KnowledgeVersion.version_id is required")


@dataclass
class KnowledgeVersionRegistry:
    """`version_id → KnowledgeVersion` + "哪个是当前版本"。

    第一次注册自动成为当前（同 `ToolRegistry` 的默认版本策略）；
    之后换当前必须 `set_current` 明说 —— 让"现在检索打到哪一版"在代码里留痕。
    """

    _versions: dict[str, KnowledgeVersion] = field(default_factory=dict)
    _current: str = ""

    def register(self, version: KnowledgeVersion, *, make_current: bool = False) -> None:
        if version.version_id in self._versions:
            raise ValueError(f"knowledge version {version.version_id!r} already registered")
        self._versions[version.version_id] = version
        if make_current or not self._current:
            self._current = version.version_id

    def set_current(self, version_id: str) -> None:
        if version_id not in self._versions:
            raise UnknownVersion(f"no such knowledge version: {version_id!r}")
        self._current = version_id

    def resolve(self, version_id: str) -> KnowledgeVersion:
        version = self._versions.get(version_id)
        if version is None:
            raise UnknownVersion(f"no such knowledge version: {version_id!r}")
        return version

    def current(self) -> KnowledgeVersion:
        if not self._current:
            raise NoCurrentVersion(
                "no knowledge version is registered; refusing to retrieve against "
                "an unversioned index (K-3)"
            )
        return self._versions[self._current]

    def versions(self) -> tuple[KnowledgeVersion, ...]:
        return tuple(self._versions[k] for k in sorted(self._versions))

    @property
    def current_id(self) -> str:
        return self._current


@dataclass(frozen=True)
class VersionedRetriever:
    """把一次检索锁到一个知识版本上（实现 `Retriever`，与 pipeline 无缝组合）。

    `version_id` 为空 = 用登记的**当前**版本（K-3）。
    """

    retriever: Retriever
    registry: KnowledgeVersionRegistry
    version_id: str = ""
    #: 片自述版本的字段名（默认 `knowledge_version`）。
    version_key: str = KNOWLEDGE_VERSION_KEY

    def resolved_version(self) -> KnowledgeVersion:
        if self.version_id:
            return self.registry.resolve(self.version_id)
        return self.registry.current()

    def search(self, query: RetrievalQuery) -> Sequence[Chunk]:
        version = self.resolved_version()
        # push-down（同 C-9）：让底层别去别的版本里找。
        pinned = replace(
            query, filters={**query.filters, self.version_key: version.version_id}
        )
        chunks = list(self.retriever.search(pinned))
        # verify（同 C-9 的兜底）：换一个 Retriever 就没了 push-down。
        out: list[Chunk] = []
        for chunk in chunks:
            declared = str(chunk.attributes.get(self.version_key) or "")
            if declared != version.version_id:
                raise VersionMismatch(
                    f"K-2: chunk {chunk.chunk_id!r} declares knowledge version "
                    f"{declared or '<none>'!r}, but this retrieval is pinned to "
                    f"{version.version_id!r}; a pinned retrieval must never mix "
                    f"versions"
                )
            out.append(chunk)
        return out


__all__ = [
    "KnowledgeVersion",
    "KnowledgeVersionRegistry",
    "NoCurrentVersion",
    "UnknownVersion",
    "VersionMismatch",
    "VersionedRetriever",
]
