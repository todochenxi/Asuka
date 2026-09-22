"""真 PostgreSQL 的连接与库重置。

三条硬规则：

1. **没有 PG 就跳过。** 缺 psycopg、连不上、或 `AGENTOS_SKIP_IT=1` 时
   一律 `SkipTest`。集成层永远不许让"没装数据库"变成红灯 ——
   否则 629 个单元测试的价值就被这一层绑架了。

2. **每个用例都用全新 schema。** `DROP SCHEMA public CASCADE` 比
   DROP/CREATE DATABASE 快，也避免连接串里的库名变化。
   用例之间不留任何残留，于是"上一个用例污染了下一个"这类
   只在集成层才出现的偶发红没有立足之地。

3. **走的是生产同一批 SQL 文件。** 不复制、不改写；
   `infrastructure/postgres/*.sql` 的原文在真 PG 上执行一次。

4. **同一时刻只许一个进程在跑。** 推平 schema 是破坏性的，
   两个进程互相拆台的产物是一地跟被测代码无关的红灯
   （`InvalidSchemaName`、"表不存在"、随机失败……）。
   所以整场运行攥着一把 PG 咨询锁：第二个进程排队，不抢。
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: 覆盖它就能指向任意 PG（CI、远程、另一个端口）。
DSN_ENV = "AGENTOS_PG_DSN"

#: 本机 Docker Desktop 里那个 `pgvector/pgvector:pg16`，5432 → 5433。
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agentos_it"

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "infrastructure" / "postgres"

#: 推平 schema 的那个咨询锁（见 `real_pg`）。值本身没有含义，
#: 只要和同一个库里别的咨询锁不撞上就行。
SCHEMA_RESET_LOCK_KEY = 728_394_115

_probe_cache: dict[str, Any] = {}


def dsn() -> str:
    return os.environ.get(DSN_ENV) or DEFAULT_DSN


def _admin_dsn(target: str) -> str:
    """把 DSN 里的库名换成 `postgres` —— 建/删库要用它。"""
    head, _, _ = target.rpartition("/")
    return f"{head}/postgres" if head else target


def migration_names() -> list[str]:
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


def far_enough_deadline(hours: int = 5) -> str:
    """一个"还在平台上限之内、但测试期间绝不会到期"的等待上限。

    015 只要求 `wait_until` 有值且在 `spawned_at` 之后，于是夹具里
    曾经写着 `'2099-01-01T00:00:00+00:00'` —— 那是"等到世界末日"的
    另一种写法，而它当时是**合法**的。

    016 给 `wait_until` 加了平台上限（6 小时，D-32）之后它就不合法了：
    一次派生不许把父 Run 挂到明年，哪怕是在夹具里。
    所以夹具改用"派生后 5 小时" —— 仍在 6 小时之内，
    且在测试跑完之前绝不会到期（语义与 2099 完全一致）。
    """
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def migration_sql() -> str:
    """全部迁移的原文，按文件序 —— 与生产迁移顺序一致。"""
    return "\n".join((MIGRATIONS_DIR / n).read_text(encoding="utf-8") for n in migration_names())


def _connect(target: str, *, autocommit: bool = True) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 没装驱动
        raise unittest.SkipTest(f"psycopg is not installed: {exc}") from exc
    return psycopg.connect(target, autocommit=autocommit, row_factory=dict_row)


def real_pg(*, fresh: bool = True) -> Any:
    """拿一个真 PG 连接；不可用就 `SkipTest`。

    `fresh=True` 时先把 `public` schema 推平再重跑全部迁移 ——
    于是每个用例起点都是"刚迁移完的空库"。
    """
    if os.environ.get("AGENTOS_SKIP_IT"):
        raise unittest.SkipTest("AGENTOS_SKIP_IT is set")

    target = dsn()
    cache_key = f"ok:{target}"
    if cache_key not in _probe_cache:
        try:
            conn = _connect(target)
            conn.close()
            _probe_cache[cache_key] = True
        except Exception as exc:
            # 库不存在 → 建一次；建不了就跳过（而不是让测试变红）
            try:
                admin = _connect(_admin_dsn(target))
                admin.execute(f'CREATE DATABASE "{target.rpartition("/")[2]}"')
                admin.close()
                _probe_cache[cache_key] = True
            except Exception as exc2:
                _probe_cache[cache_key] = False
                raise unittest.SkipTest(
                    f"no PostgreSQL at {_hide(target)}: "
                    f"{type(exc).__name__}: {str(exc)[:80]} / {type(exc2).__name__}"
                ) from exc2

    conn = _connect(target)
    if fresh:
        # 只锁"推平"那一刻是**不够的**：A 推完平去跑用例了，
        # B 的推平会把 A 脚下的表抽走 —— 于是 A 不是死在推平里，
        # 而是死在自己用例的正中间（实测 110 条里 87 条红，
        # 报错五花八门，没有一条指向真凶）。
        #
        # 所以这把锁的粒度是**整场运行**：谁先开始谁跑完，
        # 第二个进程在自己的第一次 `real_pg()` 上等着。
        # 等待比互相拆台好 —— 至少红灯是真的（PR-19）。
        _hold_schema_lock(target)
        # `IF EXISTS`：上一次跑被中断（超时被杀 / 事务被卡住）时
        # `public` 可能已经没了，下一次跑就死在 `InvalidSchemaName` 上 ——
        # 一个跟被测代码毫无关系的原因，却让整层集成测试再也起不来。
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(migration_sql())
    return conn


#: 那把锁住的连接。刻意**不是**测试用的连接：
#: 它只在进程里活一份，从第一次推平一直攥到进程结束 ——
#: 于是"两个进程同时跑集成层"变成"第二个排队"。
#: 进程被杀时 PG 会在连接断开时自动放锁，不会留下死锁。
_lock_conn: Any = None


def _hold_schema_lock(target: str) -> None:
    global _lock_conn
    if _lock_conn is not None:
        return
    conn = _connect(target)
    # 阻塞式：拿不到就等。等到的时候，上一个进程已经跑完了。
    conn.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_RESET_LOCK_KEY,))
    _lock_conn = conn


def _hide(dsn_value: str) -> str:
    """DSN 里的密码不该进测试输出。"""
    if "@" in dsn_value and ":" in dsn_value:
        head, _, tail = dsn_value.rpartition("@")
        scheme, _, rest = head.partition("//")
        user = rest.split(":")[0] if "//" in head else rest
        return f"{scheme}//{user}:***@{tail}"
    return dsn_value


class RealPostgresCase(unittest.TestCase):
    """基类：`self.conn` 是一个迁移到最新状态的真 PG 连接。"""

    def setUp(self) -> None:
        self.conn = real_pg()
        self.addCleanup(self.conn.close)
