"""部署声明清单（M59 / M11 Manifest）。

--------------------------------------------------------------------------
它解决什么

现在一次部署的配置散在一堆环境变量里：

    AGENTOS_STACK_PROVIDER   AGENTOS_MODEL_PROVIDER   AGENTOS_TOOL_PROVIDER
    AGENTOS_PG_DSN           AGENTOS_KAFKA_BROKERS    AGENTOS_LEASE_TTL_SECONDS
    AGENTOS_BATCH_SIZE       AGENTOS_POLL_LIMIT       AGENTOS_HEARTBEAT_SECONDS
    ...

能跑，但**不可提交、不可 diff、不可校验**：
漏了一项不会报错，只会退化成某个默认值或某个内存兜底 ——
而"退化"在这套系统里是最危险的一类失败（它看起来是好的）。

一份清单把它们收在一起：

    [stack]
    provider = "examples.demo_stack:build_approval_demo_stack_factory"

    [storage]
    pg_dsn = "postgresql://..."

于是它可以进版本库、可以被 review、可以在启动时被校验。

--------------------------------------------------------------------------
为什么是 TOML 而不是 YAML

`tomllib` 是标准库；`pyyaml` 在这台机器上不可用。
用一个需要新增依赖的格式，会让"能不能读自己的配置文件"
取决于装没装上某个包 —— 不值得。

--------------------------------------------------------------------------
为什么它不新增第二条配置路径（B-7）

清单**不是**与环境变量并列的一套新配置。
它是环境变量的**唯一声明源**：`to_env()` 把清单翻译成环境变量，
交给既有的 `apps._bootstrap.RuntimeConfig` 去读。

于是"配置怎么生效"仍然只有一条路径 —— 只是写在哪儿变了。
否则就会出现"清单里改了、启动时却读的是旧环境变量"这种只对了一半的改动。

--------------------------------------------------------------------------
校验原则（PR-19）

缺必填 / 未知键 / 类型不对 → **点名拒绝**，不静默回退默认。
静默回退会让一句写错的键名变成一次成功的、但配置不对的启动。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any, Mapping


class ManifestError(ValueError):
    """清单写错了 —— 启动前就该死，不该带病启动。"""


#: 已知键 → 环境变量名。清单里出现这个表里没有的键 = 拼错了，拒绝。
KNOWN: Mapping[str, str] = {
    # stack —— 装配
    "stack.provider": "AGENTOS_STACK_PROVIDER",
    "stack.model": "AGENTOS_MODEL_PROVIDER",
    "stack.tool": "AGENTOS_TOOL_PROVIDER",
    "stack.executors": "AGENTOS_EXECUTORS",
    "stack.executor_provider": "AGENTOS_EXECUTOR_PROVIDER",
    # storage —— 存储
    "storage.pg_dsn": "AGENTOS_PG_DSN",
    "storage.kafka_brokers": "AGENTOS_KAFKA_BROKERS",
    # runtime —— 运行时
    "runtime.lease_ttl_seconds": "AGENTOS_LEASE_TTL_SECONDS",
    "runtime.heartbeat_seconds": "AGENTOS_HEARTBEAT_SECONDS",
    "runtime.batch_size": "AGENTOS_BATCH_SIZE",
    "runtime.poll_limit": "AGENTOS_POLL_LIMIT",
    "runtime.free_slots": "AGENTOS_FREE_SLOTS",
    # api —— 控制面
    "api.host": "AGENTOS_API_HOST",
    "api.port": "AGENTOS_API_PORT",
    # 标识
    "instance.id": "AGENTOS_INSTANCE_ID",
    "instance.labels": "AGENTOS_LABELS",
}

#: 必填。缺了就跑不起来 —— 但注意 `stack.provider` 缺了不是"用默认栈"，
#: 而是"不知道该装什么"，所以必须点名。
REQUIRED: tuple[str, ...] = ("stack.provider", "storage.pg_dsn")


@dataclass(frozen=True)
class Manifest:
    """一份校验过的部署声明。"""

    values: dict[str, Any] = field(default_factory=dict)

    def to_env(self) -> dict[str, str]:
        """翻译成环境变量 —— 交给既有的 bootstrap，不新增配置路径。"""
        env: dict[str, str] = {}
        for key, value in self.values.items():
            env[KNOWN[key]] = "" if value is None else str(value)
        return env

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _flatten(raw: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套的 TOML 表压成 `a.b.c` 的扁平键。"""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def parse(text: str) -> Manifest:
    """读一份清单文本并校验。

    三件事会让它拒绝：未知键（多半是拼错）、缺必填、类型不对。
    一律点名 —— 不静默忽略、不静默回退默认。
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"MANIFEST_NOT_TOML: {e}") from None

    flat = _flatten(raw)
    if not flat:
        raise ManifestError("MANIFEST_EMPTY: the manifest declares nothing")

    unknown = sorted(k for k in flat if k not in KNOWN)
    if unknown:
        raise ManifestError(
            "MANIFEST_UNKNOWN_KEY: " + ", ".join(unknown)
            + " (known keys: " + ", ".join(sorted(KNOWN)) + ")"
        )

    missing = [k for k in REQUIRED if k not in flat]
    if missing:
        raise ManifestError(
            "MANIFEST_MISSING: " + ", ".join(missing)
        )

    for key, value in flat.items():
        if isinstance(value, (dict, list)):
            raise ManifestError(
                f"MANIFEST_BAD_TYPE: {key} must be a scalar, got "
                f"{type(value).__name__}"
            )

    return Manifest(values=flat)


def load(path: str) -> Manifest:
    with open(path, "rb") as fh:
        return parse(fh.read().decode("utf-8"))
