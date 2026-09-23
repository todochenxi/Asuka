"""M102 · M7 Production：CI/CD 流水线 + HPA + 滚动/回滚策略。

补的空洞：`deploy/` 下一整套清单与一个 Dockerfile 都在（M69/M70/M75），
但**没有任何一条流水线真的跑过它们**，"能上线"的每一步都靠人记得做；
`replicas: 1` 也是写死的，负载上来在编排层没有任何出口。

同 M75：本机没有 pyyaml，所以按行扫文本。代价是脆，因此每条都配一条
**控制组**先证明"扫得到东西" —— 否则每一条都是"因为空集合才通过"的。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"
CI = ROOT / ".github" / "workflows" / "ci.yml"

_UNIT_CMD = "python -m unittest discover -s tests/unit -t ."
_IT_CMD = "python -m unittest discover -s tests/integration -t ."


def _documents(text: str) -> list[str]:
    return [d for d in text.split("\n---\n") if d.strip()]


def _deployment_names() -> set[str]:
    """所有 `kind: Deployment` 文档的 `metadata.name`（2 空格缩进那一行）。"""
    names: set[str] = set()
    for path in sorted(K8S.glob("*.yaml")):
        for doc in _documents(path.read_text(encoding="utf-8")):
            if not re.search(r"^kind:\s*Deployment\s*$", doc, re.M):
                continue
            m = re.search(r"^  name:\s*(\S+)", doc, re.M)
            if m:
                names.add(m.group(1))
    return names


def _hpa_docs() -> list[str]:
    return [
        doc
        for path in sorted(K8S.glob("*.yaml"))
        for doc in _documents(path.read_text(encoding="utf-8"))
        if re.search(r"^kind:\s*HorizontalPodAutoscaler\s*$", doc, re.M)
    ]


def _hpa_targets() -> set[str]:
    return {
        m.group(1)
        for doc in _hpa_docs()
        for m in re.finditer(r"scaleTargetRef:.*?name:\s*(\S+)", doc, re.S)
    }


class TestTheScannersReallyFindThings(unittest.TestCase):
    """控制组：扫不到东西的话，下面每条都是"因为空集合才通过"的。"""

    def test_the_ci_workflow_exists(self) -> None:
        self.assertTrue(CI.is_file(), f"missing {CI.relative_to(ROOT)}")

    def test_the_hpa_manifest_exists(self) -> None:
        self.assertTrue((K8S / "07-hpa.yaml").is_file())

    def test_deployments_are_found(self) -> None:
        self.assertGreaterEqual(len(_deployment_names()), 6)

    def test_hpas_are_found(self) -> None:
        self.assertGreaterEqual(len(_hpa_docs()), 2)


class TestThePipelineRunsTheRealCommands(unittest.TestCase):
    """B-7：CI 跑的那条命令，必须和开发者手跑的是**同一句**。"""

    def setUp(self) -> None:
        self.text = CI.read_text(encoding="utf-8")

    def test_it_runs_the_unit_suite(self) -> None:
        self.assertIn(
            _UNIT_CMD,
            self.text,
            "CI must run the exact unit command — a different path would "
            "silently test nothing (or the wrong thing)",
        )

    def test_it_runs_the_integration_suite_against_a_real_postgres(self) -> None:
        self.assertIn(_IT_CMD, self.text)
        self.assertIn("AGENTOS_PG_DSN", self.text)
        self.assertIn("pgvector/pgvector:pg16", self.text)

    def test_it_refuses_to_pass_when_integration_was_skipped(self) -> None:
        """没有 PG 时集成层会**整层跳过并报绿** —— 假绿比红危险。"""
        self.assertIn(
            "skipped=",
            self.text,
            "the integration job must fail when tests were skipped, otherwise "
            "a broken DB connection reads as a passing pipeline",
        )

    def test_it_checks_the_deploy_manifest(self) -> None:
        self.assertIn("apps.cli manifest check", self.text)

    def test_it_builds_the_image(self) -> None:
        self.assertIn("docker build -f deploy/Dockerfile", self.text)


class TestTheHpaScalesRealDeployments(unittest.TestCase):
    def test_it_scales_the_api_and_the_worker(self) -> None:
        self.assertEqual(_hpa_targets(), {"agentos-api", "agentos-worker"})

    def test_every_target_is_a_deployment_that_exists(self) -> None:
        """HPA 指向一个不存在的 Deployment，只会安静地不工作。"""
        known = _deployment_names()
        for target in sorted(_hpa_targets()):
            with self.subTest(target=target):
                self.assertIn(target, known, f"HPA targets unknown Deployment {target!r}")

    def test_every_hpa_is_autoscaling_v2_on_cpu(self) -> None:
        for doc in _hpa_docs():
            name = re.search(r"^  name:\s*(\S+)", doc, re.M)
            with self.subTest(hpa=name.group(1) if name else "?"):
                self.assertIn("apiVersion: autoscaling/v2", doc)
                self.assertIn("type: Resource", doc)
                self.assertIn("averageUtilization:", doc)

    def test_min_replicas_is_below_max_replicas(self) -> None:
        for doc in _hpa_docs():
            name = re.search(r"^  name:\s*(\S+)", doc, re.M)
            lo = int(re.search(r"minReplicas:\s*(\d+)", doc).group(1))
            hi = int(re.search(r"maxReplicas:\s*(\d+)", doc).group(1))
            with self.subTest(hpa=name.group(1) if name else "?"):
                self.assertLess(lo, hi, "an HPA that cannot scale is not an HPA")


class TestTheApiRollsWithoutDowntime(unittest.TestCase):
    def test_it_uses_a_rolling_update_that_never_drops_the_last_pod(self) -> None:
        text = (K8S / "04-api.yaml").read_text(encoding="utf-8")
        self.assertIn("type: RollingUpdate", text)
        self.assertIn(
            "maxUnavailable: 0",
            text,
            "with a single replica, the default maxUnavailable would drop the "
            "only endpoint before the replacement is ready",
        )
        self.assertIn("maxSurge: 1", text)


class TestTheHpaIsNotMistakenForAWorkload(unittest.TestCase):
    """M75 的探活判据只看"引用 apps. 且 replicas>0 的文档" —— HPA 不是。"""

    def test_the_hpa_does_not_look_like_a_running_process(self) -> None:
        for doc in _hpa_docs():
            self.assertNotIn("apps.", doc)
            self.assertNotRegex(doc, r"^\s*replicas:\s*\d+", re.M)


if __name__ == "__main__":
    unittest.main()
