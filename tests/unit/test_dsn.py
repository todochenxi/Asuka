"""M71 · DSN 的解析顺序。

这条是被一次真实部署逼出来的：`apps.migrate apply` 在集群里报
`NO_DSN`，而那份 Pod 挂着 `AGENTOS_MANIFEST`，清单里写着 DSN ——
因为迁移器自己去找了环境变量，绕过了清单。

于是"这一次部署的配置"有了两个来源，而清单模式的定义恰恰是
"清单是唯一声明源"。两个来源的经典后果是：
清单里改了 DSN，迁移器连到旧的那个库，把迁移应用到**另一个**库，
然后报 up to date —— 一次看不出任何问题的成功。

这里钉住的是**顺序**，而且必须与 `RuntimeConfig.from_env()` 一致：
同一个进程里 bootstrap 连 A 库、迁移器连 B 库，
是没有任何机制能阻止、也没有任何报错会提到的失败。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps._dsn import hint, resolve_dsn

_MANIFEST = """
[stack]
provider = "examples.demo_stack:build_approval_demo_stack_factory"

[storage]
pg_dsn = "postgresql://from-manifest@host:5432/agentos"
"""
_MANIFEST_NO_DSN = """
[stack]
provider = "examples.demo_stack:build_approval_demo_stack_factory"

[storage]
kafka_brokers = "broker:9092"
"""


def _write(text: str) -> str:
    """写一份临时清单，返回路径。"""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return handle.name


class TestResolveDsn(unittest.TestCase):
    def test_an_explicit_dsn_wins_over_everything(self):
        path = _write(_MANIFEST)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        dsn = resolve_dsn(
            "postgresql://explicit@h/db",
            env={"AGENTOS_MANIFEST": path, "AGENTOS_PG_DSN": "postgresql://env@h/db"},
        )
        self.assertEqual(dsn, "postgresql://explicit@h/db")

    def test_a_manifest_wins_over_the_environment_variable(self):
        """要害：与 `RuntimeConfig.from_env()` 的顺序一致。"""
        path = _write(_MANIFEST)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        dsn = resolve_dsn(
            "",
            env={"AGENTOS_MANIFEST": path, "AGENTOS_PG_DSN": "postgresql://env@h/db"},
        )
        self.assertEqual(dsn, "postgresql://from-manifest@host:5432/agentos")

    def test_the_environment_variable_is_used_when_there_is_no_manifest(self):
        dsn = resolve_dsn("", env={"AGENTOS_PG_DSN": "postgresql://env@h/db"})
        self.assertEqual(dsn, "postgresql://env@h/db")

    def test_a_manifest_without_pg_dsn_is_rejected_not_silently_fallen_back(self):
        """清单缺 `storage.pg_dsn` 时**不许**悄悄退回环境变量。

        `storage.pg_dsn` 是 `agent_manifest.REQUIRED`：清单校验会先拒绝它，
        于是"清单里没写"根本走不到 DSN 解析这一步 —— 这是更强的保证，
        因为它在**读配置**时就死了，而不是等到连库那一刻。

        允许退回意味着：一份写漏了 DSN 的清单会带着进程连到
        某个旧环境变量指向的库上，而那份清单看起来是"通过了校验的"。
        """
        from packages.agent_manifest import ManifestError

        path = _write(_MANIFEST_NO_DSN)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        with self.assertRaises(ManifestError):
            resolve_dsn(
                "",
                env={
                    "AGENTOS_MANIFEST": path,
                    "AGENTOS_PG_DSN": "postgresql://env@h/db",
                },
            )

    def test_nothing_configured_is_an_empty_string_not_a_guess(self):
        """给不出就说给不出。猜一个 localhost 是最糟的：
        它会连到某个真实存在的库上去。"""
        self.assertEqual(resolve_dsn("", env={}), "")

    def test_the_hint_names_all_three_sources(self):
        text = hint()
        for source in ("AGENTOS_MANIFEST", "AGENTOS_PG_DSN", "--dsn"):
            self.assertIn(source, text)
