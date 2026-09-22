"""DSN 的解析 —— 只有一处（M71）。

--------------------------------------------------------------------------
它是被一次真实部署逼出来的

`python -m apps.migrate apply` 在集群里报的是：

    NO_DSN: empty dsn; set AGENTOS_PG_DSN or pass --dsn

而那份 Pod 里清清楚楚挂着 `AGENTOS_MANIFEST=/etc/agentos/agentos.toml`，
清单里写着 `storage.pg_dsn`。

原因是迁移器自己去找了 `AGENTOS_PG_DSN` 环境变量 ——
于是"这一次部署的配置"有了**两个来源**：清单和环境变量。
而清单模式的定义就是"清单是唯一声明源"（`RuntimeConfig.from_env`），
迁移器与探针绕过了它，成为这条规矩上的两个缺口。

后果正是对"两个来源"的经典后果：
    清单里改了 DSN，迁移器读的是环境变量 → 它连到旧的那个库上，
    把迁移应用到**另一个**库，然后报"up to date"。
    一次看不出任何问题的成功。

--------------------------------------------------------------------------
优先级

    --dsn（命令行）    > 清单（$AGENTOS_MANIFEST） > $AGENTOS_PG_DSN

命令行最高，是人对"我显式指定了"的期待。

清单高于环境变量，与 `RuntimeConfig.from_env()` 完全一致：
清单模式是"这次启动只认这一个来源"，不是"清单再叠加一层环境变量"。
两者顺序必须一致 —— 否则"同一个进程里，bootstrap 连 A 库、
迁移器连 B 库"这种事就没有任何机制能阻止它发生。
"""
from __future__ import annotations

import os
from typing import Mapping


def resolve_dsn(explicit: str = "", env: Mapping[str, str] | None = None) -> str:
    """按上面的优先级给出 DSN。给不出就返回空串，由调用方点名报错。"""
    if explicit.strip():
        return explicit.strip()

    source = env if env is not None else os.environ

    manifest_path = (source.get("AGENTOS_MANIFEST") or "").strip()
    if manifest_path:
        from packages.agent_manifest import load

        dsn = load(manifest_path).get("storage.pg_dsn") or ""
        if dsn:
            return str(dsn)

    return (source.get("AGENTOS_PG_DSN") or "").strip()


def hint() -> str:
    """报错里那句"你该配什么"。

    三种来源都要说出来：只说其中一个，会让用另外两种的人
    以为自己配错了地方。
    """
    return (
        "set AGENTOS_MANIFEST (with storage.pg_dsn), "
        "or AGENTOS_PG_DSN, or pass --dsn"
    )
