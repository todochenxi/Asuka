"""迁移器的实现。

分两层，边界是明确的：

    **纯层**（零依赖、可单测）
        `discover` / `checksum` / `unwrap_transaction` / `Plan`
        —— 只认文件与字符串，不认数据库。

    **库层**（惰性 import psycopg）
        `connect` / `read_applied` / `apply_pending`
        —— 没有驱动就 `MigrationError(NO_PG_DRIVER)`，
           绝不让"没装驱动"在 import 阶段炸掉（PR-15）。

这个边界和集成测试那条"没装 psycopg 就 SkipTest，不许变红"是同一条规矩，
只是换到了生产入口上：**缺依赖要说清楚缺什么，不是崩在 import 上**。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 迁移文件的位置。与集成测试读的是**同一批文件**（B-7：不复制一份）。
DEFAULT_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "postgres"
)

#: 账本表。它自己也是被迁移器建的，所以不在 001 里 ——
#: 001 是"业务 schema 的第一版"，账本是迁移器的，不是业务的。
BOOKKEEPING_TABLE = "agentos_schema_migrations"

#: 并发迁移的会话咨询锁。两个 Pod 同时跑 migrate Job 时排队，不抢。
#: 值本身没有含义，只要和别的咨询锁不撞上。
MIGRATION_LOCK_KEY = 418_207_733


class MigrationError(RuntimeError):
    """迁移干不下去。带一个 `code`，调用方靠它决定退出码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 纯层
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """一个迁移文件。"""

    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return checksum(self.sql)


def checksum(sql: str) -> str:
    """内容的指纹。用来发现"已上线的迁移被改过"。

    取前 16 位就够了：它只需要区分同一批文件的两个版本，
    不是抗碰撞 —— 真要有人刻意碰撞，他早就能直接改库了。
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def unwrap_transaction(sql: str) -> tuple[str, bool]:
    """剥离文件首尾自带的 `BEGIN;` / `COMMIT;`，返回 `(body, 剥没剥)`。

    为什么要剥：17 个文件里 8 个自带事务包裹、9 个不带。
    若在 autocommit 下执行原文，自带的那 8 个会在**自己的**事务里提交，
    于是"迁移"与"记一笔已应用"落在两个事务里 —— 中间崩一次就留下
    "库改了、账上没记"的账实不符，而这是整套系统最忌讳的形态。

    剥掉之后，迁移器统一把它们和记账放进**同一个**事务：
    要么这一条既改了库也记了账，要么都没发生。

    只有首尾成对出现才剥。文件中间的 `COMMIT;` 不碰 ——
    那种写法是作者有意分段，替他合并是另一种篡改。
    """
    lines = sql.splitlines()
    start, end = 0, len(lines)
    while start < end and _is_blank_or_comment(lines[start]):
        start += 1
    while end > start and _is_blank_or_comment(lines[end - 1]):
        end -= 1
    body = lines[start:end]
    if len(body) < 2:
        return "\n".join(body), False

    if (
        body[0].strip().lower().rstrip(";") == "begin"
        and body[-1].strip().lower().rstrip(";") == "commit"
    ):
        return "\n".join(body[1:-1]), True
    return "\n".join(body), False


def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return (not s) or s.startswith("--")


def discover(directory: Path | str | None = None) -> tuple[Migration, ...]:
    """按文件序读出全部迁移。

    顺序**只**来自文件名（`001_` … `017_`）—— 这是这套约定唯一可靠的地方：
    文件的 mtime 在 checkout / 打包后会变，靠它排序能得到任何顺序。
    """
    d = Path(directory) if directory is not None else DEFAULT_MIGRATIONS_DIR
    if not d.is_dir():
        raise MigrationError("NO_MIGRATIONS_DIR", f"{d} is not a directory")

    paths = sorted(p for p in d.glob("*.sql") if p.is_file())
    if not paths:
        raise MigrationError("NO_MIGRATIONS", f"{d} contains no .sql files")

    out: list[Migration] = []
    for p in paths:
        out.append(Migration(name=p.name, sql=p.read_text(encoding="utf-8")))
    return tuple(out)


@dataclass(frozen=True)
class Plan:
    """`已应用的` 与 `磁盘上的` 对照之后的结果。"""

    pending: tuple[Migration, ...] = ()
    drifted: tuple[tuple[str, str, str], ...] = ()
    """"`(name, 已应用时的指纹, 现在磁盘上的指纹)` —— 已上线的迁移被改过。"""

    applied_count: int = 0

    @property
    def ok(self) -> bool:
        """没有篡改 = 可以继续。有待应用不算错，那只是"落后了"。"""
        return not self.drifted

    @property
    def up_to_date(self) -> bool:
        return self.ok and not self.pending


def plan(
    applied: Mapping[str, str],
    migrations: Sequence[Migration],
) -> Plan:
    """纯函数：对照账本与磁盘。"""
    pending: list[Migration] = []
    drifted: list[tuple[str, str, str]] = []

    for m in migrations:
        recorded = applied.get(m.name)
        if recorded is None:
            pending.append(m)
        elif recorded != m.checksum:
            drifted.append((m.name, recorded, m.checksum))

    return Plan(
        pending=tuple(pending),
        drifted=tuple(drifted),
        applied_count=sum(1 for m in migrations if m.name in applied),
    )


def report(p: Plan) -> str:
    """给人看的一行一段。`check` 与 `apply` 共用它（B-7：一种说法）。"""
    lines: list[str] = []
    lines.append(f"applied: {p.applied_count}  pending: {len(p.pending)}")
    for m in p.pending:
        lines.append(f"  pending  {m.name}")
    for name, was, now in p.drifted:
        # 点名 + 两个指纹：让人能直接 diff，而不是只知道"有个文件不对"
        lines.append(f"  DRIFT    {name}: applied {was}, on disk {now}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 库层
# ---------------------------------------------------------------------------


def connect(dsn: str) -> Any:
    """连库。**惰性** import psycopg（PR-15）。

    顶层 `import psycopg` 会让一切用到迁移器的命令在没装驱动的机器上
    直接 ImportError —— 连 `check` 都跑不了。而"缺驱动"要说的话是
    "请先装 psycopg"，不是一段栈。
    """
    if not dsn:
        from apps._dsn import hint

        raise MigrationError("NO_DSN", f"empty dsn; {hint()}")
    try:
        # PR-14：客户端只许**惰性**出现。写成 `import psycopg` 会让
        # `tests.unit.test_bootstrap_and_worker` 的扫描器把它抓出来 ——
        # 那条守卫是对的：顶层 import 会让"没装驱动"变成 import 阶段的崩，
        # 于是 1000 多个不需要数据库的测试一起红。
        import importlib

        psycopg = importlib.import_module("psycopg")
    except ImportError as e:  # pragma: no cover - 取决于机器
        raise MigrationError(
            "NO_PG_DRIVER", f"psycopg is not installed ({e}); pip install 'psycopg[binary]'"
        ) from None
    try:
        return psycopg.connect(dsn)
    except Exception as e:
        # 连不上要说"连不上哪个库"，但不回显密码
        raise MigrationError("CONNECT_FAILED", _safe_dsn_error(dsn, e)) from None


def _scalar(row: Any) -> Any:
    """取一行的第一列，**不挑** `row_factory`。

    迁移器可能被塞进任何一条连接里（测试、运维脚本、别人的组合根），
    而 `tuple_row` 与 `dict_row` 的行取法不同。让它在这件事上挑食，
    换来的报错是 `KeyError: 0` —— 一句完全说不出真发生了什么的话（PR-19）。
    """
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def _safe_dsn_error(dsn: str, e: Exception) -> str:
    """报错里不带口令：日志是会被引用的，口令不该出现在那儿。"""
    head, sep, _ = dsn.rpartition("@")
    shown = f"{head}{sep}***" if sep else "***"
    return f"cannot connect to {shown}: {e}"


def ensure_bookkeeping(conn: Any) -> None:
    """建账本表。它不属于任何一次业务迁移，所以由迁移器自己管。"""
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} (
            name       TEXT PRIMARY KEY,
            checksum   TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def read_applied(conn: Any) -> dict[str, str]:
    """读出已应用的迁移。**只读**：表不存在就是"一个都没跑"。

    刻意不在这里建表 —— `check` 是拿来做上线前门禁的，
    一个"检查一下"的命令不该有写库的副作用。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT to_regclass(%s)", (BOOKKEEPING_TABLE,)
    )
    if _scalar(cur.fetchone()) is None:
        return {}
    cur.execute(f"SELECT name, checksum FROM {BOOKKEEPING_TABLE}")
    out = {str(r[0]): str(r[1]) for r in cur.fetchall()}
    # 只读却留下一个开着的事务，是"没人看见的副作用"：它会让接下来的
    # `commit` 把一次空提交算进去，也会让连接一直报 INTRANS。
    conn.rollback()
    return out


def apply_pending(conn: Any, p: Plan, *, log: Any = None) -> tuple[str, ...]:
    """应用 `p.pending`。**每条一个事务**，失败点名当时的那条。

    为什么不包成一个大事务：17 条里的任何一条失败都会把前面全部回滚，
    于是"已经建好的 12 张表"也被撤销 —— 那比留着它们难收拾得多
    （库被推平过一半，而账本上什么都没有）。

    一条一提交，加上账本同事务，得到的是"崩在哪一条"有唯一答案。
    """
    if not p.ok:
        raise MigrationError(
            "MIGRATION_DRIFT",
            "; ".join(f"{n}: {a} -> {b}" for n, a, b in p.drifted),
        )

    applied: list[str] = []
    cur = conn.cursor()
    for m in p.pending:
        body, _ = unwrap_transaction(m.sql)
        try:
            cur.execute(body)
            cur.execute(
                f"INSERT INTO {BOOKKEEPING_TABLE} (name, checksum) VALUES (%s, %s)",
                (m.name, m.checksum),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            # 点名：哪一条、PG 说了什么。不吞、不重试、不"继续试试下一条"
            raise MigrationError("MIGRATION_FAILED", f"{m.name}: {e}") from None
        applied.append(m.name)
        if log is not None:
            log(f"applied {m.name}")
    return tuple(applied)


def with_lock(conn: Any, key: int = MIGRATION_LOCK_KEY) -> None:
    """拿**会话**咨询锁。两个进程同时跑迁移时后一个排队。

    会话锁（不是 `pg_advisory_xact_lock`）是刻意的：事务锁会在每次
    `commit()` 时释放，而每条迁移都要 commit 一次 —— 那等于没锁。

    刻意**不**在这里切 `autocommit`：
    psycopg 不允许在事务进行中改它（`INTRANS`），而调用方手里的事务
    状态不是迁移器该管的事。会话锁的生命周期是连接，与事务无关，
    所以原地执行就够了。
    """
    conn.cursor().execute("SELECT pg_advisory_lock(%s)", (key,))


def release_lock(conn: Any, key: int = MIGRATION_LOCK_KEY) -> None:
    conn.cursor().execute("SELECT pg_advisory_unlock(%s)", (key,))
