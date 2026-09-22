"""集成测试：跑在**真实 PostgreSQL** 上。

--------------------------------------------------------------------------
为什么要有这一层

`tests/unit/` 里所有"PG 适配器"测试其实跑在 `sqlite_shim` 上。它做三件翻译
（`%s`→`?`、`now()`→字符串、JSON 列反序列化），其余一律放行。于是下面这些
**从来没有被 PostgreSQL 真正验过**：

    `::jsonb`            被 shim 剥掉（sqlite 里是语法错误）
    `ON CONFLICT DO NOTHING`  sqlite 恰好也有同名语法，于是"碰巧"通过
    `CHECK (… <> '{}'::jsonb)`  剥掉 cast 后变成字符串比较，"碰巧"等价
    `TIMESTAMPTZ`        shim 用 register_converter 模拟
    `rowcount`           语义由 shim 自己实现

每一处都碰巧对上，但没有一处是被 PG 认可的。这正是 PR-23 说的那种
"测试全绿但从未触碰被约束的那一层"——**本层就是为它存在的**。

--------------------------------------------------------------------------
默认行为：没有 PG 就**跳过**，绝不报红

    python -m unittest discover -s tests -t .      # 有 PG 就跑，没有就 skip

`packages/` 依然零第三方依赖： psycopg 只在这里被导入，且是惰性的。
"""
