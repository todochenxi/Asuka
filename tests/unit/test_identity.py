"""M105 · IAM 身份层（M9）。

补的空洞：API 此前**没有任何"谁在调用"的概念** —— `by` 是请求体里的自由字符串，
`PolicyContext.user_id` 从没被认证过。这一层钉住三条不变量：

    I-1  没凭据 = 没身份（401，绝不退化成匿名）
    I-2  有身份没 scope → 403（与 401 分开）
    I-3  actor 来自身份，不来自请求体（这条在 HTTP 层由集成测试钉住）
"""
from __future__ import annotations

import json
import unittest

from packages.agent_api.errors import Forbidden, Unauthenticated
from packages.agent_api.identity import (
    SCOPE_RUNS_WRITE,
    Identity,
    InMemoryIdentityProvider,
    authenticate,
    bearer_token,
    require_scope,
)


class BearerTest(unittest.TestCase):
    def test_a_well_formed_header_yields_the_token(self) -> None:
        self.assertEqual(bearer_token("Bearer abc.def"), "abc.def")
        self.assertEqual(bearer_token("bearer xyz"), "xyz")  # 大小写不敏感

    def test_i1_a_missing_header_is_unauthenticated(self) -> None:
        for header in ("", "   ", None):
            with self.subTest(header=header):
                with self.assertRaises(Unauthenticated):
                    bearer_token(header)

    def test_i1_a_garbled_header_is_unauthenticated(self) -> None:
        for header in ("Bearer", "Bearer a b", "Basic xyz", "abc"):
            with self.subTest(header=header):
                with self.assertRaises(Unauthenticated):
                    bearer_token(header)


class ProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Identity(subject="alice", tenant_id="acme", scopes=frozenset({SCOPE_RUNS_WRITE}))
        self.provider = InMemoryIdentityProvider({"tok-alice": self.alice})

    def test_authenticate_returns_the_identity(self) -> None:
        self.assertIs(authenticate(self.provider, "Bearer tok-alice"), self.alice)

    def test_an_unknown_token_is_unauthenticated(self) -> None:
        with self.assertRaises(Unauthenticated):
            authenticate(self.provider, "Bearer nope")

    def test_an_empty_token_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            InMemoryIdentityProvider({"": self.alice})


class ScopeTest(unittest.TestCase):
    def test_i2_a_missing_scope_is_forbidden(self) -> None:
        identity = Identity(subject="bob", scopes=frozenset({"runs:read"}))
        with self.assertRaises(Forbidden) as ctx:
            require_scope(identity, SCOPE_RUNS_WRITE)
        self.assertIn("runs:write", str(ctx.exception))

    def test_a_present_scope_passes_and_returns_the_identity(self) -> None:
        identity = Identity(subject="bob", scopes=frozenset({SCOPE_RUNS_WRITE}))
        self.assertIs(require_scope(identity, SCOPE_RUNS_WRITE), identity)

    def test_401_and_403_are_distinct_http_semantics(self) -> None:
        self.assertEqual(Unauthenticated("x").http_status, 401)
        self.assertEqual(Forbidden("x").http_status, 403)


class IdentityShapeTest(unittest.TestCase):
    def test_subject_is_required(self) -> None:
        with self.assertRaises(ValueError):
            Identity(subject="")

    def test_scopes_and_attributes_are_copied(self) -> None:
        identity = Identity(subject="a", scopes=["x"], attributes={"k": 1})
        self.assertEqual(identity.scopes, frozenset({"x"}))
        self.assertEqual(identity.attributes, {"k": 1})



class FromJsonTest(unittest.TestCase):
    def test_a_static_table_is_parsed(self) -> None:
        provider = InMemoryIdentityProvider.from_json(
            json.dumps(
                {"tok": {"subject": "alice", "tenant_id": "acme", "scopes": ["runs:write"]}}
            )
        )
        identity = provider.authenticate("tok")
        self.assertEqual(identity.subject, "alice")
        self.assertEqual(identity.tenant_id, "acme")
        self.assertTrue(identity.has_scope("runs:write"))

    def test_empty_text_is_an_empty_provider(self) -> None:
        self.assertEqual(InMemoryIdentityProvider.from_json("").tokens, {})

    def test_an_unknown_field_is_refused(self) -> None:
        """少写一个 s（`scope` 而非 `scopes`）不许静默失效。"""
        with self.assertRaises(ValueError) as ctx:
            InMemoryIdentityProvider.from_json(json.dumps({"t": {"subject": "a", "scope": ["x"]}}))
        self.assertIn("scope", str(ctx.exception))

    def test_a_missing_subject_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            InMemoryIdentityProvider.from_json(json.dumps({"t": {"tenant_id": "acme"}}))

    def test_bad_json_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            InMemoryIdentityProvider.from_json("{not json")


if __name__ == "__main__":
    unittest.main()
