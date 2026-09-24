"""M105 · Vault：密钥引用与脱敏（M9）。

补的空洞：一个秘密在代码里此前就是**普通字符串**，可以被随手 log / 塞进异常。
这一层给 `Secret`（知道自己是谁，repr 一律 `***`）与 `SecretProvider`
（`secret://<scheme>/<key>` → `Secret`），并钉住一条不变量：

    V-1  Secret 在 repr / str / f-string / format 里绝不输出明文
"""
from __future__ import annotations

import unittest

from packages.agent_harness.secrets import (
    EnvSecretProvider,
    InMemorySecretProvider,
    Secret,
    SecretNotFound,
    SecretReference,
    resolve,
    resolve_secret,
)


class SecretTest(unittest.TestCase):
    def test_v1_nothing_reveals_the_value_but_reveal(self) -> None:
        secret = Secret("hunter2")
        self.assertNotIn("hunter2", repr(secret))
        self.assertNotIn("hunter2", str(secret))
        self.assertNotIn("hunter2", f"{secret}")
        self.assertNotIn("hunter2", "{}".format(secret))  # noqa: UP032
        self.assertNotIn("hunter2", f"{secret:>10}")
        self.assertEqual(secret.reveal(), "hunter2")

    def test_truthiness_tracks_the_value(self) -> None:
        self.assertTrue(Secret("x"))
        self.assertFalse(Secret(""))
        self.assertTrue(Secret("").is_empty())


class ReferenceTest(unittest.TestCase):
    def test_a_plain_value_is_not_a_reference(self) -> None:
        self.assertIsNone(SecretReference.parse("postgresql://host/db"))

    def test_a_reference_is_parsed(self) -> None:
        ref = SecretReference.parse("secret://env/DB_PASSWORD")
        assert ref is not None
        self.assertEqual((ref.scheme, ref.key), ("env", "DB_PASSWORD"))
        # key 允许含 `/`
        deep = SecretReference.parse("secret://vault/db/password")
        assert deep is not None
        self.assertEqual(deep.key, "db/password")

    def test_a_malformed_reference_is_refused(self) -> None:
        for text in ("secret://env", "secret://env/", "secret:///key"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    SecretReference.parse(text)


class ProviderTest(unittest.TestCase):
    def test_env_provider_resolves(self) -> None:
        provider = EnvSecretProvider(env={"DB_PASSWORD": "s3cret"})
        ref = SecretReference.parse("secret://env/DB_PASSWORD")
        assert ref is not None
        self.assertEqual(provider.resolve(ref).reveal(), "s3cret")

    def test_a_missing_env_var_is_refused_not_empty(self) -> None:
        provider = EnvSecretProvider(env={})
        ref = SecretReference.parse("secret://env/NOPE")
        assert ref is not None
        with self.assertRaises(SecretNotFound):
            provider.resolve(ref)

    def test_in_memory_provider_and_scheme_mismatch(self) -> None:
        provider = InMemorySecretProvider({"k": "v"})
        good = SecretReference.parse("secret://memory/k")
        bad = SecretReference.parse("secret://env/k")
        assert good is not None and bad is not None
        self.assertEqual(provider.resolve(good).reveal(), "v")
        with self.assertRaises(SecretNotFound):
            provider.resolve(bad)


class ResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = {
            "memory": InMemorySecretProvider({"db": "postgresql://u@h/db"}),
            "env": EnvSecretProvider(env={"KEY": "value"}),
        }

    def test_resolve_secret_returns_a_secret_for_a_reference(self) -> None:
        secret = resolve_secret("secret://memory/db", self.providers)
        assert secret is not None
        self.assertEqual(secret.reveal(), "postgresql://u@h/db")

    def test_resolve_secret_returns_none_for_a_plain_value(self) -> None:
        self.assertIsNone(resolve_secret("just-a-string", self.providers))

    def test_resolve_returns_plaintext_at_the_boundary(self) -> None:
        self.assertEqual(resolve("secret://env/KEY", self.providers), "value")
        self.assertEqual(resolve("plain", self.providers), "plain")

    def test_an_unknown_scheme_is_refused(self) -> None:
        with self.assertRaises(SecretNotFound) as ctx:
            resolve_secret("secret://vault/k", self.providers)
        self.assertIn("vault", str(ctx.exception))


class DsnReferenceTest(unittest.TestCase):
    """`_dsn.resolve_dsn` 的 M105 接法：配置里可以放引用而不是明文。"""

    def test_a_reference_env_dsn_is_resolved(self) -> None:
        from apps._dsn import resolve_dsn

        providers = {"memory": InMemorySecretProvider({"dsn": "postgresql://from-vault/db"})}
        dsn = resolve_dsn(
            env={"AGENTOS_PG_DSN": "secret://memory/dsn"}, secrets=providers
        )
        self.assertEqual(dsn, "postgresql://from-vault/db")

    def test_without_a_provider_the_reference_is_untouched(self) -> None:
        from apps._dsn import resolve_dsn

        dsn = resolve_dsn(env={"AGENTOS_PG_DSN": "secret://memory/dsn"})
        self.assertEqual(dsn, "secret://memory/dsn")


if __name__ == "__main__":
    unittest.main()
