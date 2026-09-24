"""Vault / 密钥引用与脱敏（基线 §23 / M9）。

### 它补的是什么

到 M105 之前，"一个秘密"在代码里**就是一个普通字符串**：
`os.environ["DEEPSEEK_API_KEY"]`、DSN 里的口令……它和别的字符串没有任何区别，
于是它可以被随手 log、被塞进异常消息、被写进 Trace，而**没有任何机制**会发现。
`SecretPatternGuardrail` 只能按 `sk-` 这类**形状**猜，猜不到一个长得像普通词的口令。

所以这一层给两样东西：

    Secret           一个**知道自己是秘密**的值：repr / str / f-string 一律 `***`，
                     只有显式 `.reveal()` 才拿得到明文
    SecretProvider   引用（`secret://env/NAME`）→ `Secret` 的端口，
                     env 与内存两个实现，真实 Vault 实现同一个端口即可

### 一条不变量

| # | 内容 |
|---|---|
| V-1 | `Secret` 在 `repr` / `str` / f-string / `"{}".format` 里**绝不**输出明文（只有 `reveal()` 可以） |

### 为什么引用语法是 `secret://<scheme>/<key>`

因为它把"这个值是明文"和"这个值是指向秘密的引用"分开了：
配置文件、manifest、ConfigMap 里放的是**引用**（可以进版本库、可以 review），
明文只活在 SecretProvider 背后（环境变量 / Vault / KMS）。
`resolve()` 遇到不是引用的普通值原样返回 —— 于是"只有秘密走引用"，
而不是把所有配置都强行包一层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

_REFERENCE_PREFIX = "secret://"


class SecretNotFound(Exception):
    """provider 里没有这个 key。**不返回空串** —— 空密钥会让认证静默失败。"""


@dataclass(frozen=True)
class Secret:
    """一个知道自己是秘密的字符串（V-1）。"""

    _value: str

    def reveal(self) -> str:
        """显式取出明文。只有在**边界**（拼 HTTP 头、连库）才该调它。"""
        return self._value

    def is_empty(self) -> bool:
        return not self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return format("***", spec)


@dataclass(frozen=True)
class SecretReference:
    """`secret://<scheme>/<key>`。key 允许含 `/`（`vault/db/password`）。"""

    scheme: str
    key: str

    @classmethod
    def parse(cls, text: str) -> "SecretReference | None":
        """是引用就解析，不是就返回 `None`（普通配置值不归这里管）。"""
        if not isinstance(text, str) or not text.startswith(_REFERENCE_PREFIX):
            return None
        rest = text[len(_REFERENCE_PREFIX) :]
        scheme, sep, key = rest.partition("/")
        if not sep or not scheme or not key:
            raise ValueError(
                f"malformed secret reference {text!r}; expected "
                f"'secret://<scheme>/<key>'"
            )
        return cls(scheme=scheme, key=key)

    def __str__(self) -> str:
        return f"{_REFERENCE_PREFIX}{self.scheme}/{self.key}"


class SecretProvider(Protocol):
    """引用 → `Secret`。查不到**必须**抛 `SecretNotFound`。"""

    scheme: str

    def resolve(self, reference: SecretReference) -> Secret: ...


@dataclass
class InMemorySecretProvider:
    """进程内表（测试 / 本地）。scheme 默认 `memory`。"""

    values: Mapping[str, str] = field(default_factory=dict)
    scheme: str = "memory"

    def resolve(self, reference: SecretReference) -> Secret:
        self._check_scheme(reference)
        if reference.key not in self.values:
            raise SecretNotFound(f"no secret {reference.key!r} in the in-memory provider")
        return Secret(self.values[reference.key])

    def _check_scheme(self, reference: SecretReference) -> None:
        if reference.scheme != self.scheme:
            raise SecretNotFound(
                f"this provider serves scheme {self.scheme!r}, not {reference.scheme!r}"
            )


@dataclass
class EnvSecretProvider:
    """从环境变量取（生产里最朴素的 Vault 替身）。scheme 默认 `env`。

    ⚠️ 取值用 `os.environ` **快照**还是实时由调用方决定：传 `env` 进来就固定，
    不传就每次读进程环境（与 `_dsn.resolve_dsn` 的语义一致）。
    """

    env: Mapping[str, str] | None = None
    scheme: str = "env"

    def resolve(self, reference: SecretReference) -> Secret:
        if reference.scheme != self.scheme:
            raise SecretNotFound(
                f"this provider serves scheme {self.scheme!r}, not {reference.scheme!r}"
            )
        import os

        source = self.env if self.env is not None else os.environ
        if reference.key not in source:
            raise SecretNotFound(
                f"environment variable {reference.key!r} is not set, but a secret "
                f"reference points at it"
            )
        return Secret(source[reference.key])


def resolve_secret(
    text: str, providers: Mapping[str, SecretProvider]
) -> Secret | None:
    """`text` 是引用就解析成 `Secret`，不是就返回 `None`。"""
    reference = SecretReference.parse(text)
    if reference is None:
        return None
    provider = providers.get(reference.scheme)
    if provider is None:
        raise SecretNotFound(
            f"no provider for secret scheme {reference.scheme!r}; "
            f"configured schemes: {sorted(providers)}"
        )
    return provider.resolve(reference)


def resolve(text: str, providers: Mapping[str, SecretProvider]) -> str:
    """把引用解析成明文，普通值原样返回。

    这是**边界**上的便捷函数：调用方本就要一个字符串（例如一个 DSN）。
    只在必须交出明文的那一刻调它。
    """
    secret = resolve_secret(text, providers)
    return text if secret is None else secret.reveal()


__all__ = [
    "EnvSecretProvider",
    "InMemorySecretProvider",
    "Secret",
    "SecretNotFound",
    "SecretProvider",
    "SecretReference",
    "resolve",
    "resolve_secret",
]
