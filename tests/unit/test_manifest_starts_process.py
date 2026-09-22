"""M60：清单真的能驱动一次启动（不只是被校验）。

--------------------------------------------------------------------------
为什么单独一层测

`tests/unit/test_agent_manifest.py` 测的是"清单本身对不对"。
但清单存在的理由是**让一次部署说得出自己是什么** ——
它必须真的能把一次启动配置出来，否则就只是一个格式良好的文件而已。

所以这里测的是接缝：`AGENTOS_MANIFEST` → `RuntimeConfig`。

--------------------------------------------------------------------------
一条重要的设计约束（B-7）

清单模式与环境变量模式**二选一**，一次启动只认一个来源。
不叠加 —— 否则"清单里改了、实际读的是旧环境变量"这种
只对了一半的改动就成为可能（M58 冻过同一条道理）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from packages.agent_manifest import ManifestError


def _manifest_text(**overrides) -> str:
    base = {
        "stack.provider": "examples.demo_stack:build_approval_demo_stack_factory",
        "storage.pg_dsn": "postgresql://postgres:postgres@127.0.0.1:5433/agentos_it",
    }
    base.update(overrides)
    lines = []
    for k, v in base.items():
        section, _, key = k.partition(".")
        if not any(l.startswith(f"[{section}]") for l in lines):
            lines.append(f"[{section}]")
        lines.append(f"{key} = \"{v}\"" if isinstance(v, str) else f"{key} = {v}")
    return "\n".join(lines)


class _CfgCase(unittest.TestCase):
    def setUp(self) -> None:
        from apps._bootstrap import RuntimeConfig

        self.RuntimeConfig = RuntimeConfig

    def _config(self, manifest_text: str, env: dict | None = None):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agentos.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(manifest_text)
            e = dict(env or {})
            e["AGENTOS_MANIFEST"] = path
            return self.RuntimeConfig.from_env(e)


class TestManifestDrivesStartup(_CfgCase):
    def test_the_manifest_supplies_the_stack_and_storage(self):
        cfg = self._config(_manifest_text())
        self.assertEqual(
            cfg.stack_provider,
            "examples.demo_stack:build_approval_demo_stack_factory",
        )
        self.assertIn("agentos_it", cfg.pg_dsn)

    def test_runtime_knobs_come_from_the_manifest(self):
        cfg = self._config(
            _manifest_text(**{
                "api.host": "127.0.0.1",
                "api.port": 8011,
                "runtime.lease_ttl_seconds": 30,
            })
        )
        self.assertEqual(cfg.api_port, 8011)
        self.assertEqual(cfg.lease_ttl.total_seconds(), 30)

    def test_env_var_mode_still_works_without_a_manifest(self):
        """没给清单就还是老样子 —— 向后兼容，不强制迁移。"""
        cfg = self.RuntimeConfig.from_env({
            "AGENTOS_PG_DSN": "postgresql://x/y",
            "AGENTOS_STACK_PROVIDER": "mod:fn",
        })
        self.assertEqual(cfg.stack_provider, "mod:fn")


class TestManifestIsTheOnlySource(_CfgCase):
    """清单模式里，环境变量不该再掺和（B-7）。"""

    def test_env_vars_do_not_override_the_manifest(self):
        """清单说 8011，环境变量说 9999 —— 必须是 8011。

        若这里变成"谁都行"，一次启动就有两个来源，
        也就埋下了"改了清单却没生效"这种只对了一半的改动。
        """
        cfg = self._config(
            _manifest_text(**{"api.port": 8011}),
            env={"AGENTOS_API_PORT": "9999"},
        )
        self.assertEqual(cfg.api_port, 8011)


class TestBadManifestBlocksStartup(_CfgCase):
    def test_an_invalid_manifest_raises_before_startup(self):
        """配置错要在启动前死 —— 不能带病启动后再退化成某个默认值。"""
        from apps._bootstrap import ConfigurationError

        with self.assertRaises(ConfigurationError) as ctx:
            self._config(_manifest_text(**{"stack.stak": "typo"}))
        self.assertIn("invalid manifest", str(ctx.exception))

    def test_a_missing_manifest_file_raises(self):
        from apps._bootstrap import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self.RuntimeConfig.from_env({"AGENTOS_MANIFEST": "no/such.toml"})


if __name__ == "__main__":
    unittest.main()
