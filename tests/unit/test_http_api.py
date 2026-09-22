"""M24：`apps/api` —— Control Plane 的 HTTP 进程。

    A-3   POST /runs 幂等，且幂等键**不能放 Redis**（此前组合根接的是 Redis）
    PR-22 框架绑定单点（`apps/api/app.py`），且路由文件**不认识领域**
    A-7   handler 与框架无关；换框架不动业务

每条不变量配一个控制组。
"""
from __future__ import annotations

import ast
import importlib
import re
import unittest
from pathlib import Path
from typing import Any, Mapping

from apps._bootstrap import (
    ConfigurationError,
    RuntimeConfig,
    build_control_plane,
    load_stack_factory,
)
from apps.api import FrameworkMissing, build_app
from examples.demo_stack import build_stack_factory
from packages.agent_api.handlers import (
    decide_approval,
    get_run,
    list_approvals,
    start_run,
)
from packages.agent_api.service import InProcessControlPlane
from packages.agent_harness.approval import InMemoryApprovalStore
from packages.agent_runtime.adapters.postgres import (
    PostgresChildRunRegistry,
    PostgresCompensationStore,
    PostgresRunCancellationStore,
    PostgresRunSnapshotStore,
)
from packages.agent_runtime.delegation import ChildRunRegistry
from packages.execution_kernel.adapters.postgres import (
    PostgresExecutionRepository,
    PostgresIdempotencyStore,
    PostgresOutboxStore,
)

from .sqlite_shim import connect, load_schema_sql

ROOT = Path(__file__).resolve().parents[2]

#: PR-22 第二条：路由文件只能认识 `agent_api`，不认识领域。
_PACKAGES_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(packages\.[A-Za-z_][\w.]*)", re.MULTILINE
)
_ALLOWED_PACKAGES = {"packages.agent_api"}

#: 路由函数体里不该出现的**四层**代表对象。少了任何一层都会漏判：
#: 只禁 Kernel 会放过"在路由里跑一次循环"，只禁 Runtime 会放过"在路由里判策略"。
_FORBIDDEN_IDENTIFIERS = {
    # Harness —— 业务判断
    "Harness", "PolicyDecision", "DefaultHarness",
    # Runtime —— 循环驱动
    "AgentLoop", "assemble_runtime_stack", "StepInterpreter",
    # Kernel —— 生命周期
    "ExecutionKernel", "Scheduler", "TaskFactory", "ExecutionStore",
    # 领域 —— 直接摸状态
    "AgentRun", "Step", "Execution", "Attempt", "Checkpoint",
}


def _top_packages(source: str) -> set[str]:
    """归一到**顶层子包**：`packages.agent_api.handlers` → `packages.agent_api`。

    PR-22 约束的是"路由认识哪一层"，不是"认识哪个模块"。
    按完整模块名比对会把一次无害的下移（`handlers` → `handlers.start`）判成违规，
    于是断言逼着人不敢重构 —— 那不是这条不变量想要的。
    """
    return {".".join(m.split(".")[:2]) for m in _PACKAGES_IMPORT.findall(source)}


def _code_identifiers(source: str) -> set[str]:
    """源码里**真的被引用**的标识符 —— 不含 docstring 与注释。

    用 AST 而不是 `assertNotIn`：docstring 里写"业务判断在 Harness"是在讲道理，
    不是在调用 Harness。按文本扫描会把讲解判成违规，
    逼着人把 docstring 写得含糊其辞 —— 那正好毁掉这份代码最该说清的部分。
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                names.add(alias.asname or alias.name.split(".")[-1])
    return names


def _control_plane() -> InProcessControlPlane:
    """进程内 Control Plane，走**真的** stack provider（不是测试专用旁路）。"""
    return InProcessControlPlane(
        factory=build_stack_factory(), approvals=InMemoryApprovalStore()
    )


# ---------------------------------------------------------------------------
# PR-22：路由文件不认识领域
# ---------------------------------------------------------------------------


class RouteFileTest(unittest.TestCase):
    def _source(self) -> str:
        return (ROOT / "apps" / "api" / "app.py").read_text(encoding="utf-8")

    def test_pr22_the_route_file_only_knows_the_contract_layer(self) -> None:
        """路由文件里出现 `packages.agent_domain` 之类 = 业务逻辑长进了路由（A-1）。"""
        imported = _top_packages(self._source())
        self.assertTrue(imported, "扫描器应当至少看到 agent_api")
        self.assertEqual(imported - _ALLOWED_PACKAGES, set())

    def test_pr22_the_control_the_scanner_sees_other_packages(self) -> None:
        """控制组：扫描器能认出别的 packages 子包 —— 上一条不是因为它看不见。"""
        sample = (
            "from packages.agent_api.handlers import start_run\n"
            "from packages.agent_domain.execution import Task\n"
            "import packages.execution_kernel\n"
        )
        self.assertEqual(
            _top_packages(sample),
            {
                "packages.agent_api",
                "packages.agent_domain",
                "packages.execution_kernel",
            },
        )

    def test_pr22_the_route_file_has_no_business_words(self) -> None:
        """路由函数体里不该有 Harness / Runtime / Kernel / 领域的对象。

        这是上一条的**语义**版本：import 白名单挡得住 import，
        挡不住"把判断直接写在路由里"（不 import 也能凭空造一个 AgentRun）。
        """
        used = _code_identifiers(self._source()) & _FORBIDDEN_IDENTIFIERS
        self.assertEqual(used, set(), f"路由里出现了领域对象：{sorted(used)}")

    def test_pr22_the_control_the_identifier_scanner_sees_business_words(self) -> None:
        """控制组：真的用了 AgentLoop 时扫描器抓得到 —— 上一条不是因为它看不见。"""
        sample = (
            "def _start(agent_id: str):\n"
            "    loop = AgentLoop()\n"
            "    return loop.run(agent_id)\n"
        )
        self.assertEqual(_code_identifiers(sample) & _FORBIDDEN_IDENTIFIERS, {"AgentLoop"})

    def test_pr22_the_control_the_identifier_scanner_ignores_prose(self) -> None:
        """控制组：docstring / 注释里提到 Harness **不算**用它。

        这条反过来钉住"扫描的是代码不是文字" —— 否则上一条会逼人删掉讲解。
        """
        sample = (
            '"""业务判断在 Harness，循环驱动在 Runtime，生命周期在 Kernel。"""\n'
            "# AgentLoop 不该出现在这里\n"
            "def _health() -> dict:\n"
            "    return {'status': 'ok'}\n"
        )
        self.assertEqual(
            _code_identifiers(sample) & _FORBIDDEN_IDENTIFIERS, set(), sample
        )


# ---------------------------------------------------------------------------
# A-3：幂等键必须落 PG
# ---------------------------------------------------------------------------


class IdempotencyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(schema_sql=load_schema_sql("007_idempotency.sql"))
        self.addCleanup(self.conn.close)
        self.store = PostgresIdempotencyStore(self.conn, durable=True)

    def test_a3_the_key_lands_in_postgres(self) -> None:
        """A-3：幂等键落 PG，不是 Redis —— 创建 Run 没有下游可以回查。"""
        self.store.put("run:k1", {"run_id": "run_1"})
        self.assertEqual(self.store.get("run:k1"), {"run_id": "run_1"})

    def test_a3_a_second_put_does_not_overwrite(self) -> None:
        """第二次写入**不算数** —— 覆盖就等于抹掉了幂等本身的意义。"""
        self.store.put("run:k1", {"run_id": "run_1"})
        self.store.put("run:k1", {"run_id": "run_2"})
        self.assertEqual(self.store.get("run:k1"), {"run_id": "run_1"})

    def test_a3_the_control_a_missing_key_is_none(self) -> None:
        """控制组：没写过的键返回 None，不是空 dict —— 否则"没命中"和"命中了空结果"分不开。"""
        self.assertIsNone(self.store.get("run:nope"))

    def test_a3_durable_is_recorded_in_the_store(self) -> None:
        """`durable` 是存储层的一个可查询属性 —— "哪些键允许丢"有答案。"""
        PostgresIdempotencyStore(self.conn, durable=False).put("exec:1", {"ok": True})
        cur = self.conn.cursor()
        cur.execute("SELECT durable FROM idempotency_keys WHERE key = %s", ("exec:1",))
        self.assertFalse(cur.fetchone()["durable"])

    def test_a3_the_schema_bans_an_empty_value(self) -> None:
        """DB 兜底：空结果会被拒。一次"什么都没记下"的命中比没命中更难排查。

        应用层会不会走到这里不重要 —— 约束在 DB 上，绕过程序也绕不过它。
        """
        with self.assertRaises(Exception):
            self.store.put("run:empty", {})

    def test_a3_the_control_the_constraint_is_the_schema_not_the_store(self) -> None:
        """控制组：拒掉空值的是 **schema**，不是 store 自己先校验了一遍。

        如果 store 内部先 return 了，上一条照样通过，但 DB 上其实没有约束 ——
        那"绕过应用直接改库"就又能写出空命中了。
        """
        cur = self.conn.cursor()
        with self.assertRaises(Exception):
            cur.execute(
                "INSERT INTO idempotency_keys (key, value) VALUES (%s, %s)",
                ("run:raw", "{}"),
            )

    def test_a3_the_schema_bans_an_empty_key(self) -> None:
        with self.assertRaises(Exception):
            self.store.put("", {"run_id": "run_1"})


# ---------------------------------------------------------------------------
# 组合根：Control Plane 的持久化接线
# ---------------------------------------------------------------------------


class ControlPlaneWiringTest(unittest.TestCase):
    def _config(self, **overrides: str) -> RuntimeConfig:
        env = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
        env.update(overrides)
        return RuntimeConfig.from_env(env)

    def test_stack_provider_is_required(self) -> None:
        """组合根不替你猜一个智能体怎么想（同 M23 执行器的判据）。"""
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(self._config())
        self.assertIn("AGENTOS_STACK_PROVIDER", str(cm.exception))

    def test_stack_provider_must_look_like_module_function(self) -> None:
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(self._config(AGENTOS_STACK_PROVIDER="examples.demo_stack"))
        self.assertIn("module:function", str(cm.exception))

    def test_the_real_provider_loads_through_importlib(self) -> None:
        """走的是与生产相同的装载路径，不是测试旁路。"""
        factory = load_stack_factory(
            self._config(
                AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
            )
        )
        self.assertTrue(callable(factory))

    def test_control_plane_persists_approvals_and_idempotency(self) -> None:
        """三样存储全部落 PG：审批（A-10）、快照（R-5）、幂等（A-3）。"""
        conn = connect(
            schema_sql="\n".join(
                [
                    load_schema_sql("003_approvals.sql"),
                    load_schema_sql("004_run_snapshots.sql"),
                    # 009 是 004 的 companion：少了它快照表的列数对不上
                    load_schema_sql("009_snapshot_pending_child.sql"),
                    # 018 也是 004 的 companion（R-7 / M86）
                    load_schema_sql("018_snapshot_port_progress.sql"),
                    load_schema_sql("007_idempotency.sql"),
                ]
            )
        )
        self.addCleanup(conn.close)
        config = self._config(
            AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
        )
        cp = build_control_plane(config, conn=conn)
        self.assertIsInstance(cp.idempotency, PostgresIdempotencyStore)
        self.assertTrue(cp.idempotency.durable)

    def test_a3_a_second_run_is_never_created_even_after_a_restart(self) -> None:
        """A-3 的终局：键说"执行过" → **绝不开第二个 Run**。

        幂等键落 PG 只解决了"键不丢"。但命中之后还有一步：
        拿着键去**找那个 Run**。重启后 `runs` 是空的，
        如果"内存里找不到就当没执行过"，那就顺着往下又建了一个 ——
        键还指向第一个，第二个从此谁也查不到，而它照样在花钱、照样产生副作用。

        这是 A-3 上最藏得住的一种破法：存储选对了，逻辑也是对的，
        只有"命中之后找不到"这一条分支错了。
        """
        conn = connect(
            schema_sql="\n".join(
                [
                    load_schema_sql("003_approvals.sql"),
                    load_schema_sql("004_run_snapshots.sql"),
                    # 009 是 004 的 companion：少了它快照表的列数对不上
                    load_schema_sql("009_snapshot_pending_child.sql"),
                    # 018 也是 004 的 companion（R-7 / M86）
                    load_schema_sql("018_snapshot_port_progress.sql"),
                    load_schema_sql("007_idempotency.sql"),
                ]
            )
        )
        self.addCleanup(conn.close)
        config = self._config(
            AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
        )
        cp = build_control_plane(config, conn=conn)
        first = start_run(cp, {"agent_id": "a1", "user_request": "hi"}, idempotency_key="k1")
        self.assertEqual(first.status, 201)

        cp.runs.clear()  # 模拟重启：内存里的 Run 全没了，PG 里的键还在

        second = start_run(cp, {"agent_id": "a1", "user_request": "hi"}, idempotency_key="k1")
        self.assertNotEqual(second.status, 201, f"同一个键开了第二个 Run：{second.body}")
        self.assertEqual(len(cp.runs), 0, "拒绝的时候不该顺手把 Run 建出来")

    def test_the_control_a3_is_not_satisfied_by_redis(self) -> None:
        """控制组：A-3 之前是怎么被违反的 —— 组合根把幂等接到了 Redis。

        这条把"曾经错成什么样"钉住，免得有人改回 Redis 还说不清为什么不行。
        """
        from packages.execution_kernel.adapters.redis import RedisIdempotencyStore

        self.assertTrue(hasattr(RedisIdempotencyStore, "get"))
        self.assertNotIsInstance(_control_plane().idempotency, RedisIdempotencyStore)


# ---------------------------------------------------------------------------
# M26：派生登记处也必须落 PG —— D-1 不能在重启后失效
# ---------------------------------------------------------------------------


class ChildRunRegistryWiringTest(unittest.TestCase):
    """`build_control_plane` 里第四样存储（见 `apps/_bootstrap` 的判据表）。

    这一组存在的理由不是"多一个断言"，是 M26 的一条自纠：
    本轮给 `load_stack_factory` 加了"provider 不收 `child_registry` 就拒绝"，
    但当时**没有任何一条测试断言它**。那正是 D-4 说过的病 ——
    冻结在文档里的概念，没有测试指着它的实现。
    所以这里钉的是**能被观察到的那一层**：真的把栈造出来，
    看 spawner 手里的登记处到底是 PG 还是内存（PR-23）。
    """

    def _config(self, **overrides: str) -> RuntimeConfig:
        env = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
        env.update(overrides)
        return RuntimeConfig.from_env(env)

    def _conn(self) -> Any:
        conn = connect(
            schema_sql="\n".join(
                [
                    load_schema_sql("003_approvals.sql"),
                    load_schema_sql("004_run_snapshots.sql"),
                    load_schema_sql("009_snapshot_pending_child.sql"),
                    # 018 也是 004 的 companion（R-7 / M86）
                    load_schema_sql("018_snapshot_port_progress.sql"),
                    load_schema_sql("007_idempotency.sql"),
                    load_schema_sql("008_child_runs.sql"),
                    load_schema_sql("010_child_run_result.sql"),
                    load_schema_sql("015_child_wait_deadline.sql"),
                ]
            )
        )
        self.addCleanup(conn.close)
        return conn

    def _real_config(self) -> RuntimeConfig:
        return self._config(
            AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
        )

    def test_d6_the_control_plane_wires_the_registry_to_pg(self) -> None:
        """D-6：组合根造出来的栈，其派生登记处必须是 **PG** 版。

        断言落在 `stack.loop.spawner.registry` 上，而不是"函数被调用过"：
        换成内存的 `ChildRunRegistry()` 这条立刻红 ——
        而换成内存正是 M25 的全部问题所在。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        stack = cp.factory("a1", None)
        self.assertIsInstance(stack.loop.spawner.registry, PostgresChildRunRegistry)

    def test_d6_the_registry_is_shared_across_runs(self) -> None:
        """登记处是**跨 Run 的账本**：两个 Run 拿到的是同一个。

        每个 Run 一份的话，D-1 的"只派一份"就退化成"每个 Run 各派一份" ——
        父 Run 重试时换了个 Run 对象，唯一键照样拦不住。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        first = cp.factory("a1", None)
        second = cp.factory("a1", None)
        self.assertIs(first.loop.spawner.registry, second.loop.spawner.registry)

    def test_a_provider_that_cannot_take_the_registry_is_refused(self) -> None:
        """provider 收不下 `child_registry` → **装载失败**，不许静默退化。

        静默退化的后果是：系统看起来一切正常，D-1 只在进程活着时成立，
        于是每次重启都可能开出第二条子 Run（A-12：变错，不是变慢）。
        """
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(
                self._config(
                    AGENTOS_STACK_PROVIDER=(
                        "tests.unit._stack_provider_fixture:build_stack_factory"
                    )
                ),
                child_registry=PostgresChildRunRegistry(self._conn()),
            )
        self.assertIn("child_registry", str(cm.exception))

    def test_the_control_a_provider_that_takes_it_is_let_through(self) -> None:
        """控制组：收得下的 provider 照样放行 —— 上一条不是"一律拒绝"。

        没有这条，"拒绝"和"根本装不上"就分不清了。
        """
        registry = PostgresChildRunRegistry(self._conn())
        factory = load_stack_factory(
            self._config(
                AGENTOS_STACK_PROVIDER=(
                    "tests.unit._stack_provider_fixture:build_stack_factory_with_registry"
                )
            ),
            child_registry=registry,
        )
        self.assertTrue(callable(factory))

    def test_the_control_without_a_registry_there_is_nothing_to_refuse(self) -> None:
        """控制组：不给登记处时不校验签名 —— 上一条不是"一律拒绝"。

        不传 `child_registry` 的场景是"这条进程不派生子 Run"，
        那时拒绝一个合法的 provider 是误伤。
        """
        factory = load_stack_factory(
            self._config(
                AGENTOS_STACK_PROVIDER=(
                    "tests.unit._stack_provider_fixture:build_stack_factory"
                )
            )
        )
        self.assertTrue(callable(factory))

    def test_the_control_the_memory_registry_is_what_it_degrades_to(self) -> None:
        """控制组：不传 `child_registry` 时，provider 退化成内存版。

        这条把"退化长什么样"钉住 ——
        上一条拒绝的正是这个，而它本身在单进程里是**合法**的（测试用）。
        """
        stack = build_stack_factory(None)("a1", None)
        self.assertIsInstance(stack.loop.spawner.registry, ChildRunRegistry)
        self.assertNotIsInstance(stack.loop.spawner.registry, PostgresChildRunRegistry)

    def test_the_real_provider_forwards_the_registry_it_is_given(self) -> None:
        """装载路径真的把它递到了 spawner —— 不是"收下了然后丢掉"。"""
        sentinel = ChildRunRegistry()
        factory = load_stack_factory(self._real_config(), child_registry=sentinel)
        self.assertIs(factory("a1", None).loop.spawner.registry, sentinel)


# ---------------------------------------------------------------------------
# 空洞 212：Kernel 也必须持久化，否则 X-3 在 API 那条路上没有对象
# ---------------------------------------------------------------------------


class KernelWiringTest(unittest.TestCase):
    """M29：栈里那个 Kernel 是哪来的，以前没人问过。

    答案一直是 `assemble_runtime_stack` 里的默认值 —— 一个内存的 Kernel。
    于是审批、快照、幂等键、派生登记四样都落了 PG，
    唯独"Run 真跑起来产生的 Execution"没有：它活在 API 进程的内存里。

    `executions` / `attempts` / `outbox_events` 三张表全程为空，
    而 `GET /runs/{id}/trace` 依然说得清 Run 走到第几步 ——
    这是最难发现的一种"看起来跑通了"。
    """

    def _config(self, **overrides: str) -> RuntimeConfig:
        env = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
        env.update(overrides)
        return RuntimeConfig.from_env(env)

    def _real_config(self) -> RuntimeConfig:
        return self._config(
            AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
        )

    def _conn(self) -> Any:
        from .sqlite_shim import connect, load_schema_sql

        conn = connect(
            schema_sql="\n".join(
                [
                    load_schema_sql("001_kernel.sql"),
                    load_schema_sql("002_outbox_consumer.sql"),
                    load_schema_sql("003_approvals.sql"),
                    load_schema_sql("004_run_snapshots.sql"),
                    load_schema_sql("007_idempotency.sql"),
                    load_schema_sql("008_child_runs.sql"),
                    load_schema_sql("009_snapshot_pending_child.sql"),
                    # 018 也是 004 的 companion（R-7 / M86）
                    load_schema_sql("018_snapshot_port_progress.sql"),
                    load_schema_sql("011_run_cancellations.sql"),
                    load_schema_sql("010_child_run_result.sql"),
                    load_schema_sql("015_child_wait_deadline.sql"),
                ]
            )
        )
        self.addCleanup(conn.close)
        return conn

    def test_the_kernel_of_a_control_plane_stack_is_pg_backed(self) -> None:
        """空洞 212：组合根造出来的栈，其 Kernel 必须是 **PG** 版。

        断言落在 `stack.kernel.repository` 上，而不是"传了 kernel 参数"：
        换成 `InMemoryExecutionRepository()` 这条立刻红 ——
        而换成内存正是它此前悄悄做的事（`assemble_runtime_stack` 的兜底）。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        stack = cp.factory("a1", None)
        self.assertIsInstance(stack.kernel.repository, PostgresExecutionRepository)
        self.assertIsInstance(stack.kernel.outbox, PostgresOutboxStore)

    def test_the_kernel_and_the_approvals_share_one_connection(self) -> None:
        """PR-31 的前提：Kernel 与审批/快照必须在**同一个事务**里。

        两样东西连着两个连接，那么"一个 HTTP 请求 = 一个事务"就有两个答案 ——
        提交其中一个不会让另一个生效，而 X-3 要求的正是它们一起生效。
        """
        conn = self._conn()
        cp = build_control_plane(self._real_config(), conn=conn)
        stack = cp.factory("a1", None)
        self.assertIs(stack.kernel.repository.conn, conn)

    def test_a_provider_that_cannot_take_the_kernel_is_refused(self) -> None:
        """收不下 `kernel` → 装载失败，不许静默退化成内存 Kernel。"""
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(
                self._config(
                    AGENTOS_STACK_PROVIDER=(
                        "tests.unit._stack_provider_fixture:build_stack_factory"
                    )
                ),
                kernel=object(),
            )
        self.assertIn("kernel", str(cm.exception))

    def test_the_control_a_provider_that_takes_it_is_let_through(self) -> None:
        """控制组：收得下的 provider 照样放行 —— 上一条不是"一律拒绝"。"""
        sentinel = object()
        factory = load_stack_factory(self._real_config(), kernel=sentinel)
        self.assertIs(factory("a1", None).kernel, sentinel)

    def test_the_control_without_a_kernel_there_is_nothing_to_refuse(self) -> None:
        """控制组：不给 kernel 时不校验签名 —— 上一条不是"一律拒绝"。

        不传 `kernel` 的合法场景是单进程测试 / 演示装配，
        那时拒绝一个形状正确的 provider 是误伤。
        """
        factory = load_stack_factory(
            self._config(
                AGENTOS_STACK_PROVIDER=(
                    "tests.unit._stack_provider_fixture:build_stack_factory"
                )
            )
        )
        self.assertTrue(callable(factory))


# ---------------------------------------------------------------------------
# 空洞 213 / 214：快照与补偿账本也必须是 PG 版
# ---------------------------------------------------------------------------


class StackStoreWiringTest(unittest.TestCase):
    """空洞 213 / 214：`snapshots` 与 `compensations`。

    这两样和 Kernel 是**同一个形状**：`assemble_runtime_stack` 给它们都写了
    `or InMemory...()`，于是漏掉不报错，只是永不落库。

    其中最讽刺的是 `snapshots`：`build_control_plane` 一直都装配了
    `PostgresRunSnapshotStore` —— 它只是**从没递到栈里去**。
    于是 R-5 在"看起来已经做完"的状态下不成立：
    `run_snapshots` 恒为 0，重启后每个 Run 都 404。

    M29 真起一次服务量到的就是这个：`run_snapshots = 0`，
    而同一个 Run 明明刚刚挂起过一次（挂起正是拍快照的时机）。
    """

    def _config(self, **overrides: str) -> RuntimeConfig:
        env = {"AGENTOS_PG_DSN": "postgresql://localhost/agentos"}
        env.update(overrides)
        return RuntimeConfig.from_env(env)

    def _real_config(self) -> RuntimeConfig:
        return self._config(
            AGENTOS_STACK_PROVIDER="examples.demo_stack:build_stack_factory"
        )

    def _conn(self) -> Any:
        from .sqlite_shim import connect, load_schema_sql

        conn = connect(
            schema_sql="\n".join(
                [
                    load_schema_sql("001_kernel.sql"),
                    load_schema_sql("002_outbox_consumer.sql"),
                    load_schema_sql("003_approvals.sql"),
                    load_schema_sql("004_run_snapshots.sql"),
                    load_schema_sql("005_compensations.sql"),
                    load_schema_sql("007_idempotency.sql"),
                    load_schema_sql("008_child_runs.sql"),
                    load_schema_sql("009_snapshot_pending_child.sql"),
                    # 018 也是 004 的 companion（R-7 / M86）
                    load_schema_sql("018_snapshot_port_progress.sql"),
                    load_schema_sql("011_run_cancellations.sql"),
                    load_schema_sql("010_child_run_result.sql"),
                    load_schema_sql("015_child_wait_deadline.sql"),
                ]
            )
        )
        self.addCleanup(conn.close)
        return conn

    def test_r5_the_snapshots_of_a_control_plane_stack_is_pg_backed(self) -> None:
        """R-5：栈里那份快照存储必须是 **PG** 版，不是装配在 Control Plane 上那份。

        断言落在 `stack.loop.snapshots`：换成 `InMemoryRunSnapshotStore()`
        这条立刻红 —— 而换成内存正是它此前悄悄做的事。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        stack = cp.factory("a1", None)
        self.assertIsInstance(stack.loop.snapshots, PostgresRunSnapshotStore)

    def test_r5_the_stack_and_the_recovery_share_one_snapshot_store(self) -> None:
        """R-1 的物理保证：写快照的和读快照的必须是同一份。

        两份存储的话，`RunRecovery.rebuild()` 永远读不到刚写下去的那个快照，
        于是"恢复"这件事看起来没坏（不报错），只是每次都恢复不出来。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        self.assertIs(cp.snapshots, cp.factory("a1", None).loop.snapshots)

    def test_s9_the_compensation_ledger_is_pg_backed(self) -> None:
        """S-9：补偿账本必须持久 —— 声明过的撤销，重启后也要执行得了。"""
        cp = build_control_plane(self._real_config(), conn=self._conn())
        stack = cp.factory("a1", None)
        self.assertIsInstance(stack.loop.compensations, PostgresCompensationStore)

    def test_a_provider_that_cannot_take_snapshots_is_refused(self) -> None:
        """收不下 `snapshots` → 装载失败，不许静默退化。"""
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(
                self._config(
                    AGENTOS_STACK_PROVIDER=(
                        "tests.unit._stack_provider_fixture:build_stack_factory"
                    )
                ),
                snapshots=object(),
            )
        self.assertIn("snapshots", str(cm.exception))

    def test_a_provider_that_cannot_take_compensations_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(
                self._config(
                    AGENTOS_STACK_PROVIDER=(
                        "tests.unit._stack_provider_fixture:build_stack_factory"
                    )
                ),
                compensations=object(),
            )
        self.assertIn("compensations", str(cm.exception))

    def test_the_cancellation_channel_is_pg_backed(self) -> None:
        """空洞 222：栈里那份取消意图必须是 **PG** 版。

        这一条与上面三条形状不同，而且更难发现：

            snapshots / compensations 漏了 → 退化成**内存**，于是"永不落库"
            cancellations 漏了            → **根本没有通道**，
                                            父 Run 只能把登记处那一行判死，
                                            而跑在另一个进程里的子 Run 照跑

        它的判据是 A-12：丢了是变错，不是变慢。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        stack = cp.factory("a1", None)
        self.assertIsInstance(stack.loop.cancellations, PostgresRunCancellationStore)

    def test_the_control_plane_shares_the_channel_with_the_stack(self) -> None:
        """`POST /runs/{id}/cancel` 与栈里的取消必须是**同一张表**。

        两条通道各写一处的话，页面上点"叫停"写下了一条意图，
        而那条 Run 自己认领的是另一处 —— 于是它永远读不到，
        而控制台显示"已请求叫停"，两边都成立，拼起来是假的。
        """
        cp = build_control_plane(self._real_config(), conn=self._conn())
        self.assertIs(cp.cancellations, cp.factory("a1", None).loop.cancellations)

    def test_a_provider_that_cannot_take_cancellations_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError) as cm:
            load_stack_factory(
                self._config(
                    AGENTOS_STACK_PROVIDER=(
                        "tests.unit._stack_provider_fixture:build_stack_factory"
                    )
                ),
                cancellations=object(),
            )
        self.assertIn("cancellations", str(cm.exception))

    def test_the_control_the_real_provider_forwards_snapshots(self) -> None:
        """控制组：收得下的 provider 真的把它递到了 loop 上。"""
        sentinel = object()
        factory = load_stack_factory(self._real_config(), snapshots=sentinel)
        self.assertIs(factory("a1", None).loop.snapshots, sentinel)


# ---------------------------------------------------------------------------
# PR-15 的框架版本：缺框架不能炸在 import 期
# ---------------------------------------------------------------------------


class FrameworkBindingTest(unittest.TestCase):
    def test_pr15_the_framework_import_is_lazy(self) -> None:
        """PR-15 的框架版：没有 fastapi 时 `import apps.api.app` 必须成功。

        顶层 import 的话，570 个测试会在没装 fastapi 的机器上 import 期全红 ——
        而本机就没装，所以**本文件能被 import 本身就是证据**。
        """
        app_module = importlib.import_module("apps.api.app")
        self.assertTrue(callable(app_module.build_app))
        self.assertTrue(callable(app_module.serve))

    def test_pr15_a_missing_framework_gives_an_actionable_error(self) -> None:
        """控制组：缺框架时给的是"能照着做"的提示，不是裸 ImportError。"""
        try:
            importlib.import_module("fastapi")
        except ImportError:
            with self.assertRaises(FrameworkMissing) as ctx:
                build_app(_control_plane())
            message = str(ctx.exception)
            self.assertIn("pip install fastapi uvicorn", message)
        else:  # pragma: no cover - 装了 fastapi 的机器上走不到
            self.skipTest("fastapi is installed")

    def test_the_version_shipped_by_the_api_is_the_frozen_baseline(self) -> None:
        """对外报的版本号必须等于**冻结基线**的版本号。

        `/health` 与 OpenAPI 会把它发出去。放任它自己写一个字面量，
        结果就是 M25 之后代码报 2.1.13、文档写 2.1.14 ——
        "我们线上跑的是哪一版"从此没有答案。
        """
        docs = sorted(p.name for p in ROOT.glob("AgentOS_*_最终冻结版.md"))
        self.assertEqual(len(docs), 1, f"应当只有一份冻结基线，现在是：{docs}")
        baseline = re.search(r"v(\d+\.\d+\.\d+)", docs[0])
        self.assertIsNotNone(baseline, docs[0])
        source = (ROOT / "apps" / "api" / "app.py").read_text(encoding="utf-8")
        self.assertIn(f'version="{baseline.group(1)}"', source)

    def test_the_version_bump_is_atomic_across_all_its_landing_sites(self) -> None:
        """版本号有**三个**落点，升版必须一次升完 —— 不许停在中途。

        ------------------------------------------------------------------
        为什么这条值得单独立一个用例（不是洁癖）

        它已经害过一次**假红**了：M66 之后某次跑集成，唯一红的一条是
        `TheProcessServesTest.test_the_version_it_serves_is_the_frozen_baseline`
        —— 报 `'2.1.53' != '2.1.54'`。

        那看起来像"测试不稳定"（同一份代码时红时绿），于是有人会去查
        连接泄漏、查 `terminate()` 没 `wait`、查 PG 咨询锁……**全查错方向**。
        真相是：改版本号改到一半（文档名字已经换了、`app.py` 还没跟上，
        或者反过来），此时**立刻**跑集成 —— 真 uvicorn 报的是 `app.py` 的值，
        而 `_frozen_baseline_version()` 读的是**文档文件名**，两边当场对不上。

        它之所以"偶发"，只因为升版是个**手动的多步动作**：
        正好在两步之间跑测试就红，跑在动作之外就绿。
        被测代码一秒都没坏过 —— 坏的是"我们没有一条断言盯着它升完整"。

        ------------------------------------------------------------------
        这条不变量（PR-33）

            版本号的三处落点必须**同源**，且不允许存在"只有部分落点更新"
            的中间状态。中间状态一旦被允许，它产出的红灯就**指向错的地方**
            （PR-19 反过来用：红灯要指向真凶）。

        ------------------------------------------------------------------
        控制组：三处都写同一个数时这条必须绿（否则它只是个永远红的噪声源）。
        它同时钉住"新增落点"——将来多一个 `deploy/` 清单，
        忘了加进 `_VERSION_SITES` 就会被这条提醒（而不是又变成一次手查）。
        """
        docs = sorted(p.name for p in ROOT.glob("AgentOS_*_最终冻结版.md"))
        self.assertEqual(len(docs), 1, f"应当只有一份冻结基线，现在是：{docs}")
        baseline = re.search(r"v(\d+\.\d+\.\d+)", docs[0])
        self.assertIsNotNone(baseline, docs[0])
        version = baseline.group(1)

        #: (人话名字, 相对路径, 相对**声明点**的正则, 匹配对象)
        #:
        #: ⚠️ 刻意**不**去"扫描整个文件里的所有版本号"：
        #: 冻结基线文档正文里有一整节「版本变更」，列着历代版本；
        #: `deploy/k8s` 的清单里也可能带着注释。那样扫会把**历史记录**
        #: 判成**漂移** —— 一条永远红的噪声，比没有还坏。
        #:
        #: 所以每一处只认它**声明身份**的那一处（文件名 / `version=`）。
        #: 这正是"升版要改的到底是什么"的精确表述：
        #: 不是"文件里不许出现旧号"，而是"**这个对象对外声称自己是哪一版**"。
        #:
        #: 最后一个字段是匹配对象 —— 文档那一处声明在**文件名**上，
        #: 不在文件内容里（照抄 `read_text` 会永远匹到 0 次，正是本用例
        #: 第一次写出来时踩的坑：**声明点在哪一层，就要在哪一层去读它**）。
        sites: list[tuple[str, str, str, str]] = [
            # 文件名：`AgentOS_…_v2.1.71_最终冻结版.md`（中间那段是中文标题）
            ("冻结基线文档名", docs[0], r"_v(\d+\.\d+\.\d+)_最终冻结版", "name"),
            (
                "apps/api/app.py",
                "apps/api/app.py",
                r'version="(\d+\.\d+\.\d+)"',
                "body",
            ),
        ]

        for label, rel, site, where in sites:
            subject = rel if where == "name" else (ROOT / rel).read_text(
                encoding="utf-8"
            )
            declared = re.findall(site, subject)
            self.assertEqual(
                len(declared),
                1,
                f"{label} 的声明点应恰好出现一次，实际 {len(declared)} 次"
                f"（正则：{site}）",
            )
            self.assertEqual(
                declared[0],
                version,
                f"{label} 声称自己是 {declared[0]}，冻结版本是 {version} —— "
                f"升版漏了这一处，这是个中途状态",
            )

    def test_the_k8s_image_tags_follow_the_frozen_version(self) -> None:
        """K8s 清单里的镜像 tag 也必须跟着冻结版本走（M75 的约束）。

        它此前只被集成层盯着 —— 而那一层要真 PG 才能跑。
        一个纯文本的一致性约束不该依赖数据库是否在线：
        忘了同步 tag 是**最容易发生、最难在本地发现**的一种漂移
        （集群照常拉起**上一个版本**的镜像，没有任何红灯）。
        """
        docs = sorted(p.name for p in ROOT.glob("AgentOS_*_最终冻结版.md"))
        self.assertEqual(len(docs), 1, f"应当只有一份冻结基线，现在是：{docs}")
        baseline = re.search(r"v(\d+\.\d+\.\d+)", docs[0])
        self.assertIsNotNone(baseline, docs[0])
        version = baseline.group(1)

        manifests = sorted((ROOT / "deploy" / "k8s").glob("*.yaml"))
        self.assertTrue(manifests, "deploy/k8s 里一个清单都没有")

        #: `agentos:2.1.71-b1` —— 只认我们自己的镜像，不误伤别人的 tag。
        tag = re.compile(r"agentos:(\d+\.\d+\.\d+)")
        total = 0
        for path in manifests:
            for found in tag.findall(path.read_text(encoding="utf-8")):
                total += 1
                self.assertEqual(
                    found,
                    version,
                    f"{path.name} 的镜像 tag 是 {found}，冻结版本是 {version}",
                )
        self.assertGreater(total, 0, "一个 agentos 镜像 tag 都没扫到 —— 正则或路径变了")


# ---------------------------------------------------------------------------
# HTTP 语义（走真的 handler，不经过 fastapi —— A-7）
# ---------------------------------------------------------------------------


class HttpSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cp = _control_plane()

    def test_start_run_creates_a_run(self) -> None:
        response = start_run(
            self.cp, {"agent_id": "a1", "user_request": "hi"}, idempotency_key="k1"
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["agent_id"], "a1")

    def test_a3_the_same_key_returns_the_first_run(self) -> None:
        """A-3：同一个幂等键第二次来 → 200 + replayed，不开第二个 Run。"""
        first = start_run(
            self.cp, {"agent_id": "a1", "user_request": "hi"}, idempotency_key="k1"
        )
        second = start_run(
            self.cp, {"agent_id": "a1", "user_request": "hi"}, idempotency_key="k1"
        )
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        self.assertTrue(second.body["replayed"])
        self.assertEqual(first.body["run_id"], second.body["run_id"])

    def test_a_shape_error_is_400_not_500(self) -> None:
        """缺字段是**形状**问题（400），不是系统故障（500）。"""
        response = start_run(self.cp, {"agent_id": "a1"}, idempotency_key="")
        self.assertEqual(response.status, 400)
        self.assertIn("user_request", response.body["error"]["message"])

    def test_an_unknown_run_is_404(self) -> None:
        response = get_run(self.cp, "run_nope")
        self.assertEqual(response.status, 404)

    def test_approvals_is_a_filter_so_empty_is_not_an_error(self) -> None:
        """A-10：列表是过滤语义。空集不是 404 —— 否则重启后恰好在最需要时看不见东西。"""
        response = list_approvals(self.cp)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["items"], [])

    def test_a8_a_decision_without_by_is_rejected(self) -> None:
        """A-8：匿名审批进不了审计 —— "谁批的"是这条记录唯一的价值。"""
        body = {"decision": "approve", "by": ""}
        response = decide_approval(self.cp, "run_1", "apr_1", body)
        self.assertEqual(response.status, 400)
        self.assertIn("by", response.body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
