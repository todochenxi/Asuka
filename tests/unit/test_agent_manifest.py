"""M59 / M11 Manifest：清单的校验与翻译。

最要紧的一条：**静默回退默认是最危险的失败** ——
一句写错的键名会变成一次成功的、但配置不对的启动。
所以未知键 / 缺必填 / 类型错都必须点名拒绝。
"""
from __future__ import annotations

import unittest

from packages.agent_manifest import (
    KNOWN,
    REQUIRED,
    ManifestError,
    Manifest,
    parse,
)


def _valid() -> str:
    return """
[stack]
provider = "examples.demo_stack:build_approval_demo_stack_factory"

[storage]
pg_dsn = "postgresql://postgres:postgres@127.0.0.1:5433/agentos_it"
"""


class TestParsing(unittest.TestCase):
    def test_a_valid_manifest_is_accepted(self):
        m = parse(_valid())
        self.assertEqual(
            m.get("stack.provider"),
            "examples.demo_stack:build_approval_demo_stack_factory",
        )
        self.assertIn("storage.pg_dsn", m.values)

    def test_nested_tables_are_flattened(self):
        m = parse(_valid() + '\n[runtime]\nlease_ttl_seconds = 30\n')
        self.assertEqual(m.get("runtime.lease_ttl_seconds"), 30)


class TestRefusal(unittest.TestCase):
    """三类错误都必须点名，不许静默。"""

    def test_unknown_key_is_refused(self):
        """拼错的键名 —— 静默忽略的话，那条配置就永远不生效。"""
        with self.assertRaises(ManifestError) as ctx:
            parse('[stack]\nprovider = "x"\nstak = "y"\n')
        self.assertIn("MANIFEST_UNKNOWN_KEY", str(ctx.exception))
        self.assertIn("stak", str(ctx.exception))

    def test_missing_required_is_refused(self):
        with self.assertRaises(ManifestError) as ctx:
            parse('[stack]\nprovider = "x"\n')
        self.assertIn("MANIFEST_MISSING", str(ctx.exception))
        self.assertIn("storage.pg_dsn", str(ctx.exception))

    def test_empty_manifest_is_refused(self):
        with self.assertRaises(ManifestError) as ctx:
            parse("")
        self.assertIn("MANIFEST_EMPTY", str(ctx.exception))

    def test_broken_toml_is_refused(self):
        with self.assertRaises(ManifestError) as ctx:
            parse("[stack\nprovider = ")
        self.assertIn("MANIFEST_NOT_TOML", str(ctx.exception))

    def test_a_list_where_a_scalar_is_expected_is_refused(self):
        """`provider = ["x"]` 写成了列表 —— 得说清是类型不对。

        注意嵌套**表**不会走到这里：压平后它变成一个未知键
        （`storage.pg_dsn.a`），先被上一条拦住。
        """
        # 必填项得先齐全，否则会先被 MANIFEST_MISSING 拦住（校验有顺序）
        with self.assertRaises(ManifestError) as ctx:
            parse(_valid() + "\n[runtime]\nbatch_size = [1, 2]\n")
        self.assertIn("MANIFEST_BAD_TYPE", str(ctx.exception))

    def test_the_error_lists_known_keys_so_the_caller_can_fix_it(self):
        """报错要能让人改好，不只是说"错了"（PR-19）。"""
        with self.assertRaises(ManifestError) as ctx:
            parse('[stack]\nprovider = "x"\nnope = 1\n')
        self.assertIn("known keys", str(ctx.exception))


class TestToEnv(unittest.TestCase):
    """清单是环境变量的**声明源**，不是并列的第二套配置（B-7）。"""

    def test_it_translates_to_the_existing_env_var_names(self):
        m = parse(_valid())
        env = m.to_env()
        self.assertEqual(
            env["AGENTOS_STACK_PROVIDER"],
            "examples.demo_stack:build_approval_demo_stack_factory",
        )
        self.assertIn("AGENTOS_PG_DSN", env)

    def test_every_known_key_maps_to_a_declared_env_var(self):
        """表里的每个键都得有对应环境变量 —— 否则那条声明落地不了。"""
        for key, var in KNOWN.items():
            self.assertTrue(var.startswith("AGENTOS_"), f"{key} -> {var}")

    def test_required_keys_are_a_subset_of_known(self):
        for key in REQUIRED:
            self.assertIn(key, KNOWN)

    def test_empty_values_become_empty_strings_not_none(self):
        """环境变量没有 None —— 空声明要落成空串，交给下游判定。"""
        m = Manifest(values={"api.host": None})
        self.assertEqual(m.to_env()["AGENTOS_API_HOST"], "")


if __name__ == "__main__":
    unittest.main()
