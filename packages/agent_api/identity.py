"""IAM：这次调用**是谁**（基线 §23 / M9）。

### 它补的是什么

直到 M105，API 这一层**没有任何"谁在调用"的概念**：

    · `PolicyContext.user_id` 一直都在，但从没有任何东西真的认证过它；
    · `cancel` / `decide` 的 `by` 是请求体里一个**自由字符串** ——
      任何人都能声称自己是任何人，而审计要回答的正是"到底是谁"；
    · 没有认证，也谈不上按身份限权。

所以这一层只做两件事，且都在**框架之外**（A-7）：

    authenticate()      凭据 → Identity（不认识就 401，**不退回匿名**）
    require_scope()     身份 → 有没有干这件事的权限（没有就 403）

⚠️ 它**不** import `agent_harness.policy` —— A-1 最强的形式是"API 连策略规则
都看不见"（`test_control_plane_api` 会断言这一点）。把 Identity 送进
PolicyContext 是 Runtime/Harness 侧的事，不该由契约层越界去拼。

### 三条不变量

| # | 内容 |
|---|---|
| I-1 | **没有凭据 = 没有身份**。缺失 / 拼错的 `Authorization` 一律 401，绝不退化成匿名身份 |
| I-2 | 身份有效但缺 scope → **403**，与 401 分开（该重登 vs 该申请权限是两件事） |
| I-3 | 配了认证器时，**actor（`by`）来自身份，不来自请求体** —— 否则署名可伪造，审计形同虚设 |

⚠️ 本模块不 import `fastapi`，也不认识 HTTP：它产出/抛出的都是**领域级**的
`Identity` 与 `Unauthenticated`/`Forbidden`（后者是 `ApiError`，映射在 `errors.py` 一处）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .errors import Forbidden, Unauthenticated

#: scope 名字集中在这一处 —— 字符串散落会让"这个端点要什么权限"没有答案（B-7）。
SCOPE_RUNS_WRITE = "runs:write"
SCOPE_RUNS_READ = "runs:read"
SCOPE_APPROVALS_DECIDE = "approvals:decide"

_IDENTITY_KEYS = frozenset({"subject", "tenant_id", "scopes", "attributes"})


@dataclass(frozen=True)
class Identity:
    """一个已认证的调用者。"""

    subject: str
    tenant_id: str = ""
    scopes: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("Identity.subject is required (an identity with no name is anonymous)")
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        object.__setattr__(self, "attributes", dict(self.attributes))

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class IdentityProvider(Protocol):
    """凭据 → 身份。查不到**必须**抛 `Unauthenticated`，不许返回 None。"""

    def authenticate(self, token: str) -> Identity: ...


@dataclass
class InMemoryIdentityProvider:
    """`token → Identity` 的静态表。

    够用于单租户部署 / 测试；真实的 IdP（OIDC / 内部账号）实现同一个端口即可。
    凭据按**原样**比对 —— 刻意不做大小写折叠或前缀模糊，那会把一个近似 token
    认成一个真 token（PR-34：宁可拒绝，不要编）。
    """

    tokens: Mapping[str, Identity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tokens = dict(self.tokens)
        for token, identity in self.tokens.items():
            if not token:
                raise ValueError("an identity token must not be empty")
            if not isinstance(identity, Identity):
                raise ValueError(f"token {token!r} does not map to an Identity")

    def authenticate(self, token: str) -> Identity:
        identity = self.tokens.get(token)
        if identity is None:
            # 不区分"没这个 token"与"token 拼错"：都只说"不认识"。
            raise Unauthenticated("unknown credential")
        return identity

    @classmethod
    def from_json(cls, text: str) -> "InMemoryIdentityProvider":
        """从 `AGENTOS_IDENTITY_TOKENS` 那样的一份 JSON 建表。

        形状：`{"<token>": {"subject": "...", "tenant_id": "...", "scopes": [...]}}`。
        未知字段**拒绝** —— 一个拼错的 `scope`（少了 s）静默失效，
        会让"这个 token 能写 Run"变成"只能读"，且没有任何报错。
        """
        raw = (text or "").strip()
        if not raw:
            return cls({})
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"identity tokens must be JSON: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("identity tokens must be a JSON object of token → identity")

        tokens: dict[str, Identity] = {}
        for token, spec in parsed.items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"identity for token {token!r} must be an object")
            unknown = sorted(set(spec) - _IDENTITY_KEYS)
            if unknown:
                raise ValueError(
                    f"identity for token {token!r} has unknown key(s) {unknown}; "
                    f"allowed: {sorted(_IDENTITY_KEYS)}"
                )
            subject = str(spec.get("subject") or "")
            if not subject:
                raise ValueError(f"identity for token {token!r} has no 'subject'")
            scopes = spec.get("scopes") or ()
            if not isinstance(scopes, (list, tuple)):
                raise ValueError(f"identity for token {token!r}: 'scopes' must be a list")
            attributes = spec.get("attributes") or {}
            if not isinstance(attributes, Mapping):
                raise ValueError(f"identity for token {token!r}: 'attributes' must be an object")
            tokens[str(token)] = Identity(
                subject=subject,
                tenant_id=str(spec.get("tenant_id") or ""),
                scopes=frozenset(str(s) for s in scopes),
                attributes=dict(attributes),
            )
        return cls(tokens)


def bearer_token(authorization: str) -> str:
    """把 `Authorization: Bearer <token>` 解析成一个 token（I-1）。

    严格：必须是**恰好两段**、第一段是 `Bearer`（大小写不敏感）、token 非空。
    `"Bearer"`（少 token）、`"bearer a b"`、`"Basic xyz"` 一律 401 ——
    宽松解析会让一个格式错误的头变成一个"空 token 的身份"。
    """
    if not authorization or not authorization.strip():
        raise Unauthenticated("missing Authorization header; expected 'Bearer <token>'")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise Unauthenticated("Authorization must be exactly 'Bearer <token>'")
    return parts[1]


def authenticate(provider: IdentityProvider, authorization: str) -> Identity:
    """认证一个请求头。这是"谁在调用"的唯一入口。"""
    return provider.authenticate(bearer_token(authorization))


def require_scope(identity: Identity, scope: str) -> Identity:
    """I-2：没有 scope → 403（不是 401）。"""
    if not identity.has_scope(scope):
        raise Forbidden(
            f"identity {identity.subject!r} lacks the {scope!r} scope",
            required_scope=scope,
        )
    return identity


__all__ = [
    "SCOPE_APPROVALS_DECIDE",
    "SCOPE_RUNS_READ",
    "SCOPE_RUNS_WRITE",
    "Identity",
    "IdentityProvider",
    "InMemoryIdentityProvider",
    "authenticate",
    "bearer_token",
    "policy_context_for",
    "require_scope",
]
