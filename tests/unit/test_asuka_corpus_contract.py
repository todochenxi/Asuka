"""Asuka 语料层 / 向量层的契约。

--------------------------------------------------------------------------
这个文件锁的是**两个实证过的缺陷**，不是推演出来的担心

**一、模板样板块 `## Code Examples Legend`（实证：expire.md）**

redis.io 有 14/20 份文档带这一节，且**逐字相同**。它的位置是：

    # EXPIRE
    ## Code Examples Legend          ← 页面模板（官方 metadata 的 tableOfContents
    <12 条语言 bullet>                   里**没有**它，所以它不是文档结构）
    ---                              ← 模板的分隔线
    Set a timeout on `key`...        ← 这份文档**真正的概述**，而且**没有标题**
    ## Required arguments

切分器按标题切，把这段无标题概述归给了上一个标题 ⇒
citation 变成 `Redis · EXPIRE · Code Examples Legend`，
而内容是 "Set a timeout on `key`..." —— **citation 在说谎**，
而 Citation 指标正是拿 citation 判分的。

⚠️ 这不是 LangChain 的 bug：它按标题切、把标题后的内容归给该标题，**行为正确**。
错的是这份模板结构，所以修在**切分之前**（归一化），而不是切分之后去猜。

**二、`##### <语言名>` 对切分器不可见（实证：35% 的 Examples chunk 混语言）**

代码示例是 codetabs：同一操作 × 14 种语言，用 `##### Go` 这样的 h5 划 tab。
`headers_to_split_on` 只有 h1/h2/h3 时，h5 不可见 ⇒ `RecursiveCharacterTextSplitter`
按字符切到哪算哪，实测 123/355 = **35% 的 Examples chunk 混了 ≥2 种语言**
（如 `redis:set:008 = ['C#','Go']`，一片里既有 C# 尾巴又有 Go 开头）。
citation 只能说 `Examples`，说不出"这是 Go 的例子"。

⇒ 语料自己声明 `headers`（`registry/redis.json`），库的默认不动。
A/B 实测：混语言 35% → **0%**，oversized 11 → 9，代价 chunks 355 → 408。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from asuka.corpus import (
    MIN_CHARS,
    Unit,
    chunk_document,
    prepare_document,
    strip_template_legend,
)
from asuka.embedding import (
    SELFTEST_SENTENCES,
    EmbedderInfo,
    EmbeddingError,
    HashingEmbedder,
    build_embedder,
    selftest,
)
from asuka.splitters import DEFAULT_HEADERS, HEADER_PRESETS, headers_for
from asuka.vectorstore import (
    assert_embedder_matches,
    collection_name,
    corpus_fingerprint,
    point_id,
)

_REPO = Path(__file__).resolve().parents[2]
_RAW_REDIS = _REPO / "asuka" / "raw" / "redis"

#: 复刻 expire.md 的真实形状（含模板图例 + 无标题概述 + 代码围栏里的 `#` 注释）
_SYNTHETIC = """# EXPIRE

```json metadata
{"syntax_fmt": "EXPIRE key seconds", "complexity": "O(1)", "group": "generic"}
```

## Code Examples Legend

The code examples below show how to perform the same operations in different programming languages and client libraries:

- **Redis CLI**: Command-line interface for Redis
- **Python**: redis-py client

Each code example demonstrates the same basic operation across different languages.

---

Set a timeout on `key`.
After the timeout has expired, the key will automatically be deleted.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

## Examples

```bash
# this is a shell comment, NOT a heading
EXPIRE mykey 10
```
"""

_UNIT = Unit(topic="redis", unit_id="expire", title="EXPIRE", group="generic")


def _chunks(markdown: str = _SYNTHETIC):
    return chunk_document(
        markdown,
        _UNIT,
        display_name="Redis",
        url="https://redis.io/docs/latest/commands/expire/",
        backend="stdlib",  # 零依赖：单测用 managed python，不许引第三方
    )


# ====================================================================== 一


class TestTemplateLegendRemoved(unittest.TestCase):
    """一、模板样板块必须在**切分之前**被移除，且移除量要记账。"""

    def test_removed_through_separator(self) -> None:
        body, removed = strip_template_legend(_SYNTHETIC)
        self.assertGreater(removed, 0)
        self.assertNotIn("Code Examples Legend", body)
        self.assertNotIn("redis-py client", body, "12 条语言 bullet 也该一起走")
        # 模板的分隔线属于模板，一并去掉
        self.assertNotIn("\n---\n", body)

    def test_overview_survives(self) -> None:
        """被移除的是模板，不是内容 —— 概述必须原样留下。"""
        body, _ = strip_template_legend(_SYNTHETIC)
        self.assertIn("Set a timeout on `key`.", body)
        self.assertIn("## Required arguments", body)

    def test_noop_when_absent(self) -> None:
        plain = "# GET\n\nReturns the string value of a key.\n\n## Examples\n\n```bash\nGET k\n```\n"
        body, removed = strip_template_legend(plain)
        self.assertEqual(removed, 0)
        self.assertEqual(body, plain)

    def test_falls_back_to_next_heading_without_separator(self) -> None:
        """没有 `---` 时不能把整篇吞掉 —— 切到下一个标题为止。"""
        doc = "# X\n\n## Code Examples Legend\n\nboilerplate\n\n## Real section\n\ncontent\n"
        body, removed = strip_template_legend(doc)
        self.assertGreater(removed, 0)
        self.assertIn("## Real section", body)
        self.assertIn("content", body)
        self.assertNotIn("boilerplate", body)

    def test_removed_chars_recorded_on_every_chunk(self) -> None:
        """账本要能**不回看 manifest** 就读出来（审计自包含）。"""
        _, _, removed = prepare_document(_SYNTHETIC)
        self.assertGreater(removed, 0)
        for c in _chunks():
            self.assertEqual(c.attributes["legend_removed_chars"], removed)


class TestOverviewAttribution(unittest.TestCase):
    """一（续）：无标题概述必须归到 overview，而不是归给模板标题。"""

    def test_overview_labeled_overview(self) -> None:
        sections = [c.attributes["section"] for c in _chunks()]
        self.assertIn("overview", sections)
        self.assertNotIn("Code Examples Legend", sections)

    def test_no_legend_text_anywhere(self) -> None:
        for c in _chunks():
            self.assertNotIn("Code Examples Legend", c.text)

    def test_citation_shape(self) -> None:
        """citation 形如 `Redis · EXPIRE · overview` —— 它是 C-10 的载体。"""
        by_section = {c.attributes["section"]: c for c in _chunks()}
        self.assertEqual(by_section["overview"].citation, "Redis · EXPIRE · overview")
        self.assertEqual(
            by_section["Required arguments"].citation,
            "Redis · EXPIRE · Required arguments",
        )

    def test_spec_chunk_present(self) -> None:
        """metadata 块被取出来渲染成独立的 spec chunk，不污染正文、也不丢。"""
        spec = [c for c in _chunks() if c.attributes["section"] == "command spec"]
        self.assertEqual(len(spec), 1)
        self.assertIn("EXPIRE key seconds", spec[0].text)
        self.assertIn("table", spec[0].attributes["kinds"])
        # 正文里不该再有原始 JSON
        self.assertNotIn('"syntax_fmt"', "".join(c.text for c in _chunks()))


class TestAtomsNeverSplit(unittest.TestCase):
    """围栏与表格是**原子块**：跨切即废（社区结论）。"""

    def test_fences_are_balanced(self) -> None:
        for c in _chunks():
            self.assertEqual(
                c.text.count("```") % 2,
                0,
                f"{c.chunk_id} 的代码围栏不成对 —— 说明被切开了",
            )

    def test_shell_comment_not_treated_as_heading(self) -> None:
        """`# this is a shell comment` 在围栏内，不许被当成 h1 切开。"""
        for c in _chunks():
            if "this is a shell comment" in c.text:
                self.assertEqual(c.attributes["section"], "Examples")
                return
        self.fail("没找到含 shell 注释的 chunk")

    def test_min_chars_respected(self) -> None:
        for c in _chunks():
            self.assertGreaterEqual(c.attributes["chars"], MIN_CHARS)


@unittest.skipUnless(_RAW_REDIS.exists(), "没有 asuka/raw/redis（未抓取）")
class TestRealRedisCorpus(unittest.TestCase):
    """在**真实抓下来的**文档上复验（合成 fixture 可能掩盖真实形状）。"""

    def _real(self, unit_id: str):
        text = (_RAW_REDIS / f"{unit_id}.md").read_text(encoding="utf-8")
        unit = Unit(topic="redis", unit_id=unit_id, title=unit_id.upper())
        return chunk_document(
            text,
            unit,
            display_name="Redis",
            url=f"https://redis.io/docs/latest/commands/{unit_id}/",
            backend="stdlib",
        )

    def test_expire_overview_is_not_mislabeled(self) -> None:
        """这正是当初实测到的那个错标：`Redis · EXPIRE · Code Examples Legend`。"""
        chunks = self._real("expire")
        sections = {c.attributes["section"] for c in chunks}
        self.assertNotIn("Code Examples Legend", sections)
        self.assertIn("overview", sections)
        overview = [c for c in chunks if c.attributes["section"] == "overview"]
        self.assertTrue(
            any("Set a timeout on" in c.text for c in overview),
            "EXPIRE 的概述没落在 overview 里",
        )

    def test_all_units_have_no_legend_residue(self) -> None:
        for path in sorted(_RAW_REDIS.glob("*.md")):
            with self.subTest(unit=path.stem):
                for c in self._real(path.stem):
                    self.assertNotIn("Code Examples Legend", c.text)

    def test_all_units_have_balanced_fences(self) -> None:
        for path in sorted(_RAW_REDIS.glob("*.md")):
            with self.subTest(unit=path.stem):
                for c in self._real(path.stem):
                    self.assertEqual(c.text.count("```") % 2, 0, c.chunk_id)

    def test_every_unit_has_an_overview(self) -> None:
        for path in sorted(_RAW_REDIS.glob("*.md")):
            with self.subTest(unit=path.stem):
                sections = {c.attributes["section"] for c in self._real(path.stem)}
                self.assertIn("overview", sections)


# ====================================================================== 二


class TestHeaderPresets(unittest.TestCase):
    """二、标题层级是**语料的属性**，库的默认不动。"""

    def test_library_default_is_the_community_spec(self) -> None:
        self.assertEqual(DEFAULT_HEADERS, "spec")
        self.assertEqual(
            HEADER_PRESETS["spec"], (("#", "h1"), ("##", "h2"), ("###", "h3"))
        )

    def test_deep_adds_h4_h5(self) -> None:
        self.assertEqual(
            HEADER_PRESETS["deep"],
            (("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4"), ("#####", "h5")),
        )

    def test_explicit_levels_accepted(self) -> None:
        self.assertEqual(
            headers_for("h1,h2"), (("#", "h1"), ("##", "h2"))
        )

    def test_unknown_preset_rejected(self) -> None:
        with self.assertRaises(ValueError):
            headers_for("nope")

    def test_redis_declares_deep(self) -> None:
        """redis.io 的 `##### <语言名>` 必须被尊重，否则 35% 的片混语言。"""
        import json

        raw = json.loads(
            (_REPO / "asuka" / "registry" / "redis.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["headers"], "deep")


# ====================================================================== 三


class TestEmbedderIdentity(unittest.TestCase):
    """三、embedding 必须自述身份，且**不许静默降级**。"""

    def test_hashing_is_deterministic_and_unit_norm(self) -> None:
        emb = HashingEmbedder()
        a = emb.embed(["EXPIRE key seconds"])[0]
        b = emb.embed(["EXPIRE key seconds"])[0]
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a) ** 0.5, 1.0, places=6)

    def test_hashing_declares_itself_non_semantic(self) -> None:
        """`semantic=False` 不是注释，是**被机器读的**。"""
        self.assertFalse(HashingEmbedder().info.semantic)

    def test_local_bge_m3_identity(self) -> None:
        from asuka.embedding import LocalBgeM3Embedder

        info = LocalBgeM3Embedder().info
        self.assertEqual(info.signature, "BAAI/bge-m3@1024")
        self.assertTrue(info.semantic)
        self.assertEqual(info.kind, "local")

    def test_auto_without_key_refuses_instead_of_degrading(self) -> None:
        """没有 key 时必须**拒绝** —— 静默降级到 hashing 会让垃圾看起来像结果。"""
        with mock.patch.dict(os.environ, {"ASUKA_EMBED_API_KEY": ""}):
            with self.assertRaises(EmbeddingError) as ctx:
                build_embedder("auto")
        self.assertIn("不会静默降级", str(ctx.exception))

    def test_api_embedder_requires_key(self) -> None:
        from asuka.embedding import APIEmbedder

        with self.assertRaises(EmbeddingError):
            APIEmbedder(api_key="")

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_embedder("nope")


class TestVectorStoreIdentity(unittest.TestCase):
    """三（续）：换模型 = 换一格 collection；对不上就拒绝。"""

    def test_collection_name_carries_model_and_dim(self) -> None:
        self.assertEqual(
            collection_name("redis", EmbedderInfo("BAAI/bge-m3", 1024)),
            "asuka_redis_baai-bge-m3_1024",
        )

    def test_two_models_get_two_collections(self) -> None:
        a = collection_name("redis", EmbedderInfo("BAAI/bge-m3", 1024))
        b = collection_name("redis", EmbedderInfo("BAAI/bge-large-en-v1.5", 1024))
        self.assertNotEqual(a, b, "同维度不同模型必须分格，否则会悄悄混在一起")

    def test_point_id_is_deterministic(self) -> None:
        self.assertEqual(point_id("redis:set:007"), point_id("redis:set:007"))
        self.assertNotEqual(point_id("redis:set:007"), point_id("redis:set:008"))

    def test_assert_matches_refuses_other_model(self) -> None:
        man = {"collection": "c", "embedder": EmbedderInfo("BAAI/bge-m3", 1024).as_dict()}
        with self.assertRaises(Exception) as ctx:
            assert_embedder_matches(man, EmbedderInfo("other-model", 1024))
        self.assertIn("other-model@1024", str(ctx.exception))

    def test_assert_matches_refuses_non_semantic_index(self) -> None:
        """签名对得上也不行 —— 不承载语义的索引不能当检索质量。"""
        man = {"collection": "c", "embedder": HashingEmbedder().info.as_dict()}
        with self.assertRaises(Exception) as ctx:
            assert_embedder_matches(man, HashingEmbedder().info)
        self.assertIn("不承载语义", str(ctx.exception))

    def test_refusal_tells_you_both_ways_to_opt_in(self) -> None:
        """拒绝信息要能**照着做**：Python 调用方与 CLI 用户看到的不是同一个名字。"""
        man = {"collection": "c", "embedder": HashingEmbedder().info.as_dict()}
        with self.assertRaises(Exception) as ctx:
            assert_embedder_matches(man, HashingEmbedder().info)
        msg = str(ctx.exception)
        self.assertIn("allow_non_semantic=True", msg, "Python 调用方要看到 kwarg")
        self.assertIn("--allow-non-semantic", msg, "CLI 用户要看到 flag")

    def test_non_semantic_needs_an_explicit_opt_in(self) -> None:
        """冒烟要能跑，但那个选择必须**出现在代码里**，不能是默认。"""
        man = {"collection": "c", "embedder": HashingEmbedder().info.as_dict()}
        self.assertEqual(
            assert_embedder_matches(
                man, HashingEmbedder().info, allow_non_semantic=True
            ),
            "c",
        )

    def test_assert_matches_allows_same_signature(self) -> None:
        info = EmbedderInfo("BAAI/bge-m3", 1024)
        self.assertEqual(
            assert_embedder_matches({"collection": "c", "embedder": info.as_dict()}, info),
            "c",
        )

    def test_fingerprint_tracks_text_changes(self) -> None:
        """语料变了指纹就要变 —— 否则"向量比语料旧"这件事读不出来。"""
        chunks = list(_chunks())
        before = corpus_fingerprint(chunks)
        self.assertEqual(before, corpus_fingerprint(list(chunks)), "同样输入必须同指纹")
        mutated = list(chunks)
        object.__setattr__(mutated[0], "text", mutated[0].text + " 改一个字")
        self.assertNotEqual(before, corpus_fingerprint(mutated))


class _FakeEmbedder:
    """可控的假 embedder：用来把自检的**判据**单独拎出来验。

    前两句（含 `EXPIRE`/`expire`）映射到同一个方向，后两句映射到正交方向。
    """

    def __init__(self, dim: int = 4, *, semantic: bool = True, boom: bool = False) -> None:
        self._dim = dim
        self._semantic = semantic
        self._boom = boom

    @property
    def info(self) -> EmbedderInfo:
        return EmbedderInfo("fake", self._dim, kind="fake", semantic=self._semantic)

    def embed(self, texts):
        if self._boom:
            raise RuntimeError("权重坏了")
        out = []
        for t in texts:
            if "EXPIRE" in t or "expire" in t:
                out.append([1.0] + [0.0] * (self._dim - 1))
            else:
                out.append([0.0] * (self._dim - 1) + [1.0])
        return out


class _FlatEmbedder(_FakeEmbedder):
    """**说谎的** embedder：自述 semantic=True，但把所有句子映射到同一点。

    这是实测判据存在的唯一理由 —— 声明可以撒谎，常数向量不会。
    """

    def embed(self, texts):
        return [[1.0] * self._dim for _ in texts]


class TestEmbedderSelftest(unittest.TestCase):
    """自检要能区分"维度对"和"语义对" —— 前者不能代替后者。"""

    def test_selftest_rejects_hashing_embedder(self) -> None:
        """`HashingEmbedder` 维度自洽、向量归一，但**自述不承载语义**。

        注意判据是**自述**（确定性），不是拿哈希噪声去量余弦差 ——
        后者会时红时绿，等于没有判据。
        ⚠️ 所以这里**只断言"自述那条"出现**，不断言总条数：
        哈希向量的余弦差是随机量，实测那条可能偶尔也报出来，那是正常的。
        """
        problems = selftest(HashingEmbedder())
        self.assertTrue(problems, "不承载语义的 embedder 不该通过自检")
        self.assertIn("semantic=False", " ".join(problems))

    def test_selftest_reports_self_declared_non_semantic(self) -> None:
        """判据一是**自述**，与实测无关。

        这个 fake 的语义分辨力是完美的（相关 1.0 / 无关 0.0），
        只要它自述 `semantic=False` 就必须报 —— 删掉那半行判据，这条会**确定**变红。
        （拿 HashingEmbedder 测做不到这点：它的余弦差是随机量。）
        """
        problems = selftest(_FakeEmbedder(semantic=False))
        self.assertEqual(len(problems), 1, f"应当只报自述这条：{problems}")
        self.assertIn("semantic=False", problems[0])

    def test_selftest_catches_a_liar(self) -> None:
        """自述 True 但实际分辨不了语义 ⇒ 实测判据必须抓到。"""
        problems = selftest(_FlatEmbedder())
        self.assertEqual(len(problems), 1, f"应当只报实测这一条：{problems}")
        self.assertIn("不分辨语义", problems[0])

    def test_selftest_passes_a_semantic_embedder(self) -> None:
        self.assertEqual(selftest(_FakeEmbedder()), [])

    def test_selftest_reports_dimension_mismatch(self) -> None:
        """自述 8 维、实际给 4 维 —— 必须报出来，不能只看语义。"""

        class _Short(_FakeEmbedder):
            def embed(self, texts):
                return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        problems = selftest(_Short(dim=8))
        self.assertTrue(problems)
        self.assertIn("维度", " ".join(problems))

    def test_selftest_reports_both_kinds_of_problem(self) -> None:
        """一次报全部 —— 别让人改一条跑一次。"""
        problems = selftest(_FlatEmbedder(semantic=False))
        self.assertEqual(len(problems), 2, f"自述 + 实测都该报：{problems}")

    def test_selftest_never_raises(self) -> None:
        """加载/编码炸了也要变成**一条问题**，不能把异常抛给调用方。"""
        problems = selftest(_FakeEmbedder(boom=True))
        self.assertEqual(len(problems), 1)
        self.assertIn("embed 失败", problems[0])

    def test_selftest_sentences_are_the_contract(self) -> None:
        """自检句本身是契约：前两句相关、后两句无关。改它们要连判据一起想。"""
        self.assertEqual(len(SELFTEST_SENTENCES), 4)
        self.assertIn("EXPIRE", SELFTEST_SENTENCES[0])
        self.assertIn("expire", SELFTEST_SENTENCES[1])
        self.assertIn("Kubernetes", SELFTEST_SENTENCES[2])
        self.assertIn("France", SELFTEST_SENTENCES[3])


if __name__ == "__main__":
    unittest.main()
