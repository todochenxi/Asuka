"""M12 / I-21 变红验证：逐条拆掉 kind -> Action 的分派契约。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
RUN_TIMEOUT = 120
LOOP = ROOT / "packages" / "agent_runtime" / "loop.py"
BACKUP = ROOT / ".workbuddy-ai" / "tmp" / "red12.orig"

COUNT = re.compile(r"FAILED \((failures=(\d+))?(?:, )?(errors=(\d+))?\)")
TEST_ID = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)

_GATE = (
    "        node = self._plan_node_for_next_action()\n"
    "        if node is not None:\n"
    "            mismatch = _plan_node_action_reason(node, action)\n"
    "            if mismatch is not None:\n"
    "                self._declare_terminal(\n"
    "                    AgentRunStatus.FAILED,\n"
    "                    reason=f\"{PLAN_NODE_ACTION_MISMATCH}: {mismatch}\",\n"
    "                )\n"
    "                return self._record(StepOutcome.FAILED)"
)

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "M1",
        "删除 I-21 整道门，回到 kind 只做注释",
        _GATE,
        "        pass",
    ),
    (
        "M2",
        "让 action mismatch 判据永远返回 None",
        "            mismatch = _plan_node_action_reason(node, action)",
        "            mismatch = None",
    ),
    (
        "M3",
        "把 tool 节点错误地放宽成所有 Action",
        "    PlanNodeKind.TOOL: frozenset({ActionType.TOOL_CALL}),",
        "    PlanNodeKind.TOOL: EXECUTABLE_ACTION_TYPES,",
    ),
    (
        "M4",
        "把 human 节点错误地放宽成所有 Action",
        "    PlanNodeKind.HUMAN: frozenset({ActionType.HUMAN_APPROVAL, ActionType.ASK_USER}),",
        "    PlanNodeKind.HUMAN: EXECUTABLE_ACTION_TYPES,",
    ),
    (
        "M5",
        "从运行时自述能力中删掉 tool/human",
        "SUPPORTED_PLAN_NODE_KINDS: frozenset[PlanNodeKind] = frozenset(\n"
        "    {\n"
        "        PlanNodeKind.TASK,\n"
        "        PlanNodeKind.TOOL,\n"
        "        PlanNodeKind.HUMAN,\n"
        "    }\n"
        ")",
        "SUPPORTED_PLAN_NODE_KINDS: frozenset[PlanNodeKind] = frozenset({PlanNodeKind.TASK})",
    ),
    (
        "M6",
        "让节点查询永远返回 None，绕过分派门",
        "        return self._next_plan_node(plan)\n\n    def _ensure_step",
        "        return None\n\n    def _ensure_step",
    ),
]


def run_suite() -> tuple[int, int, list[str]]:
    try:
        proc = subprocess.run(
            [PY, "-u", "-m", "unittest", "discover", "-s", "tests/unit", "-t", "."],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONPATH": "."},
            timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -2, -2, ["<超时 —— 变异让用例转不完>"]
    output = proc.stdout + proc.stderr
    match = COUNT.search(output)
    if match:
        return int(match.group(2) or 0), int(match.group(4) or 0), sorted(set(TEST_ID.findall(output)))
    if re.search(r"^OK$", output, re.MULTILINE):
        return 0, 0, []
    return -1, -1, ["<看不出来 —— 没有 OK 也没有 FAILED>"]


def main() -> int:
    print("基线（未变异）", flush=True)
    failures, errors, ids = run_suite()
    print(f"failures={failures} errors={errors}", flush=True)
    if failures != 0 or errors != 0:
        print("⚠️ 基线就不绿，拒绝开始变异。", flush=True)
        print("\n".join(ids[:10]), flush=True)
        return 1

    original = LOOP.read_text(encoding="utf-8")
    BACKUP.mkdir(parents=True, exist_ok=True)
    backup_file = BACKUP / LOOP.name
    backup_file.write_text(original, encoding="utf-8")
    results: list[tuple[str, int, int]] = []

    try:
        for name, description, old, new in MUTATIONS:
            current = LOOP.read_text(encoding="utf-8")
            hits = current.count(old)
            if hits != 1:
                print(f"{name}: ⚠️ SKIP anchor_hits={hits} (expected 1) - {description}", flush=True)
                results.append((name, -1, -1))
                continue
            LOOP.write_text(current.replace(old, new, 1), encoding="utf-8")
            try:
                failures, errors, ids = run_suite()
            finally:
                LOOP.write_text(original, encoding="utf-8")
            print(
                f"{name}: {'★ RED' if failures + errors > 0 else '⚠️ NOT RED'} "
                f"failures={failures} errors={errors} - {description}",
                flush=True,
            )
            for test_id in ids[:4]:
                print(f"  {test_id}", flush=True)
            results.append((name, failures, errors))
    finally:
        if LOOP.read_text(encoding="utf-8") != original:
            LOOP.write_text(original, encoding="utf-8")
            print("⚠️ loop.py residual mutation restored from disk backup", flush=True)

    red = sum(1 for _, failures, errors in results if failures + errors > 0)
    silent = sum(1 for _, failures, errors in results if failures == 0 and errors == 0)
    skipped = sum(1 for _, failures, errors in results if failures < 0)
    print(f"summary: {red}/{len(MUTATIONS)} red, {silent} silent, {skipped} skipped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
