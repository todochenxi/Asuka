"""M75 · 部署清单也要有人检查。

--------------------------------------------------------------------------
它补的是什么空洞

`deploy/` 下那一整套（Dockerfile + 8 份 K8s 清单 + 2 份 TOML）此前
**零测试**。它们引用的东西没有一条断言要求它们成立：

    python -m apps.migrate          改个名就失效，而失效的方式是
                                    initContainer 起不来 → Pod 卡在
                                    Init:0/1，日志里说的是别的
    examples.demo_stack:build_...   函数改名同样静默
    ConfigMap 里内嵌的 TOML         与 deploy/config/*.toml 是**同一份
                                    声明的两处**（B-7），改一处漏一处
                                    就对不上，而没有任何机制会发现

最后一条最要紧：M59 给 TOML 做了 `manifest check`，M60 让它能启动服务，
**但 `deploy/config/agentos.toml` 从来没有被执行过那个 check** ——
也就是说"集群里那份部署声明"是不是合法的，此前没人问过。

--------------------------------------------------------------------------
为什么是扫文本而不是解析 YAML

本机没有 pyyaml（这也是 M59 选 TOML 的原因之一）。与 M68 那条
`TestTheLimitSurvivesIdleBackoff` 同一个办法：按行扫。
代价是脆，所以配了一条**控制组**用例先证明"扫得到东西" ——
否则下面每一条都是"因为空集合才通过"的。

--------------------------------------------------------------------------
一个刻意的边界

这里**不**断言"06-kafka-dependent.yaml 必须挂 readiness"。
那两个进程 `replicas: 0`（本次部署不启用），且它们是后台消费者 ——
没有 Service，readiness 只影响 endpoints，对它没有意义。
只要求**真在跑的** agentos 进程有 liveness。
"""
from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
K8S = DEPLOY / "k8s"
DOCKERFILE = DEPLOY / "Dockerfile"

#: ⚠️ 名字里**允许数字**：第一版写成 `[a-z_]+`，于是把 `apps.migrate`
#: 改成 `apps.migrate_v2` 这种改名**扫不出来**（`[a-z_]+` 吃到 `migrate_v`
#: 之后要求紧跟引号，撞上 `2` 就整条失配）—— 变异测试因此假绿。
_ENTRYPOINT = re.compile(
    r"python\",\s*\"-m\",\s*\"apps\.([a-z_][a-z_0-9]*)\""
    r"|python -m apps\.([a-z_][a-z_0-9]*)"
)
_PROVIDER = re.compile(r'"([a-z_][a-z_0-9.]*):([a-z_][a-z_0-9]*)"')
_COPY = re.compile(r"^COPY\s+(\S+)\s", re.M)
_REPLICAS = re.compile(r"^\s*replicas:\s*(\d+)", re.M)


def _deploy_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(DEPLOY.rglob("*"))
        if p.is_file() and p.suffix in {".yaml", ".toml", ".txt"}
    )


def _documents(text: str) -> list[str]:
    return [d for d in text.split("\n---\n") if d.strip()]


class TestTheScannersReallyFindThings(unittest.TestCase):
    """控制组：扫不到东西的话，下面每条都是"因为空集合才通过"的。"""

    def test_entrypoints_are_found(self):
        found = {m.group(1) or m.group(2) for m in _ENTRYPOINT.finditer(_deploy_text())}
        self.assertIn("migrate", found)
        self.assertIn("probe", found)
        self.assertGreaterEqual(len(found), 8)

    def test_providers_are_found(self):
        specs = {f"{m.group(1)}:{m.group(2)}" for m in _PROVIDER.finditer(_deploy_text())}
        self.assertIn("apps.executor_provider:build_executors", specs)
        self.assertGreaterEqual(len(specs), 3)

    def test_running_deployments_are_found(self):
        self.assertGreaterEqual(len(_running_agentos_docs()), 6)


def _running_agentos_docs() -> list[str]:
    """`replicas > 0` 且真的在跑 agentos 进程的 K8s 文档。

    刻意**不**按 `kind` 或名字挑：按它是否引用 `apps.` 来判。
    一个跑着 agentos 进程的 Deployment 就是"该被探活"的那一个 ——
    这与它叫什么、是不是 StatefulSet 无关。
    """
    out: list[str] = []
    for path in sorted(K8S.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for doc in _documents(text):
            if "apps." not in doc:
                continue
            m = _REPLICAS.search(doc)
            if m and int(m.group(1)) > 0:
                out.append(doc)
    return out


class TestTheEntrypointsTheClusterRunsExist(unittest.TestCase):
    """`python -m apps.X` 里的 X 必须真是一个进程（`apps/X/__main__.py`）。"""

    def test_every_referenced_module_is_a_process(self):
        found = {m.group(1) or m.group(2) for m in _ENTRYPOINT.finditer(_deploy_text())}
        for name in sorted(found):
            with self.subTest(module=f"apps.{name}"):
                self.assertTrue(
                    (ROOT / "apps" / name / "__main__.py").is_file(),
                    f"deploy/ runs `python -m apps.{name}` but "
                    f"apps/{name}/__main__.py does not exist — the pod would "
                    f"fail to start and the reason would not mention the rename",
                )


class TestTheProvidersTheClusterNamesAreLoadable(unittest.TestCase):
    """`module:attr` 形式的 provider：模块要能 import，属性要真的有。"""

    def test_every_referenced_provider_is_importable(self):
        specs = {f"{m.group(1)}:{m.group(2)}" for m in _PROVIDER.finditer(_deploy_text())}
        for spec in sorted(specs):
            module_name, _, attr = spec.partition(":")
            with self.subTest(provider=spec):
                try:
                    module = importlib.import_module(module_name)
                except ImportError as e:
                    self.fail(
                        f"deploy/ names provider {spec!r} but {module_name!r} "
                        f"cannot be imported: {e}"
                    )
                self.assertTrue(
                    hasattr(module, attr),
                    f"deploy/ names provider {spec!r} but {module_name!r} "
                    f"has no attribute {attr!r}",
                )


class TestTheConfigMapIsNotASecondCopy(unittest.TestCase):
    """ConfigMap 内嵌的 TOML 必须与 `deploy/config/*.toml` **语义相等**。

    B-7：同一份声明放在两处，改一处漏一处就对不上。
    这里不比字符串（缩进与注释必然不同），比**解析后的结构** ——
    注释怎么写不影响它是不是同一份配置。
    """

    _BLOCK = re.compile(r"^  (agentos|agents)\.toml: \|\n((?:    .*\n|\n)+)", re.M)

    def _embedded(self) -> dict[str, object]:
        import tomllib

        text = (K8S / "01-configmap.yaml").read_text(encoding="utf-8")
        out: dict[str, object] = {}
        for m in self._BLOCK.finditer(text):
            body = "\n".join(line[4:] for line in m.group(2).splitlines())
            out[f"{m.group(1)}.toml"] = tomllib.loads(body)
        return out

    def test_both_files_are_embedded(self):
        self.assertEqual(sorted(self._embedded()), ["agentos.toml", "agents.toml"])

    def test_the_embedded_config_equals_the_file_it_came_from(self):
        import tomllib

        embedded = self._embedded()
        for name in ("agentos.toml", "agents.toml"):
            with self.subTest(file=name):
                on_disk = tomllib.loads(
                    (DEPLOY / "config" / name).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    embedded[name],
                    on_disk,
                    f"deploy/k8s/01-configmap.yaml embeds a {name} that differs "
                    f"from deploy/config/{name} — the cluster would run a "
                    f"declaration nobody can see in the repo",
                )


class TestTheClusterDeclarationIsValid(unittest.TestCase):
    """集群里那份声明，必须过得了 M59 那个 check。"""

    def test_the_deployment_manifest_passes_the_check(self):
        from packages.agent_manifest import load

        manifest = load(DEPLOY / "config" / "agentos.toml")
        env = manifest.to_env()
        self.assertTrue(env["AGENTOS_PG_DSN"], "the cluster manifest has no pg_dsn")
        self.assertTrue(
            env["AGENTOS_STACK_PROVIDER"], "the cluster manifest has no stack provider"
        )

    def test_the_agent_registry_the_cluster_mounts_loads(self):
        from packages.agent_registry import load

        registry = load(DEPLOY / "config" / "agents.toml")
        self.assertIsNotNone(registry.get("agent-demo"))

    def test_the_heartbeat_ttl_is_shorter_than_the_lease(self):
        """与组合根那条配置期校验同源：心跳必须比租约短。"""
        from packages.agent_manifest import load

        env = load(DEPLOY / "config" / "agentos.toml").to_env()
        self.assertLess(
            int(env["AGENTOS_HEARTBEAT_SECONDS"]),
            int(env["AGENTOS_LEASE_TTL_SECONDS"]),
            "the cluster manifest would fail RuntimeConfig.from_env() at boot",
        )


class TestTheImageIsBuildable(unittest.TestCase):
    def test_every_copied_path_exists(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        paths = _COPY.findall(text)
        self.assertTrue(paths, "no COPY found — the scanner is broken")
        for src in paths:
            with self.subTest(path=src):
                self.assertTrue(
                    (ROOT / src).exists(),
                    f"Dockerfile copies {src!r} but it is not in the repo",
                )


class TestEveryRunningProcessIsProbed(unittest.TestCase):
    """M68 那条判据的部署侧：真在跑的进程，不许有一个漏掉探活。"""

    def test_every_running_agentos_process_has_a_liveness_probe(self):
        docs = _running_agentos_docs()
        for doc in docs:
            name = re.search(r"^\s*name:\s*(\S+)", doc, re.M)
            with self.subTest(deployment=name.group(1) if name else "?"):
                self.assertIn(
                    "livenessProbe",
                    doc,
                    "a running agentos deployment has no livenessProbe — "
                    "it would hang forever and the orchestrator would never "
                    "notice (M68 gave it a probe, but nobody wired it here)",
                )


class TestTheHeartbeatPathIsDeclaredOnce(unittest.TestCase):
    """B-7：心跳文件在哪，只许有一个答案。"""

    #: ⚠️ 两种写法都要认：Dockerfile 是 `ENV AGENTOS_HEARTBEAT_FILE=...`，
    #: K8s 是 `- {name: AGENTOS_HEARTBEAT_FILE, value: ...}`。
    #: 只认等号的话，在 K8s 里再声明一遍**扫不出来**（变异测试抓到的）。
    _DECLARED = re.compile(
        r"AGENTOS_HEARTBEAT_FILE\s*=|name:\s*AGENTOS_HEARTBEAT_FILE"
    )

    def test_it_is_set_in_exactly_one_place(self):
        hits = [
            f"{p.relative_to(ROOT)}:{n}"
            for p in sorted(DEPLOY.rglob("*"))
            if p.is_file()
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if self._DECLARED.search(line)
        ]
        self.assertEqual(
            len(hits),
            1,
            f"AGENTOS_HEARTBEAT_FILE is declared in {len(hits)} places: {hits} "
            f"— two answers means `apps.probe live` may read a file nobody writes",
        )


class TestTheImageTagNamesTheFrozenVersion(unittest.TestCase):
    """B-7：对外报的版本只许有一个 —— 清单里那份 tag 必须跟着它走。

    这条是补的时候**当场撞出来的**：清单里 14 处写着 `agentos:2.1.55-b3`，
    而服务已经报到 2.1.63。它不是"少了个功能"，是
    **"线上跑的是哪个构建"这个问题有了两个答案**，
    而且每次冻结基线都会再漂移一次（除非有东西盯着）。
    """

    _TAG = re.compile(r"image:\s*agentos:([^\s]+)")
    _VERSION = re.compile(r'version="(2\.\d+\.\d+)"')

    def _served(self) -> str:
        text = (ROOT / "apps" / "api" / "app.py").read_text(encoding="utf-8")
        m = self._VERSION.search(text)
        assert m is not None, "cannot find the version apps/api/app.py serves"
        return m.group(1)

    def test_every_agentos_image_tag_names_the_same_version(self):
        served = self._served()
        tags: set[str] = set()
        for path in sorted(K8S.glob("*.yaml")):
            tags.update(self._TAG.findall(path.read_text(encoding="utf-8")))
        self.assertTrue(tags, "no agentos image tag found — the scanner is broken")
        for tag in sorted(tags):
            with self.subTest(tag=tag):
                self.assertEqual(
                    tag.split("-")[0],
                    served,
                    f"deploy/ pins agentos:{tag} but the service reports "
                    f"{served} — the cluster would run a build nobody can name",
                )
