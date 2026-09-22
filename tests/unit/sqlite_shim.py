"""测试替身：让 PG 方言的 SQL 能在 sqlite 上跑起来。

**这不是生产代码。** 它的唯一目的是：在没有 psycopg / 没有真实数据库的机器上，
仍然验证 `001_kernel.sql` 与 `adapters/postgres.py` 的**语义**：
乐观锁（E-13）、唯一约束（E-19 / E-6 / E-21）、CHECK 约束（E-8）、Outbox（X-3）。

它只做五件翻译，不模拟任何业务行为：
    1. `%s` → `?`，`= ANY(%s)` → `IN (?, ?, ...)`
    2. `now()` → 与 datetime 适配器同格式的字符串（保证时间比较是字典序正确的）
    3. 行 → Mapping，且 JSON 列自动反序列化
    4. `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ...` 整句丢掉
       （sqlite 加不了外键）；于是 013 的外键只由真 PostgreSQL 验证
    5. `col + interval 'N unit'` → `datetime(col, '+N units')`
       （014 回填历史取消意图的等待上限）

四、五都只是**方言**，不是语义：替身跳过或改写的那几句，
判定的责任落在真 PostgreSQL 上（见 `tests/integration/`）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_DT_FMT = "%Y-%m-%d %H:%M:%S.%f"

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "postgres" / "001_kernel.sql"
)


def _dt_adapter(value: datetime) -> str:
    """统一成 UTC 无时区的定长字符串 —— 这样 `<` 比较才等价于时间先后比较。"""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime(_DT_FMT)


sqlite3.register_adapter(datetime, _dt_adapter)

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S%z",
)


def _timestamp_converter(value: bytes | str) -> datetime:
    """TIMESTAMPTZ → datetime。

    真实 psycopg 会返回 datetime；替身必须保持一致，否则
    `Lease.is_expired(now)` 会拿 datetime 去比字符串。
    """
    if isinstance(value, bytes):
        value = value.decode()
    parsed: datetime | None = None
    for fmt in _TS_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        parsed = datetime.fromisoformat(value)
    # TIMESTAMPTZ 在 PG 里返回的一定是**带时区**的；库里存的始终是 UTC
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


sqlite3.register_converter("TIMESTAMPTZ", _timestamp_converter)
sqlite3.register_converter("JSONB", json.loads)


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_DT_FMT)


def _looks_like_json(value: Any) -> bool:
    return isinstance(value, str) and value[:1] in ("{", "[")


class DictRow(Mapping):
    """sqlite3.Row 的 Mapping 包装：支持 `row["x"]` 与 `row.get("x")`。"""

    __slots__ = ("_data",)

    def __init__(self, row: sqlite3.Row) -> None:
        data: dict[str, Any] = {}
        for key in row.keys():
            value = row[key]
            if _looks_like_json(value):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            data[key] = value
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"DictRow({self._data!r})"


def _strip_jsonb_cast(sql: str) -> str:
    """`::jsonb` 在 sqlite 里是语法错误 —— DDL 与 DML 都要剥掉。

    DML 里的强制转换（`%s::jsonb`）以前没人测到，因为只有 007 之后
    才有测试真的往 JSONB 列里写带 cast 的值。剥掉不影响语义：
    JSONB 列在 sqlite 里就是 TEXT，本来也不需要转换。
    """
    return sql.replace("::jsonb", "")


def _translate(sql: str, params: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    """PG 占位符 → sqlite 占位符。"""
    sql = _strip_jsonb_cast(sql)
    params = list(params)
    parts = sql.split("ANY(%s)")
    if len(parts) == 1:
        return sql.replace("%s", "?"), tuple(params)

    out_sql = ""
    out_params: list[Any] = []
    for i, part in enumerate(parts):
        n = part.count("%s")
        out_sql += part.replace("%s", "?")
        out_params.extend(params[:n])
        params = params[n:]
        if i < len(parts) - 1:
            items = list(params.pop(0))
            out_sql = out_sql.rstrip()
            if out_sql.endswith("="):
                out_sql = out_sql[:-1].rstrip() + " "
            out_sql += "IN (" + ", ".join("?" * len(items)) + ")"
            out_params.extend(items)
    return out_sql, tuple(out_params)


#: sqlite 的 `ALTER TABLE` 能加 CHECK（`ADD CONSTRAINT ... CHECK` 是过的），
#: 但**加不了 FOREIGN KEY** —— `near "FOREIGN": syntax error`。
#: 013 那根 `executions.task_id → tasks.task_id` 的外键
#: 因此**只能在真 PostgreSQL 上生效**（见 `tests/integration/`）。
#: 替身选择"跳过"而不是"仿造"：仿造一个 sqlite 外键会让人以为
#: E-27 被测到了，而它其实没被那个数据库判定过（PR-23 / PR-26）。
#:
#: 正则只认带 FOREIGN KEY 的那一句，010~012 的 CHECK 不受影响。
_ALTER_ADD_FK_RE = re.compile(
    r"ALTER\s+TABLE\s+[\w.\"]+\s+ADD\s+CONSTRAINT\b[^;]*?\bFOREIGN\s+KEY\b[^;]*;",
    re.IGNORECASE | re.DOTALL,
)


#: `014_cancellation_grace.sql` 那句回填写的是 PG 方言
#:   `requested_at + interval '15 minutes'`
#: sqlite 不认 `interval` 字面量，但认 `datetime(x, '+15 minutes')`。
#: 再加一层 `strftime` 是为了**格式对齐**：适配器把 datetime 存成
#: `'%Y-%m-%d %H:%M:%S.%f'`，而 `datetime()` 返回的是不带小数秒的
#: `'%Y-%m-%d %H:%M:%S'`。两种格式混在同一列里，
#: 字典序比较仍然是"时间先后"正确的（短的那个是长的前缀），
#: 但会让"替身与真库是同一条约束"这句话变得需要解释 ——
#: 对齐掉就不需要解释了。
_INTERVAL_ADD_RE = re.compile(
    r"(?P<col>\w+)\s*\+\s*interval\s*'(?P<n>\d+)\s*(?P<unit>second|minute|hour|day)s?'",
    re.IGNORECASE,
)


def _to_sqlite_interval(sql: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        # ⚠️ 这里必须是**单个** `%`：这不是一个 Python 格式串，`%%` 不会被
        # 折叠，它会原样进到 SQL 里，而 sqlite 的 `strftime` 不认 `%%` ——
        # 实测 `strftime('%%Y-%%m-%%d', ...)` 返回的就是字面量 `'%Y-%m-%d'`。
        # 于是 014 的回填在替身上写进去的是一句格式串而不是一个时刻，
        # 016 的 CHECK 也永远判不成（M43 修掉的第一件事）。
        return (
            "strftime('%Y-%m-%d %H:%M:%S.%f', "
            f"datetime({m.group('col')}, '+{m.group('n')} {m.group('unit')}s'))"
        )

    return _INTERVAL_ADD_RE.sub(repl, sql)


def _to_sqlite_ddl(sql: str) -> str:
    """只改 PG 特有的字面量，表/约束/索引的形状原样保留 —— 这样测的才是真 schema。

    两个例外：
      · `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ...` 整句丢掉
        （sqlite 加不了外键），代价见 `_ALTER_ADD_FK_RE` 那段注释；
      · `col + interval 'N unit'` 翻译成 `datetime(col, '+N units')`
        （014 的回填），见 `_INTERVAL_ADD_RE` 那段注释。
    """
    sql = _strip_jsonb_cast(sql)
    sql = sql.replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP")
    sql = _to_sqlite_interval(sql)
    return _ALTER_ADD_FK_RE.sub("", sql)


class Cursor:
    def __init__(self, raw: sqlite3.Cursor) -> None:
        self._raw = raw

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    def execute(self, sql: str, params: Sequence[Any] = ()) -> "Cursor":
        self._raw.execute(*_translate(sql, params))
        return self

    def fetchone(self) -> DictRow | None:
        row = self._raw.fetchone()
        return DictRow(row) if row is not None else None

    def fetchall(self) -> list[DictRow]:
        return [DictRow(r) for r in self._raw.fetchall()]

    def close(self) -> None:
        self._raw.close()


class Connection:
    """最小 DB-API 表面：`cursor()`。"""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        raw.create_function("now", 0, _now)

    def cursor(self) -> Cursor:
        return Cursor(self._raw.cursor())

    def executescript(self, sql: str) -> None:
        """建 / 迁移表用（DDL 走同一套翻译）。"""
        self._raw.executescript(_to_sqlite_ddl(sql))

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def __del__(self) -> None:
        """兜底：忘了 close 也不要在测试输出里刷 ResourceWarning。"""
        try:
            self._raw.close()
        except Exception:  # pragma: no cover - 解释器关闭期可能已失效
            pass


#: 默认建库要吃哪些迁移。
#:
#: 只有 `001_kernel.sql` 是不够的 —— `017_execution_cancel_attribution.sql`
#: 给 `executions` 加的那两列是 **PG 适配器的功能必需**：
#: `SELECT_EXECUTION` 与 `UPDATE_EXECUTION` 都会读写它们，
#: 少了就是 `no such column`，整张表都查不了。
#:
#: 而 `013`（executions.task_id 的外键）**不**在这里 —— 它是"额外保证"：
#: sqlite 加不了 FK，跳过它只是 E-27 没被测到，功能照样跑
#: （见上面 `_ALTER_ADD_FK_RE` 那段注释）。
#: 一个是缺了就崩，一个是缺了就少一层保护，所以处置不同。
DEFAULT_SCHEMA = ("001_kernel.sql", "017_execution_cancel_attribution.sql")

#: `001_kernel.sql` 建了 `executions`，`017` 给 `executions` 加必需列 ——
#: 所以**只要有 001，就必须有 017**，否则那张表查不了。
#:
#: 为什么是"自动补"而不是让每个调用方记得写上：
#: 一堆测试各自列着自己关心的迁移（`schema_sql=load_schema_sql("001", "004", ...)`），
#: 它们关心的是 Run 快照、子 Run 那些事，**没有一条**关心 Execution 的归因列。
#: 让它们逐个补，等于把"这张表现在少两列"这件事
#: 摊派给每一个后来者去重新发现一遍（B-7：一个事实一处定义）。
_EXECUTIONS_ATTRIBUTION = "017_execution_cancel_attribution.sql"


def load_schema_sql(*names: str) -> str:
    """读 `infrastructure/postgres/` 下的迁移文件（默认见 `DEFAULT_SCHEMA`）。"""
    if not names:
        names = DEFAULT_SCHEMA
    elif "001_kernel.sql" in names and _EXECUTIONS_ATTRIBUTION not in names:
        names = tuple(names) + (_EXECUTIONS_ATTRIBUTION,)
    parts = []
    for name in names:
        path = SCHEMA_PATH.parent / name
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def connect(*extra_sql: str, schema_sql: str | None = None) -> Connection:
    """建库：默认直接吃 `infrastructure/postgres/001_kernel.sql` 的原文。

    `extra_sql` / `schema_sql` 用于追加后续迁移（例如 002_outbox_consumer.sql）。
    """
    raw = sqlite3.connect(
        ":memory:", isolation_level=None, detect_types=sqlite3.PARSE_DECLTYPES
    )
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.executescript(_to_sqlite_ddl(schema_sql or load_schema_sql()))
    conn = Connection(raw)
    for sql in extra_sql:
        conn.executescript(sql)
    return conn
