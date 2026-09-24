"""组合根（M22 / §60）：唯一允许 import 真实客户端的地方。

--------------------------------------------------------------------------
为什么需要单独一个文件

M21 建好了四个进程，但它们都只是**类** —— 构造函数要的是 Port，
谁去 new 出那些 PG / Redis / Kafka 客户端，没人回答。
于是：

    python -m apps.outbox_publisher
    → ModuleNotFoundError: No module named 'apps.outbox_publisher.__main__'

一个进程类不是进程，正如一个 `drain()` 方法不是一个进程。差的就是这一段
"把接口接到真实实现上"的代码，而这段代码必须**只有一个**：

    PR-14  组合根唯一。除了本文件，任何模块里出现
           `import psycopg` / `import redis` / `import kafka` 都是越界。
           这不是洁癖：五个进程各写一份 wiring，就会有五种"连不上库时怎么办"，
           而每一种都只在它那一个进程里被测过。

--------------------------------------------------------------------------
客户端依赖必须惰性导入（PR-15）

`packages/` 保持零第三方依赖这条纪律，约束的是"import 时不许拖进外部库"。
如果在本文件顶层 `import psycopg`，那么：

    · 想读一下 `apps/worker` 怎么写的人，得先装一遍数据库客户端
    · 490 个单元测试会在 import 阶段就全红（测试机上没有 psycopg）

所以客户端一律**在函数内部** import，缺的时候给一条能照着做的提示，
而不是把一个 ImportError 原样丢出去让人去猜。

--------------------------------------------------------------------------
配置不许静默兜底（PR-16）

判据和 A-12 是同一条：**缺了以后是变慢还是变错。**

    AGENTOS_PG_DSN      缺了 → 变错（什么都不持久，却看起来在跑）
                        → 立刻失败
    AGENTOS_REDIS_URL   缺了 → 变慢（Lease 索引没了，退回 PG 全扫）
                        → 允许缺省，装配时跳过快路径

最危险的一种写法是"没配 DSN 就 fallback 到内存实现"：
那会造出一个**跑得很好但什么都不持久**的进程，
而且它对外报 healthy —— 这是"请求值 vs 实际值"那个坑的第四种变体，
前三种是记错值，这一种是**没配值却假装有值**。
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from packages.agent_runtime.executors import executor_coverage
from packages.execution_kernel.adapters.kafka import KafkaEventPublisher
from packages.execution_kernel.adapters.postgres import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresIdempotencyStore,
    PostgresOutboxDeliveryStore,
    PostgresOutboxStore,
    PostgresTaskRepository,
)
from packages.execution_kernel.adapters.redis import (
    RedisCancelSignalStore,
    RedisIdempotencyStore,
    RedisLeaseIndex,
)
from packages.execution_kernel.inmemory import SystemClock
from packages.execution_kernel.kernel import ExecutionKernel, KernelConfig
from packages.execution_kernel.ports import Clock
from packages.execution_kernel.scheduler import Scheduler, WorkerCapability
from packages.execution_kernel.worker import Worker, WorkerConfig

from ._runtime import SignalStop, StopSignal
from .cancellation_sweeper import CancellationSweeperApp, CancellationSweeperConfig
from .child_run_consumer import ChildRunConsumerApp, ChildRunConsumerConfig
from .outbox_publisher import OutboxPublisherApp, OutboxPublisherConfig
from .recovery_controller import RecoveryControllerApp, RecoveryControllerConfig
from .run_cancellation_sweeper import (
    RunCancellationSweeperApp,
    RunCancellationSweeperConfig,
)
from .wakeup_controller import WakeupControllerApp, WakeupControllerConfig

_UNSET = object()

#: 客户端 → (模块名, 安装提示)。PR-15：全部惰性导入。
_CLIENTS: dict[str, tuple[str, str]] = {
    "psycopg": ("psycopg", "pip install 'psycopg[binary]'"),
    "redis": ("redis", "pip install redis"),
    "kafka": ("kafka", "pip install kafka-python"),
}


class ConfigurationError(Exception):
    """配置缺失或客户端没装。一律**立刻**失败，不许降级成"看起来能用"。

    PR-16：这不是"友好的默认值"，是**拒绝启动**。
    一个连不上库的进程假装在运行，比一个起不来的进程危险得多 ——
    后者会被告警接住，前者会被当成"一切正常"。
    """


@dataclass(frozen=True)
class RuntimeConfig:
    """进程配置。只有一个来源：环境变量（PR-16）。

    刻意**不用** pydantic / dynaconf：`apps/` 可以有第三方依赖，
    但组合根是所有进程的共同前置，多一个配置框架就多一个"配置从哪来"的答案。
    """

    pg_dsn: str
    redis_url: str = ""
    kafka_brokers: str = ""
    executor_provider: str = ""
    """"`module:function` 形式的执行器提供方（PR-16：没有默认执行器）。

    执行器（ToolCallExecutor / LLMCallExecutor）属于 Runtime 与 Intelligence 层，
    不是基础设施 —— 组合根不替你猜一个。猜出来的那个会让每个 Task 都以
    `EXECUTOR_NOT_FOUND`（PERMANENT）失败，而报错看起来像配置坏了，
    排查方向从一开始就错了。
    """
    tool_provider: str = ""
    """"`module:function` → 返回 `ToolRuntime`（M23：工具表的来源）。"""
    stack_provider: str = ""
    """"`module:function` → 返回 `(agent_id, approvals) -> RuntimeStack` 的工厂（M24）。

    Interpreter / Planner / DecisionEngine 属于 Intelligence 层 —— 和 M23 的
    执行器同一个判据：组合根不替你猜一个智能体怎么想。
    """
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    model_provider: str = ""
    """"`module:function` → 返回 `ModelGateway`（M23：模型网关的来源）。"""
    identity_tokens: str = ""
    """"M105 / IAM：`{token: {subject, tenant_id, scopes}}` 的 JSON（M9）。

    空 = **不认证**（保持既有部署行为）。非空时 `build_api` 建一个静态身份表，
    写路由开始要求 `Authorization: Bearer`，且 actor 来自身份而非请求体（I-3）。
    """
    task_types: frozenset[str] = field(default_factory=frozenset)
    """"**期望**本进程能服务的 task_type（PR-21）。

    注意它是**期望**，不是声明：`WorkerCapability.task_types` 一律从真实的
    执行器表里**推导**出来，不从这里读。允许两者分开配，就等于允许它们漂移 ——
    那是"声明的能力 vs 实际的能力"又一个变体。这里只用来在配置期断言：
    你指望它干的事，表里真的有 handler 吗？
    """
    instance_id: str = ""
    lease_ttl: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=10)
    poll_limit: int = 1
    batch_size: int = 100
    executors: frozenset[str] = field(default_factory=frozenset)
    """"声明本进程接受哪些 `executor_type`。

    **空集 = "表里有什么我就接什么"**（默认）。刻意不给 `{"native"}` 这种默认值：
    默认只接 native 的话，`http:llm_call` 的 Task 永远不会被派发 ——
    worker 空转，LLM 任务在队列里堆着，而它对外报健康。
    那是"默认把能力砍掉"，和 PR-16 的"静默兜底"是同一种错。
    """
    labels: frozenset[str] = field(default_factory=frozenset)
    free_slots: int = 1
    #: M96：预算。**默认不限**（保持现状），配了才拦。
    #: `CostManager` 早就是 `before_action` 的一环，但组合根从没给过 Limit —— 于是
    #: `Budget()` 默认 `max_cost=inf`，"超预算 → DENY"这条线接了却没通电。
    max_cost: float | None = None
    max_tokens: int | None = None
    max_steps: int | None = None

    def budget(self) -> Any:
        """本次启动的 `Budget`。没配任何一项 = `Budget()`（不限）。"""
        from packages.agent_harness.cost import Budget

        if self.max_cost is None and self.max_tokens is None and self.max_steps is None:
            return Budget()
        return Budget(
            max_cost=self.max_cost if self.max_cost is not None else float("inf"),
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        instance_id: str | None = None,
    ) -> "RuntimeConfig":
        source = env if env is not None else os.environ

        # ------------------------------------------------------------ 清单模式
        #
        # M59：给了 `AGENTOS_MANIFEST` 就走清单，清单是**这一次启动的唯一声明源**。
        #
        # 刻意不是"清单 + 环境变量叠加"：那会让一次启动有两个来源，
        # 于是"清单里改了、实际读的是旧环境变量"这种只对了一半的改动成为可能（B-7）。
        # 两种模式二选一，一次启动只认一个。
        manifest_path = (source.get("AGENTOS_MANIFEST") or "").strip()
        if manifest_path:
            from packages.agent_manifest import ManifestError, load

            try:
                manifest = load(manifest_path)
            except ManifestError as e:
                # 配置错要在**启动前**死，不能带病启动后再退化
                raise ConfigurationError(
                    f"invalid manifest {manifest_path!r}: {e}"
                ) from None
            except OSError as e:
                # 读不到 / 权限 / 是目录 —— 一律归成"配置错了"。
                # 不能让一个裸的 FileNotFoundError 冒到进程外面：
                # 那是同一类问题（这份部署声明不可读），就该是同一个错。
                raise ConfigurationError(
                    f"cannot read manifest {manifest_path!r}: {e}"
                ) from None
            source = dict(manifest.to_env())

        pg_dsn = (source.get("AGENTOS_PG_DSN") or "").strip()
        if not pg_dsn:
            # PR-16：不许 fallback 到内存实现。缺 PG 的进程是"什么都不持久"的进程。
            raise ConfigurationError(
                "AGENTOS_PG_DSN is required; "
                "there is no honest in-memory fallback for the source of truth"
            )

        ttl = timedelta(seconds=_int(source, "AGENTOS_LEASE_TTL_SECONDS", 30))
        heartbeat = timedelta(seconds=_int(source, "AGENTOS_HEARTBEAT_SECONDS", 10))
        if heartbeat >= ttl:
            # 与 WorkerConfig.__post_init__ 同源的校验，但要在**配置期**就炸：
            # 装在进程里才知道的配置错误，要等第一次心跳才暴露。
            raise ConfigurationError(
                f"AGENTOS_HEARTBEAT_SECONDS({heartbeat.total_seconds()}) must be "
                f"shorter than AGENTOS_LEASE_TTL_SECONDS({ttl.total_seconds()})"
            )

        return cls(
            pg_dsn=pg_dsn,
            redis_url=(source.get("AGENTOS_REDIS_URL") or "").strip(),
            kafka_brokers=(source.get("AGENTOS_KAFKA_BROKERS") or "").strip(),
            executor_provider=(
                source.get("AGENTOS_EXECUTOR_PROVIDER") or ""
            ).strip(),
            tool_provider=(source.get("AGENTOS_TOOL_PROVIDER") or "").strip(),
            model_provider=(source.get("AGENTOS_MODEL_PROVIDER") or "").strip(),
            stack_provider=(source.get("AGENTOS_STACK_PROVIDER") or "").strip(),
            identity_tokens=(source.get("AGENTOS_IDENTITY_TOKENS") or "").strip(),
            api_host=(source.get("AGENTOS_API_HOST") or "127.0.0.1").strip(),
            api_port=_int(source, "AGENTOS_API_PORT", 8000),
            task_types=frozenset(_csv(source, "AGENTOS_TASK_TYPES", [])),
            instance_id=instance_id or (source.get("AGENTOS_INSTANCE_ID") or "").strip(),
            lease_ttl=ttl,
            heartbeat_interval=heartbeat,
            poll_limit=_int(source, "AGENTOS_POLL_LIMIT", 1),
            batch_size=_int(source, "AGENTOS_BATCH_SIZE", 100),
            executors=frozenset(_csv(source, "AGENTOS_EXECUTORS", [])),
            labels=frozenset(_csv(source, "AGENTOS_LABELS", [])),
            free_slots=_int(source, "AGENTOS_FREE_SLOTS", 1),
            max_cost=_opt_float(source, "AGENTOS_MAX_COST"),
            max_tokens=_opt_int(source, "AGENTOS_MAX_TOKENS"),
            max_steps=_opt_int(source, "AGENTOS_MAX_STEPS"),
        )


def _int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer, got {raw!r}") from exc


def _csv(source: Mapping[str, str], key: str, default: list[str]) -> list[str]:
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    return [part.strip() for part in raw.split(",") if part.strip()]


def _opt_int(source: Mapping[str, str], key: str) -> int | None:
    """可选整数：没配返回 `None`（= 不限），配了但非法**报错**。"""
    raw = (source.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer, got {raw!r}") from exc


def _opt_float(source: Mapping[str, str], key: str) -> float | None:
    raw = (source.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a number, got {raw!r}") from exc


def _client(name: str) -> Any:
    """PR-15：惰性导入真实客户端，缺的时候给一条能照着做的提示。"""
    module, hint = _CLIENTS[name]
    return _import_client(module, hint)


def _import_client(module: str, hint: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConfigurationError(
            f"client {module!r} is not installed; install it with: {hint}"
        ) from exc


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def pg_unit_of_work(conn: Any) -> Any:
    """PR-30 / PR-31：一个 PG 连接 = 一个事务边界（**X-3**）。

    单独一个函数而不是在每个 `build_*` 里 `PostgresUnitOfWork(conn)`，
    是为了让"这条进程有没有事务边界"只有**一个**答案。
    写五遍就会有五种写法，而漏掉的那一个不报错 ——
    它只是让这个进程的写在 DB 意义上不再是原子的。
    """
    from packages.execution_kernel.adapters.postgres import PostgresUnitOfWork

    return PostgresUnitOfWork(conn)


def pg_compensation_store(conn: Any) -> Any:
    """补偿账本，**连着它的事件落点一起**装配（M40 / 空洞 232）。

    为什么不就地 `PostgresCompensationStore(conn)`：

    账本写入与它的事件必须**同一个 conn**、同一个事务（X-3）。
    而"事件往哪儿写"是第二个构造参数，一不留神就会漏 ——
    漏了不报错，只是这一本账从此"改了没人知道"（正是本轮要闭合的空洞）。
    四个 `build_*` 里各写一遍，就会有四个漏的机会。

    所以它和 `pg_unit_of_work` 一样是**一个函数**：
    "一个连接上的账本长什么样"只许有一个答案（B-7）。
    """
    from packages.agent_runtime.adapters.postgres import PostgresCompensationStore

    return PostgresCompensationStore(conn, events=PostgresOutboxStore(conn))


def pg_cancellation_store(conn: Any) -> Any:
    """取消意图，**连着它的事件落点一起**装配（M47 / 空洞 225）。

    和 `pg_compensation_store` 完全同款（X-3）：
    取消意图写入与它的事件必须**同一个 conn**、同一个事务。
    漏了 `events=` 不报错，只是三条写路径从此"改了没人知道"。
    """
    from packages.agent_runtime.adapters.postgres import PostgresRunCancellationStore

    return PostgresRunCancellationStore(conn, events=PostgresOutboxStore(conn))


def pg_connection(config: RuntimeConfig) -> Any:
    """PG = Truth。缺了它变错不是变慢，所以这一步没有"可选"的形态。

    两个参数都**不能省**，每一个都对应一次真实的线上故障（M28 实测）：

    `row_factory=dict_row`
        三个 PG 适配器（`execution_kernel` / `agent_runtime` /
        `agent_harness`）全部按列名取字段（`row["approval_id"]`）。
        psycopg 默认返回**元组**，于是 `row["approval_id"]` 抛
        `TypeError: tuple indices must be integers` ——
        第一步还正常，走到"挂起等审批"那一步就 500。

    而单元测试**一直是绿的**：`tests/unit/sqlite_shim.py` 设了
    `row_factory = sqlite3.Row`（支持按名取），集成层的 `real_pg()`
    自己设了 `dict_row`。也就是说 —— **替身比生产更好用**，
    而生产那条路径从来没有被跑过一次（PR-28）。

    `autocommit=False`（M29 起）
        M28 时这里开的是 `autocommit=True`，因为当时 `packages/` 里
        **没有任何一处 `commit()`** —— 不开它就所有写都在进程退出时静默回滚，
        "PG = Truth" 是一句空话，而且不报错。

        M29 补上了真正的边界（`UnitOfWork`，PR-30 / PR-31），
        于是 X-3（status 写 + event 写同一事务）第一次成立：

            一个 HTTP 请求 = 一个事务（PR-31）
            一个 tick      = 一个事务（PR-30）

        现在开 autocommit 反而**取消**了这条保证 ——
        它会让"一起生效"退化成"各自生效"。

        ⚠️ 代价是：任何一个不经过这两条边界的写都会**静默丢失**。
        这正是它此前被设成 True 的原因。所以边界必须由组合根统一接
        （`pg_unit_of_work`），而不是让每个进程自己记得提交。
    """
    psycopg = _client("psycopg")
    # `dict_row` 同样必须**动态**取（PR-14）：写成 `from psycopg.rows import dict_row`
    # 会被 `test_pr14_no_module_statically_imports_a_client` 抓到 ——
    # 那条扫描正是为了保住"没装数据库客户端的机器上 685 个测试照样能跑"。
    rows = importlib.import_module("psycopg.rows")
    return psycopg.connect(
        config.pg_dsn, row_factory=rows.dict_row, autocommit=False
    )


def redis_client(config: RuntimeConfig) -> Any | None:
    """Redis 是快路径，不是事实来源 —— 没配就返回 None，装配时跳过。

    PR-16：这是**允许**的缺省，因为缺 Redis 只让系统变慢
    （Lease 索引没了 → 退回 PG 全扫，见 `RecoveryController.sweep_every`），
    不会变错。
    """
    if not config.redis_url:
        return None
    redis = _client("redis")
    return redis.Redis.from_url(config.redis_url)


def kafka_producer(config: RuntimeConfig) -> Any:
    """Outbox publisher 的投递端。没配 broker 就不是"降级"，是**没法干活**。"""
    if not config.kafka_brokers:
        raise ConfigurationError(
            "AGENTOS_KAFKA_BROKERS is required for the outbox publisher; "
            "without a broker the outbox only grows"
        )
    kafka = _client("kafka")
    return kafka.KafkaProducer(
        bootstrap_servers=[b for b in config.kafka_brokers.split(",") if b]
    )


def kafka_consumer(
    config: RuntimeConfig, *, group_id: str | None = None, topics: Any = None
) -> Any:
    """子 Run 完成事件的消费端（M30 / 空洞 209）。

    `enable_auto_commit=False` 不是调优，是**正确性**：
    自动提交会在消息被**取出**时就推进 offset，而不是在处理完、更不是在
    落库之后。于是"进程在 handler 中途死了"会变成"这条消息消费过了，
    但它引起的写从未生效" —— 而顺序本来该由 `ProcessRuntime.on_commit`
    在事务提交之后来定（见 `apps/child_run_consumer/app.py` 文件头第 2 条）。
    """
    if not config.kafka_brokers:
        raise ConfigurationError(
            "AGENTOS_KAFKA_BROKERS is required for the child-run consumer; "
            "without a broker no parent run is ever woken"
        )
    kafka = _client("kafka")
    return kafka.KafkaConsumer(
        bootstrap_servers=[b for b in config.kafka_brokers.split(",") if b],
        group_id=group_id or config.instance_id or "agentos-child-run-consumer",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def build_kernel(
    config: RuntimeConfig,
    *,
    conn: Any = None,
    redis: Any = _UNSET,
    clock: Clock | None = None,
) -> ExecutionKernel:
    """Kernel = PG（Truth）+ 可选的 Redis（快路径）。

    连接可以由调用方注入（测试 / 复用连接池）；不注入就自己建。
    """
    conn = conn if conn is not None else pg_connection(config)
    if redis is _UNSET:
        redis = redis_client(config)
    return ExecutionKernel(
        repository=PostgresExecutionRepository(conn),
        outbox=PostgresOutboxStore(conn),
        clock=clock or SystemClock(),
        attempts=PostgresAttemptRepository(conn),
        # E-26：没有它，重启后的 Scheduler / Worker 拿不到
        # priority / tenant / resource / payload —— 那四样是正确性输入。
        tasks=PostgresTaskRepository(conn),
        lease_index=RedisLeaseIndex(redis) if redis is not None else None,
        cancel_signals=RedisCancelSignalStore(redis) if redis is not None else None,
        idempotency=RedisIdempotencyStore(redis) if redis is not None else None,
        config=KernelConfig(default_lease_ttl=config.lease_ttl),
    )


def build_executors(config: RuntimeConfig) -> Mapping[str, Any]:
    """按 `AGENTOS_EXECUTOR_PROVIDER`（`module:function`）装载执行器表。

    返回一个 `executor_type -> Executor` 的映射，交给 `build_worker`。
    没有配置就**拒绝启动**，而不是返回一个空表 ——
    空表会让每个 Task 以 PERMANENT 失败，看起来像业务坏了。

    PR-21：装完还要**校验覆盖**。你指望这个进程服务的 task_type，
    表里必须真有 handler —— 否则在这里就炸，而不是等第一个 Task 来了
    以 `EXECUTOR_NOT_FOUND`（PERMANENT）炸。判据同 PR-16：
    "起得来但干不了活"比"起不来"危险。
    """
    table = _load_executor_table(config)
    coverage = executor_coverage(table)
    missing = coverage.missing(config.task_types)
    if missing:
        raise ConfigurationError(
            f"AGENTOS_TASK_TYPES expects {sorted(missing)} but the executor table "
            f"has no handler for them; covered (executor_type, task_type) pairs are "
            f"{sorted(coverage.covered)}. A worker that starts but cannot do what "
            f"it advertised is worse than one that refuses to start"
        )
    return table


def describe_coverage(executors: Mapping[str, Any]) -> str:
    """把覆盖度的**三个缺口**说成一句话（启动时打到 stderr）。

    为什么不静默：PR-20 让 unrouted 的那些 Task **不会被派发**，
    于是它们不会报错，只会安静地在 PENDING 里堆着。
    一个不报错的洞比一个报错的洞更难发现 —— 所以要么没有洞，要么说出来。

    ------------------------------------------------------------------
    为什么 deferred 也要报（M25）

    技能与委派的执行器现在存在了，`unrouted` 随之清零。
    但它们的全部内容就是拒绝 —— **系统仍然跑不了一件技能、一次委派**。
    如果只报 unrouted，启动日志从此一片安静，
    等于把"做不到"报成了"做到了"。

    这是 PR-24（版本号漂移）的同一类错：对外报的值与实际能做的事不是同一个。
    所以三档都要出口，且 deferred 必须带上"主人是谁"。
    """
    coverage = executor_coverage(executors)
    lines: list[str] = []

    if coverage.unrouted:
        listed = ", ".join(f"{et}:{tt}" for et, tt in sorted(coverage.unrouted))
        lines.append(
            f"no executor for {listed}; those tasks will stay PENDING forever "
            f"(PR-20 keeps them from being dispatched, so they never even fail loudly)"
        )

    if coverage.deferred:
        for et, tt in sorted(coverage.deferred):
            owner = coverage.owners.get((et, tt), "")
            lines.append(
                f"{et}:{tt} is routed but NOT worker-executable — it is owned by "
                f"{owner or '(owner not declared)'}; a Worker receiving it means the "
                f"owning run is gone"
            )

    if not lines:
        return ""
    if coverage.deferred and not coverage.unrouted:
        lines.append(
            "every produced combination is routed, but the deferred ones above are "
            "refusals, not implementations — do not read an empty unrouted list as "
            "'the system can do all of it'"
        )
    return "\n".join(lines)




def _load_executor_table(config: RuntimeConfig) -> dict[str, Any]:
    """只负责装载，不做校验 —— 校验在 `build_executors` 里（PR-21）。"""
    if not config.executor_provider:
        raise ConfigurationError(
            "AGENTOS_EXECUTOR_PROVIDER is required for apps.worker "
            "(format: 'package.module:build_executors'); "
            "there is no default executor because executors belong to "
            "Runtime and Intelligence, not to infrastructure"
        )
    module_name, _, attr = config.executor_provider.partition(":")
    if not module_name:
        raise ConfigurationError(
            f"AGENTOS_EXECUTOR_PROVIDER must look like 'module:function', "
            f"got {config.executor_provider!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"cannot import executor provider module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, attr or "build_executors", None)
    if factory is None:
        raise ConfigurationError(
            f"{module_name} has no attribute {attr or 'build_executors'!r}"
        )
    table = dict(factory(config))
    if not table:
        # 空表不是"暂时没活干"，是"这个 worker 什么都不会" ——
        # 它会把每个 Task 都以 EXECUTOR_NOT_FOUND 拒掉，还对外报健康。
        raise ConfigurationError(
            f"{config.executor_provider} returned an empty executor table; "
            f"a worker with no executors fails every task permanently"
        )
    return table


def build_worker(
    config: RuntimeConfig,
    *,
    executors: Mapping[str, Any] | None = None,
    kernel: ExecutionKernel | None = None,
    capability: WorkerCapability | None = None,
    conn: Any = None,
) -> Worker:
    """Worker = Scheduler（Kernel 能力）+ Executor 表。

    `executors` 不传就走 `AGENTOS_EXECUTOR_PROVIDER`（见 `build_executors`）。
    执行器属于 Runtime / Intelligence 层（ToolCallExecutor / LLMCallExecutor），
    不是基础设施 —— 组合根不替你猜一个，猜出来的那个会让每个 Task 都以
    `EXECUTOR_NOT_FOUND`（PERMANENT）失败，而失败原因看起来像配置问题。

    PR-18：这里**没有**独立的 `apps/scheduler` 进程。派活 = 本进程 tick 内
    的一次 Atomic Claim。做成独立进程等于引入一个中心：
    中心挂了全部停摆，而且它要"通知 worker"就必须走消息，
    那正是 §36 明令禁止的"把 Kafka 当任务队列"。
    """
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    table = dict(executors if executors is not None else build_executors(config))
    if capability is None:
        # PR-21：能力一律**推导**自真实装配出来的执行器表。
        # 另行声明一份就等于允许"声明的能力"和"实际装配的能力"漂移 ——
        # 那是 A-10 / S-9 那条"记错值"的坑，换成了"声明错值"。
        # 裸执行器（测试里常见）覆盖不到任何 (executor_type, task_type)，
        # 于是 task_types 为空 = 不限 —— 向后兼容，且语义正确：
        # 它本来就没声明自己能干什么。
        covered = executor_coverage(table).covered
        # 没显式声明就接下**整张表**。默认砍掉一半能力会让 worker 空转：
        # 它明明装了 http:llm_call，却因为默认值里没有 http 而永远不派发。
        executor_types = config.executors or frozenset(table)
        capability = WorkerCapability(
            executors=executor_types,
            labels=config.labels,
            free_slots=config.free_slots,
            task_types=frozenset(
                task_type
                for executor_type, task_type in covered
                if executor_type in executor_types
            ),
        )
    return Worker(
        kernel=kernel,
        scheduler=Scheduler(kernel),
        executors=table,
        config=WorkerConfig(
            worker_id=config.instance_id or "worker",
            lease_ttl=config.lease_ttl,
            heartbeat_interval=config.heartbeat_interval,
            poll_limit=config.poll_limit,
        ),
        capability=capability,
    )


def build_outbox_publisher(
    config: RuntimeConfig,
    *,
    conn: Any = None,
    producer: Any = None,
    signal: StopSignal | None = None,
    sleep: Callable[[float], None] | None = None,
    uow: Any = None,
) -> OutboxPublisherApp:
    conn = conn if conn is not None else pg_connection(config)
    producer = producer if producer is not None else kafka_producer(config)
    return OutboxPublisherApp(
        outbox=PostgresOutboxStore(conn),
        publisher=KafkaEventPublisher(producer),
        delivery=PostgresOutboxDeliveryStore(conn),
        config=OutboxPublisherConfig(
            instance_id=config.instance_id or "outbox-publisher",
            batch_size=config.batch_size,
        ),
        signal=signal,          # 默认 ManualStop：组合根不偷偷注册信号处理器
        sleep=sleep,
        uow=uow if uow is not None else pg_unit_of_work(conn),
    )


def build_worker_app(
    config: RuntimeConfig,
    *,
    executors: Mapping[str, Any] | None = None,
    conn: Any = None,
    signal: "StopSignal | None" = None,
) -> Any:
    """PR-30：Worker 进程 = Worker + 事务边界。

    为什么它必须在这里而不是在 `__main__.py`：
    事务边界是**基础设施**，组合根是所有进程的共同前置（PR-14）。
    让入口文件自己去建连接，等于给"这个进程连的是哪个库"开第二个答案。
    """
    from apps.worker import WorkerApp, WorkerProcessConfig

    conn = conn if conn is not None else pg_connection(config)
    return WorkerApp(
        worker=build_worker(
            config,
            executors=executors,
            conn=conn,
        ),
        config=WorkerProcessConfig(
            instance_id=config.instance_id or "worker",
            poll_limit=config.poll_limit,
        ),
        signal=signal,                # 组合根不偷偷注册信号处理器
        uow=pg_unit_of_work(conn),
    )


def build_recovery_controller(
    config: RuntimeConfig,
    *,
    kernel: ExecutionKernel | None = None,
    conn: Any = None,
    signal: StopSignal | None = None,
    sleep: Callable[[float], None] | None = None,
    uow: Any = None,
) -> RecoveryControllerApp:
    conn = conn if conn is not None else pg_connection(config)
    uow = uow if uow is not None else pg_unit_of_work(conn)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    return RecoveryControllerApp(
        kernel=kernel,
        config=RecoveryControllerConfig(
            worker_id=config.instance_id or "recovery-controller",
        ),
        signal=signal,
        sleep=sleep,
        uow=uow,
    )


def build_wakeup_controller(
    config: RuntimeConfig,
    *,
    kernel: ExecutionKernel | None = None,
    approvals: Callable[[], Mapping[str, str]] | None = None,
    conn: Any = None,
    signal: StopSignal | None = None,
    sleep: Callable[[float], None] | None = None,
    uow: Any = None,
) -> WakeupControllerApp:
    conn = conn if conn is not None else pg_connection(config)
    uow = uow if uow is not None else pg_unit_of_work(conn)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    return WakeupControllerApp(
        kernel=kernel,
        approvals=approvals,
        config=WakeupControllerConfig(),
        signal=signal,
        sleep=sleep,
        uow=uow,
    )


def build_cancellation_sweeper(
    config: RuntimeConfig,
    *,
    kernel: ExecutionKernel | None = None,
    conn: Any = None,
    signal: StopSignal | None = None,
    sleep: Callable[[float], None] | None = None,
    uow: Any = None,
) -> CancellationSweeperApp:
    conn = conn if conn is not None else pg_connection(config)
    uow = uow if uow is not None else pg_unit_of_work(conn)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    return CancellationSweeperApp(
        kernel=kernel,
        config=CancellationSweeperConfig(),
        signal=signal,
        sleep=sleep,
        uow=uow,
    )


def build_run_cancellation_sweeper(
    config: RuntimeConfig,
    *,
    service: Any = None,
    cancellations: Any = None,
    snapshots: Any = None,
    conn: Any = None,
    factory: Any = None,
    approvals: Any = None,
    child_registry: Any = None,
    kernel: Any = None,
    compensations: Any = None,
    signal: StopSignal | None = None,
    sleep: Callable[[float], None] | None = None,
    uow: Any = None,
) -> RunCancellationSweeperApp:
    """M34 / 空洞 222：**Run 级**取消意图 → 终态。

    与 Execution 级的 `build_cancellation_sweeper` 是相邻的两层，不是替代：
    一条子 Run 在等孙 Run 时手上没有活的 Execution，
    Execution 级的信号没有地方挂（见 `run_cancellations.sql` 的表头）。

    `cancellations` 与 `snapshots` 必须是**同一个连接**上的 PG 实现（A-12）：

        cancellations  意图住在这里；丢了 → 取消请求从来没被写下
        snapshots      `RunRecovery` 靠它重建；丢了 → sweep 一条都认领不了

    而 `factory` 重建出来的那条栈，必须也拿到**同一份** `cancellations` ——
    否则它 `cancel()` 时写的是另一张表（或内存），
    于是 sweep 结掉的是自己写下的那一条，而真正被请求的那一道永远 pending。

    ---------------------------------------------------------------------------
    M37 / 空洞 226：它还必须拿到 `child_registry` 与 `saga`

    Sweeper 现在会**放弃**到点仍无回音的意图（R-11）。
    放弃一条子 Run 的等待时，它的副作用从此再也没有别的入口会记 ——
    所以必须当场补一笔 D-13 孤儿（R-12）。

    `compensations` 因此和 `snapshots` / `cancellations` 一样是**同一个连接**上的
    PG 实现：孤儿账本只有一本，sweeper 记的那一笔
    必须能被 `apps/api` 的补偿看板读出来（A-12：记在两处 = 变错）。
    少了这两个参数，`sweep()` 会**抛**而不是静默跳过 ——
    一个把队列腾干净却把账本留空的进程，比一个堵住的进程更难发现。
    """
    from packages.agent_harness.adapters.postgres import PostgresApprovalStore
    from packages.agent_runtime.adapters.postgres import (
        PostgresChildRunRegistry,
        PostgresCompensationStore,
        PostgresRunCancellationStore,
        PostgresRunSnapshotStore,
    )
    from packages.agent_runtime.cancellation import RunCancellationService
    from packages.agent_runtime.recovery import RunRecovery
    from packages.agent_runtime.saga import SagaCoordinator

    conn = conn if conn is not None else pg_connection(config)
    uow = uow if uow is not None else pg_unit_of_work(conn)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    snapshots = (
        snapshots if snapshots is not None else PostgresRunSnapshotStore(conn)
    )
    cancellations = (
        cancellations
        if cancellations is not None
        else pg_cancellation_store(conn)
    )
    registry = (
        child_registry
        if child_registry is not None
        else PostgresChildRunRegistry(conn)
    )
    compensations = (
        compensations if compensations is not None else pg_compensation_store(conn)
    )

    if service is None:
        service = RunCancellationService(
            store=cancellations,
            child_registry=registry,
            saga=SagaCoordinator(store=compensations),
            recovery=RunRecovery(
                snapshots=snapshots,
                factory=(
                    factory
                    if factory is not None
                    else load_stack_factory(
                        config,
                        child_registry=child_registry
                        if child_registry is not None
                        else PostgresChildRunRegistry(conn),
                        kernel=kernel,
                        snapshots=snapshots,
                        compensations=(
                            compensations
                            if compensations is not None
                            else pg_compensation_store(conn)
                        ),
                        cancellations=cancellations,
                    )
                ),
                approvals=(
                    approvals
                    if approvals is not None
                    else PostgresApprovalStore(conn)
                ),
            ),
        )
    return RunCancellationSweeperApp(
        service=service,
        config=RunCancellationSweeperConfig(),
        signal=signal,
        sleep=sleep,
        uow=uow,
    )


# ---------------------------------------------------------------------------
# M24：Control Plane（`apps/api` 背后那套东西）
# ---------------------------------------------------------------------------


#: "必须收下，否则装载失败"的关键字参数 → 收不下时的后果。
#:
#: 这四条的**形状完全一样**：`assemble_runtime_stack` 给每一个都留了一个
#: 内存兜底（`or InMemory...()`），于是漏掉任何一个都不会报错 ——
#: 系统照跑，只是那样东西从不落库。四条的判据都是 A-12：丢了变错，不是变慢。
#:
#: 把它们列成一张表而不是写四段 if，是因为第五个这样的参数迟早会出现；
#: 那时"忘了加校验"就是第五个洞。写成一个数据结构，漏的是一条数据，不是一段逻辑。
_REQUIRED_EXTRAS: dict[str, str] = {
    "child_registry": (
        "without it the child-run registry falls back to process memory and D-1 "
        "(a retry must never spawn a second child run) stops holding after a "
        "restart — add the keyword parameter and pass it into the spawner"
    ),
    "kernel": (
        "without it the stack assembles an in-memory Kernel and every Execution "
        "of every Run driven by this process is lost on exit — `executions` / "
        "`attempts` / `outbox_events` stay empty and X-3 has nothing to be atomic "
        "over — add the keyword parameter and pass it into "
        "`assemble_runtime_stack`"
    ),
    "snapshots": (
        "without it Run snapshots go to process memory and R-5 stops holding: "
        "after a restart `run_snapshots` is empty, `RunRecovery.rebuild()` finds "
        "nothing and every Run 404s even though it is still suspended — add the "
        "keyword parameter and pass it into `assemble_runtime_stack`"
    ),
    "compensations": (
        "without it every CompensationSpec declared by a Run goes to process "
        "memory and S-9 stops holding after a restart: the Saga ledger is empty, "
        "so an undo that was declared can no longer be executed — add the keyword "
        "parameter and pass it into `assemble_runtime_stack`"
    ),
    "cancellations": (
        "without it a Run-level cancellation has no durable channel and 空洞 222 "
        "reopens: the parent marks the child `cancelled` in the registry while the "
        "child keeps running in another process — nobody tells it, and the intent "
        "table stays empty so no sweeper can adopt it either — add the keyword "
        "parameter and pass it into `assemble_runtime_stack`"
    ),
    "context_snapshots": (
        "without it Context snapshots go to process memory and C-5 stops holding "
        "after a restart: `context_snapshots` stays empty, so 'what did the model "
        "actually see on that call' becomes unanswerable exactly when it is asked "
        "most (after a restart) — add the keyword parameter and pass it into the "
        "ContextAssembler"
    ),
    "budget": (
        "without it the Run's Budget stays the default (max_cost=inf) and "
        "AGENTOS_MAX_COST / MAX_TOKENS / MAX_STEPS are silently ignored: the Run "
        "will spend without limit while the config looks like it set a cap — "
        "accept the keyword parameter and pass it into Harness.default()"
    ),
    "memory": (
        "without it agent memory falls back to process memory: nothing a Run "
        "learned survives a restart — add the keyword parameter and pass it into "
        "assemble_runtime_stack()"
    ),
    "budget": (
        "without it the Run's Budget stays the default (max_cost=inf) and "
        "AGENTOS_MAX_COST / MAX_TOKENS / MAX_STEPS are silently ignored: the Run "
        "will spend without limit while the config looks like it set a cap — "
        "accept the keyword parameter and pass it into Harness.default()"
    ),
}

#: 一条链上的 MemoryManager：写（Loop._finish）与读（ContextAssembler 的
#: memory_provider）必须是**同一份** —— 写了没人读得到 = 等于没写。
_MEMORY: dict[int, Any] = {}


def pg_memory_manager(conn: Any) -> Any:
    """`PostgresMemoryStore` + `MemoryManager`，整个进程**共用一份**。

    按 `conn` 缓存：同一个连接上只应有一个 Manager，否则"写在一份、
    读另一份"会表现为"记不住"，而两条路径都不会报错。
    """
    from packages.agent_context.adapters.memory_postgres import PostgresMemoryStore
    from packages.agent_context.memory import MemoryManager

    key = id(conn)
    if key not in _MEMORY:
        _MEMORY[key] = MemoryManager(PostgresMemoryStore(conn))
    return _MEMORY[key]


def load_stack_factory(
    config: RuntimeConfig,
    *,
    child_registry: Any = None,
    kernel: Any = None,
    snapshots: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    context_snapshots: Any = None,
    memory: Any = None,
    budget: Any = None,
) -> Callable[[str, Any], Any]:
    """按 `AGENTOS_STACK_PROVIDER` 装载 `(agent_id, approvals) -> RuntimeStack`。

    Interpreter / Planner / DecisionEngine 属于 Intelligence，和执行器同一个判据：
    组合根不替你猜一个智能体怎么想。猜出来的那个会"跑通"每一个 Run，
    但每一步的决策都和你期望的无关 —— 那比报错更难发现。

    ------------------------------------------------------------------
    四个"必须收下"的关键字参数（空洞 212 / 213 / 214）

    `assemble_runtime_stack` 给 Kernel / snapshots / compensations 都写了
    一句 `or InMemory...()`，而 `child_registry` 在 provider 里也是同一形状。
    于是这四样东西**漏掉任何一样都不报错** —— 系统照跑，只是那样东西
    从不落库。M29 真起一次服务量到了其中三样：

        `executions`      = 0   （空洞 212：Kernel 是内存的）
        `run_snapshots`   = 0   （空洞 213：挂起时拍的快照存进了内存）
        `compensations`   = 0   （空洞 214：声明过的补偿账本在内存里）

    前两条尤其讽刺：Control Plane **已经**把 `PostgresRunSnapshotStore`
    装配好了（`build_control_plane`），只是从没递到栈里去 ——
    于是 R-5 在"看起来已经做完"的状态下不成立。

    判据同 A-3 / D-1：丢了是变错，不是变慢。所以**收不下就装载失败**，
    绝不静默退化。
    """
    if not config.stack_provider:
        raise ConfigurationError(
            "AGENTOS_STACK_PROVIDER is required for apps.api "
            "(format: 'package.module:build_stack_factory'); "
            "there is no default agent stack because how an agent thinks "
            "belongs to Intelligence, not to infrastructure"
        )
    module_name, _, attr = config.stack_provider.partition(":")
    if not module_name or not attr:
        raise ConfigurationError(
            f"AGENTOS_STACK_PROVIDER must look like 'module:function', "
            f"got {config.stack_provider!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"cannot import stack provider {module_name!r}: {exc}"
        ) from exc
    provider = getattr(module, attr, None)
    if provider is None:
        raise ConfigurationError(f"{module_name} has no attribute {attr!r}")

    import inspect

    try:
        params = inspect.signature(provider).parameters
    except (TypeError, ValueError):  # pragma: no cover - C 扩展等取不到签名
        params = None

    def _accepts(key: str) -> bool:
        # 取不到签名（C 扩展等）时放行：宁可放过也不误伤一个其实收得下的 provider。
        if params is None:
            return True
        if key in params:
            return True
        return any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    # 只校验**真的要递进去**的那些。不传就不校验 ——
    # 否则"这条进程不派生子 Run / 不接 PG 存储"的 provider 会被误伤
    # （那正是测试里那些形状合法的替身）。
    given = {
        "child_registry": child_registry,
        "kernel": kernel,
        "snapshots": snapshots,
        "compensations": compensations,
        "cancellations": cancellations,
        "context_snapshots": context_snapshots,
        "memory": memory,
        "budget": budget,
    }
    extras: dict[str, Any] = {}
    for key, value in given.items():
        if value is None:
            continue
        if not _accepts(key):
            raise ConfigurationError(
                f"stack provider {config.stack_provider!r} does not accept "
                f"`{key}`; {_REQUIRED_EXTRAS[key]}"
            )
        extras[key] = value
    return provider(config, **extras)


def build_control_plane(
    config: RuntimeConfig,
    *,
    conn: Any = None,
    factory: Callable[[str, Any], Any] | None = None,
    approvals: Any = None,
    snapshots: Any = None,
    idempotency: Any = None,
    child_registry: Any = None,
    kernel: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    context_snapshots: Any = None,
    memory: Any = None,
    #: M96：本进程的预算（来自环境）。`None` = 交给 provider 的默认（不限）。
    budget: Any = None,
    #: M61：agent 注册表。显式传 `None` 之外的值可绕过环境变量（测试用）。
    registry: Any = None,
) -> Any:
    """`apps/api` 背后的 Control Plane，全部持久化。

    每样东西的存储选择用的都是 A-12 那条判据（丢了是变慢还是变错）：

        approvals       审批事实 → 丢了变错（界面空白而系统全在等）→ PG（A-10/A-12）
        snapshots       Run 快照 → 丢了变错（重启后 Run 点不动）  → PG（R-5）
        idempotency     Run 幂等键 → 丢了变错（开出第二个 Run）   → PG（**A-3**）
        child_runs      派生登记 → 丢了变错（开出第二个子 Run）   → PG（**D-1**）
        kernel          Execution → 丢了变错（DB 里查不到这次执行）→ PG（**X-3**）
        compensations   补偿账本 → 丢了变错（撤销执行不了）      → PG（**S-9**）

    前三条里最该记住的是幂等键：M24 之前组合根把它接到了 Redis 上，
    而 A-3 明写着"幂等键不能放 Redis"。它在内存实现下测试全绿，所以没人发现。

    第四条是 M26 才补上的：D-1 从 M25 冻结起就落在**进程内存**的一个 dict 里，
    于是它只在"进程没重启"时成立。

    第五、六条是 M29 才补上的（空洞 212 / 213 / 214），也是最难发现的一类：
    **组合根已经把它们装配好了，只是从没递到栈里去。**

        kernel          → 由 `assemble_runtime_stack` 顺手 new 一个内存版
        snapshots       → 同上；挂起时拍的快照存进内存，`run_snapshots` 恒为 0
        compensations   → 同上；声明过的补偿账本在内存里

    M29 真起一次服务量到了这个结果：

        executions=0  run_snapshots=0  compensations=0
        而 `GET /runs/{id}/trace` 依然说得清 Run 走到第几步。

    "看起来已经做完，实际没接上" —— 这三样都是，且都不报错。
    所以判据不是"装配了没有"，而是**"栈里的那一份是不是 PG 版"**，
    断言落在 `stack.kernel.repository` / `stack.loop.snapshots` 上
    （见 `tests/unit/test_http_api.py:KernelWiringTest`）。

    所有存储必须与 `conn` 是同一个连接：否则 Execution 的写
    和审批/快照的写不在同一个事务里，PR-31 的"一个请求 = 一个事务"
    就又变成了两个答案。
    """
    from packages.agent_api.service import InProcessControlPlane
    from packages.agent_context.adapters.postgres import PostgresContextSnapshotStore
    from packages.agent_harness.adapters.postgres import PostgresApprovalStore
    from packages.agent_runtime.adapters.postgres import (
        PostgresChildRunRegistry,
        PostgresCompensationStore,
        PostgresRunCancellationStore,
        PostgresRunSnapshotStore,
    )

    conn = conn if conn is not None else pg_connection(config)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    snapshots = (
        snapshots if snapshots is not None else PostgresRunSnapshotStore(conn)
    )
    # M34 / 空洞 222：Control Plane 自己也要有那条通道。
    #
    # 一条跑在**别的进程**里的 Run，这一进程既没有它的 stack、也可能没有它的
    # 快照（R-1：快照只在挂起时拍）。那时 `POST /runs/{id}/cancel` 只能
    # 落一条意图 —— 没有这个 store，它就只能报 404 RUN_NOT_FOUND，
    # 而那条 Run 明明活着（控制台看得到它，却叫不停它）。
    cancellations = (
        cancellations
        if cancellations is not None
        else pg_cancellation_store(conn)
    )
    # M61：agent 注册表（None = 不校验，维持现状）
    _reg = registry if registry is not None else _load_agent_registry()

    #: 装载栈所需的共享参数 —— 按 agent 分派时要复用同一份（否则每个 agent
    #: 各自 new 一个 Kernel / 登记处，"跨 Run 共享"就散了）。
    _loader_kwargs: dict[str, Any] = {
        "child_registry": (
            child_registry if child_registry is not None
            else PostgresChildRunRegistry(conn)
        ),
        "kernel": kernel,
        "snapshots": snapshots,
        "compensations": (
            compensations if compensations is not None
            else pg_compensation_store(conn)
        ),
        "cancellations": cancellations,
        # M95：Context 快照 → 丢了变错（重启后"模型当时看到了什么"查不到）→ PG。
        "context_snapshots": (
            context_snapshots if context_snapshots is not None
            else PostgresContextSnapshotStore(conn)
        ),
        # M96：Memory 的**事实源** → 丢了变错（重启后记不住）→ PG。
        # 读写是**同一份 Manager**（写了没人读得到 = 等于没写）。
        "memory": memory if memory is not None else pg_memory_manager(conn),
        # M96：预算。配了就把这一次启动的限额一路递到 Harness.default()。
        "budget": budget if budget is not None else config.budget(),
    }

    if factory is not None:
        _factory = factory
    elif _reg is not None:
        # M62：注册表里声明了每 agent 的 stack —— 按它分派，别让它成为死数据
        _factory = make_dispatching_stack_factory(config, _reg, **_loader_kwargs)
    else:
        _factory = load_stack_factory(config, **_loader_kwargs)

    return InProcessControlPlane(
        factory=_factory,
        approvals=approvals if approvals is not None else PostgresApprovalStore(conn),
        snapshots=snapshots,
        idempotency=(
            idempotency
            if idempotency is not None
            else PostgresIdempotencyStore(conn, durable=True)
        ),
        cancellations=cancellations,
        # M61 / 空洞 231：给了注册表才校验 agent_id；不给就维持现状（透传）。
        #
        # 与 manifest 同一条规矩：配置错要在**启动前**死，不能带病启动后再退化。
        registry=_reg,
        # M65 / 空洞 234：Execution 取消归因的出口
        executions=_execution_lookup(conn),
    )


def _execution_lookup(conn: Any) -> Any:
    """`(run_id) -> list[dict]`：这条 Run 手上各 Execution 的取消意图与归因。

    `executions` 表没有 `run_id` 列 —— 通过 `tasks` 中转：

        executions.task_id → tasks.task_id → tasks.run_id

    这条 JOIN 线已经验证过能查出归因行（M48 时验过 5 行）。刻意**不**给
    `executions` 加一列 `run_id`：那会在两处存同一个事实（B-7），
    而这条 JOIN 已经能回答这个问题。
    """
    def lookup(run_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT e.execution_id, e.status, e.cancellation_requested,"
            "       e.cancellation_reason, e.cancellation_by"
            "  FROM executions e JOIN tasks t ON e.task_id = t.task_id"
            " WHERE t.run_id = %s"
            " ORDER BY e.created_at, e.execution_id",
            (run_id,),
        ).fetchall()
        return [
            {
                "execution_id": str(r["execution_id"]),
                "status": str(r["status"]),
                "cancellation_requested": bool(r["cancellation_requested"]),
                "cancellation_reason": str(r["cancellation_reason"] or ""),
                "cancellation_by": str(r["cancellation_by"] or ""),
            }
            for r in rows
        ]

    return lookup


# ---------------------------------------------------------------- 栈分派（M62）


# ---------------------------------------------------------------- 栈分派（M62）
#
# M61 交付的注册表里，每个 agent 可以声明自己的 `stack`。
# 但**当时没有任何代码读它** —— 那是死数据：
# 写了 `stack = "..."` 的人会以为它生效了，而实际用的还是全局那一个。
#
# 这与 M59→M60 是同一条教训：一份声明如果启动不了任何东西，
# 它就只是一份文档。所以这里把它接上。
#
# 分派规则：
#     agent 声明了自己的 stack → 用它
#     否则                     → 用全局 AGENTOS_STACK_PROVIDER
#
# 刻意**不是**"每个 agent 都必须声明"：大多数部署只有一个栈，
# 让每个 agent 各抄一遍同一个字符串，是制造不一致的最好方式（B-7）。


def make_dispatching_stack_factory(
    config: "RuntimeConfig",
    registry: Any,
    **loader_kwargs: Any,
) -> Callable[[str, Any], Any]:
    """返回一个 `(agent_id, approvals) -> RuntimeStack`，按 agent 声明分派栈。"""
    from dataclasses import replace

    cache: dict[str, Any] = {}

    def factory_for(provider: str) -> Any:
        if provider not in cache:
            cache[provider] = load_stack_factory(
                replace(config, stack_provider=provider), **loader_kwargs
            )
        return cache[provider]

    def dispatching(agent_id: str, approvals: Any) -> Any:
        agent = registry.get(agent_id) if registry is not None else None
        provider = (
            agent.stack
            if agent is not None and getattr(agent, "stack", "")
            else config.stack_provider
        )
        return factory_for(provider)(agent_id, approvals)

    return dispatching


def _load_agent_registry(env: Mapping[str, str] | None = None) -> Any:
    """`AGENTOS_AGENT_REGISTRY` 指向的注册表；没设就返回 `None`（= 不校验）。

    `None` 不是"漏配" —— 它是"这个部署选择不强校验"，见 `packages.agent_registry`
    文档里那段"为什么没配注册表就维持现状是必须的"。
    """
    source = env if env is not None else os.environ
    path = (source.get("AGENTOS_AGENT_REGISTRY") or "").strip()
    if not path:
        return None
    from packages.agent_registry import RegistryError, load

    try:
        return load(path)
    except RegistryError as e:
        raise ConfigurationError(f"invalid agent registry {path!r}: {e}") from None
    except OSError as e:
        # 与 manifest 同：读不到也是"这份声明不可读"，同一个错
        raise ConfigurationError(
            f"cannot read agent registry {path!r}: {e}"
        ) from None


def build_child_run_consumer(
    config: RuntimeConfig,
    *,
    consumer: Any = None,
    waker: Any = None,
    processed: Any = None,
    #: 空洞 229：`ChildRunWaitExpirer`。与 `waker` 共用同一份
    #: registry / recovery / saga —— 两支队列回答的是两个问题，
    #: 但"父 Run 是谁、它的账本在哪"只能有一个答案（B-7）。
    expirer: Any = None,
    #: 空洞 217 / D-27：解开阻塞之后把父 Run 往前推的那个驱动。
    #: 与 `waker` / `expirer` 一样**不给默认值**就静默退化成"解开但不推" ——
    #: 所以这里的可省略指的是"由组合根自己建"，不是"可以不接"。
    driver: Any = None,
    conn: Any = None,
    uow: Any = None,
    factory: Any = None,
    approvals: Any = None,
    snapshots: Any = None,
    child_registry: Any = None,
    kernel: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    signal: StopSignal | None = None,
    sleep: Any = None,
) -> ChildRunConsumerApp:
    """M30 / 空洞 209："派得出去、认得回来"里**认得回来**的那一半。

    五样东西必须是**同一个连接**上的 PG 实现，理由与 `build_control_plane`
    一字不差（A-12：丢了变错）：

        child_registry  结果住在这里（X-5）；丢了 → 结果只活在 Kafka 里
        snapshots       父 Run 靠它重建（R-5）；丢了 → 唤醒时 404
        processed       去重表；丢了 → 重复投递会二次交付
        approvals       重建父 Run 要它；丢了 → 父 Run 的治理状态对不上
        kernel          重建出来的栈要它；丢了 → 父 Run 的 Execution 在内存里

    尤其注意 `child_registry` 与 `snapshots` 必须是栈里那一份：
    `waker` 与 `factory` 共用它们，两边指向不同的实例就等于
    "子 Run 登记在一本账上、父 Run 恢复时查另一本"。
    """
    from packages.agent_harness.adapters.postgres import PostgresApprovalStore
    from packages.agent_runtime.adapters.postgres import (
        PostgresChildRunRegistry,
        PostgresCompensationStore,
        PostgresRunCancellationStore,
        PostgresRunSnapshotStore,
    )
    from packages.agent_runtime.child_wake import ChildRunWaker
    from packages.agent_runtime.driving import InProcessRunDriver
    from packages.agent_runtime.recovery import RunRecovery
    from packages.agent_runtime.saga import SagaCoordinator
    from packages.execution_kernel.adapters.kafka import KafkaEventConsumer
    from packages.execution_kernel.adapters.postgres import (
        PostgresProcessedEventStore,
    )

    conn = conn if conn is not None else pg_connection(config)
    uow = uow if uow is not None else pg_unit_of_work(conn)
    kernel = kernel if kernel is not None else build_kernel(config, conn=conn)
    snapshots = (
        snapshots if snapshots is not None else PostgresRunSnapshotStore(conn)
    )
    registry = (
        child_registry
        if child_registry is not None
        else PostgresChildRunRegistry(conn)
    )
    approvals = approvals if approvals is not None else PostgresApprovalStore(conn)
    # D-13：账本只有**一本**。唤醒路径登记孤儿副作用用的必须与栈里那条
    # `SagaCoordinator` 是同一个 store —— 否则"子 Run 的副作用"记在一处、
    # "父 Run 的撤销"读另一处，A-12 判据里这就是"变错"：
    # 两条都成立，拼起来是假的。
    compensations = (
        compensations if compensations is not None else pg_compensation_store(conn)
    )
    # M34：重建出来的父 Run 也要有同一条取消通道 ——
    # 否则唤醒路径上那条父 Run 想级联取消子 Run 时写不进意图表，
    # `AgentLoop._cancel_pending_child` 会静默跳过（B-9 只停一半）。
    cancellations = (
        cancellations
        if cancellations is not None
        else pg_cancellation_store(conn)
    )

    # 唤醒路径（有结果没交回）与到期路径（没结果且等不到了）
    # 用的是**同一条**恢复通道：都要把父 Run 装载回来。
    # 刻意不建两条 —— "父 Run 是怎么被装载回来的"只许有一个定义（B-7）。
    recovery = RunRecovery(
        snapshots=snapshots,
        factory=(
            factory
            if factory is not None
            else load_stack_factory(
                config,
                child_registry=registry,
                kernel=kernel,
                snapshots=snapshots,
                compensations=compensations,
                cancellations=cancellations,
            )
        ),
        approvals=approvals,
    )
    # D-27：解开阻塞的人必须把父 Run 往前推。两条路径共用**同一个**驱动 ——
    # "一条 Run 是怎么被推进的"只许有一个定义（B-7），而且它俩用的
    # 本来就是同一条恢复通道（上面那个 `recovery`）。
    if driver is None:
        driver = InProcessRunDriver(recovery=recovery)
    if waker is None:
        waker = ChildRunWaker(
            registry=registry,
            recovery=recovery,
            saga=SagaCoordinator(store=compensations),
            driver=driver,
        )
    if expirer is None:
        from packages.agent_runtime.child_wait import ChildRunWaitExpirer

        expirer = ChildRunWaitExpirer(
            registry=registry,
            recovery=recovery,
            saga=SagaCoordinator(store=compensations),
            driver=driver,
        )
    if processed is None:
        processed = PostgresProcessedEventStore(conn)
    if consumer is None:
        consumer = KafkaEventConsumer(kafka_consumer(config))

    app = ChildRunConsumerApp(
        consumer=consumer,
        waker=waker,
        processed=processed,
        expirer=expirer,
        signal=signal,
        sleep=sleep,
        uow=uow,
    )
    app.subscribe()
    return app


def build_api(
    config: RuntimeConfig,
    *,
    control_plane: Any = None,
    conn: Any = None,
    uow: Any = None,
    identity_provider: Any = None,
    metrics: Any = None,
) -> Any:
    """FastAPI app。框架绑定在 `apps/api/app.py`（PR-22），这里只负责把它拿来。

    PR-31：`uow` 与 `control_plane` 必须指向**同一个连接** ——
    否则"这个请求的写在哪个事务里"就有了两个答案，
    而提交其中一个不会让另一个生效。

    M105 / IAM：没显式注入认证器时，按 `AGENTOS_IDENTITY_TOKENS` 建一个
    静态表；**没配就是 None**（不认证）—— 保持既有部署行为不变。
    """
    from .api.app import build_app

    if identity_provider is None and config.identity_tokens:
        from packages.agent_api.identity import InMemoryIdentityProvider

        identity_provider = InMemoryIdentityProvider.from_json(config.identity_tokens)

    if control_plane is None:
        if conn is None:
            conn = pg_connection(config)
        control_plane = build_control_plane(config, conn=conn)
    elif conn is None:
        # 调用方注入了 control_plane 却没给连接 —— 那就没有事务边界。
        # 刻意**不**去猜一个：猜来的那个会让 X-3 看起来成立而实际不成立。
        conn = None
    if uow is None and conn is not None:
        uow = pg_unit_of_work(conn)

    # M68：就绪探针。**组合根**才知道 DSN 是什么 —— 清单模式下它藏在
    # TOML 里，环境变量里根本没有，路由层没法自己去翻。
    def _readiness() -> Any:
        from apps.probe import check_ready

        return check_ready(config.pg_dsn)

    # M7：业务指标取数器。路由层不认识 SQL（A-1），所以取数在组合根。
    if metrics is None and conn is not None:
        metrics = _pg_business_metrics(conn)

    return build_app(
        control_plane,
        uow=uow,
        readiness=_readiness,
        identity_provider=identity_provider,
        metrics=metrics,
    )


def _pg_business_metrics(conn: Any) -> Any:
    """返回一个 `() -> Sequence[Metric]` —— 从库里读业务计数。

    队列深度 = `executions` 里 PENDING 的条数。它是 KEDA 扩 worker 的那个数：
    CPU 高可能只是在一件长任务上，队列深度高才是"活干不完"。
    """
    from packages.agent_api.metrics import business_metrics

    def _read() -> Any:
        counts = {"PENDING": 0, "RUNNING": 0, "SUSPENDED": 0}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) AS n FROM executions GROUP BY status"
            )
            for row in cur.fetchall():
                if row["status"] in counts:
                    counts[row["status"]] = int(row["n"])
            cur.execute(
                "SELECT count(*) AS n FROM approvals WHERE status = 'pending'"
            )
            pending_approvals = int(cur.fetchone()["n"])
        return business_metrics(
            pending_executions=counts["PENDING"],
            running_executions=counts["RUNNING"],
            suspended_executions=counts["SUSPENDED"],
            pending_approvals=pending_approvals,
        )

    return _read


def stop_signal() -> StopSignal:
    """入口文件显式调用它来响应 SIGTERM / SIGINT。

    刻意**不是**组合根的默认行为：`SignalStop()` 一构造就注册信号处理器，
    那是全局副作用 —— 测试进程里注册它只会带来麻烦
    （同一个进程跑 490 个用例，谁先注册谁说了算）。
    """
    return SignalStop()


__all__ = [
    "ConfigurationError",
    "RuntimeConfig",
    "build_api",
    "build_cancellation_sweeper",
    "build_child_run_consumer",
    "build_control_plane",
    "build_executors",
    "build_kernel",
    "build_outbox_publisher",
    "build_recovery_controller",
    "build_run_cancellation_sweeper",
    "build_wakeup_controller",
    "build_worker",
    "build_worker_app",
    "describe_coverage",
    "kafka_consumer",
    "kafka_producer",
    "load_stack_factory",
    "pg_connection",
    "redis_client",
    "stop_signal",
]
