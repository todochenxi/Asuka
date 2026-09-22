# AgentOS 运维 playbook（参考）

> 本文件**不被自动注入** —— 碰到测试、环境、变红脚本、部署相关的问题时**主动读**。
> 核心笔记见同目录 `MEMORY.md`。

## ⭐ 反复踩到的坑 · B. 断言与变异

14. ⭐⭐ **断言隔离**（M85 栽两次）：除"被测那一项"外其他字段一律给足，且拒绝理由**点名**
    被测字段（`assertIn("execution_id", str(exc))`）。否则验证的是"某处会拦"而不是"这一处会拦"。
15. ⚠️ **注释不是变异**（M86）。加 `# noqa: M1` 注释字段还在，什么都没变。变异必须**真的改行为**。
16. **变红脚本优先单行替换**；**锚点命中次数必须 == 1**（否则静默 SKIP）；**每条变异后立刻还原**。
    ⚠️ **重构会让老脚本静默 SKIP**（M89）：重构掉 M88 的判据位置后，`red88.py` 的 M3/M5
    锚点**命中 0 次** → 打印 `⚠️ SKIP` 后继续，**看起来像"这条变异不重要"，
    实际是"它已指不到东西了"**。⇒ **每次重构完，回头跑一遍同族的老变红脚本。**
17. **"红了 0 条"或"基线就不绿"先怀疑脚本，不要先查代码。**
    曾栽：`ROOT = Path(__file__).parent.parent`（脚本在仓库根，该是 `.parent`）。
18. ⭐ **判断不能当结论用**（M83）。"父 Run 自己知道"是关于系统状态的断言，而状态可测。
19. ⭐ **"零代码改动"的一轮是正当的里程碑**（M83）。
20. ⭐ **补完判据跑既有测试一条不红 = 此前没被守过**（**四次同款**：M85 1287 / M87 1319 /
    M88 1334 / M89 1353，同因：既有用例恰好只走"判据成立"那一半的路 —— M89 是替身 Planner
    **全部**写着 `run_id=state.run_id`）⇒ **专门写一条"让两种取法给出不同答案"的用例**。
21. ⭐ **变红验证顺带看"隔离性"**：某条变异**恰好红 1 条**是好信号 —— 那条守点有**专属**用例。

## ⭐ 反复踩到的坑 · C. 信息与状态

22. **"内存里分得清" ≠ "账本上分得清"**（M79）。内存字段 / 快照 / 账本是三件事。
23. ⭐ **加了字段 ≠ 送达**。沿它实际走的路读到终点那一份。
24. ⭐⭐ **`None` 有两义性**（M86）："我没有这个能力"与"我有这个能力但说不出值"混为一谈时，
    第二种会**静默退化成第一种**。⇒ 先 `getattr` 判"有没有"，再判返回值"说没说出值"。
25. ⭐⭐ **两个现成选项都不对时，缺的是第三个**（M82）。**"不确定"是必须被记录的状态。**
26. ⭐ **一个 Store 两种实现（内存 + PG）时，保证必须两边同源**；**保证住在哪一层，就在哪一层
    验证**（PG 物理约束必须写 `*_real_pg.py`）。
27. **并发判胜负唯一可靠写法 = 带条件的 UPDATE + rowcount**，不是"先读再写"。
    （S-4 / S-15 / A-11 / E-25 / PR-3 都是这个形状。）

28. ⭐⭐ **「信息在，但在另一层」**（M89）。计划落在 **State / 快照**，不在 **trace** ——
    实测 trace 只有 `task.submitted` / `execution.observed` / `checkpoint.written`，
    `plan.created` 只在 `state.observations` 里。⇒ **只看 trace 审计不了"这条 Run 用了哪份计划"**。
    这不是缺陷（trace 是事件流），但它决定了那个问题的答案是"不能"。
    ⇒ 问"为什么读不出来"之前，先确认**那个字段活在哪一层**（内存 / 快照 / 账本）。
    与 #22、空洞 234 同族。

## ⭐ 反复踩到的坑 · D. 流程与纪律

29. ⭐ **诊断的归宿是「基线」，不是「任务卡」**（M84）。任务卡是过程，基线是结论。
30. ⭐ **"偶发红"先假定它是真的，但也先翻账本**（M84）。**撞不出来 ≠ 问题不存在。**
31. **交付的判据是"被用上"，不是"被写出来"**。栽过五次。验收 = **改坏它看谁红**。
32. ⚠️ **盘点用"有没有目录/文件"代替"能力是否落地"** —— 栽三次（M4/M7/M14 全判错）。
    正确问法：**这条里程碑描述的那件事，代码里做得到吗？**
33. **空洞登记会过期**，动手前先实证。

## ⭐ 反复踩到的坑 · E. 环境与工具链

34. ⚠️ **改版本号必须在跑测试之前做完，且不许与跑测试并发**（M84+M85）。集成起真 uvicorn 读
    `app.py`（**进程启动时**的快照），基线版本执行断言时**现读文档文件名** → 两边是两个时代的值。
    ⇒ **红灯不一定是被测代码红，也可能是你在它背后改掉了它测的世界。**
    排查：用 `stat` 列**文件时间戳**建时间线（别推算），`log 的 mtime - 测试耗时` = 测试启动时刻。
35. ⭐ **声明点在哪一层，就在哪一层读它**（M84）。版本声明在**文件名**上，别对正文 `read_text()`。
36. ⚠️ **一致性检查只认"声明点"，不扫全文**（M84）。扫全文会把历史记录判成漂移 → 永远红的噪声。
37. ⚠️ **两个 python 不是同一个**。单测 `.../python/versions/3.13.12/python.exe`；集成必须
    `.../python/envs/default/Scripts/python.exe`（系统 python 没装 psycopg/httpx，会得到
    `FAILED (errors=1, skipped=188)` = **一条都没真跑**）。本项目用 **`unittest`**（不是 pytest）：
    `PYTHONPATH=. <py> -m unittest discover -s tests/<dir> -t .`
38. **`real_pg()` 默认 `fresh=True` 会重建库**！附加连接必须 `fresh=False`。
39. **断言 API 错误必须比 `.code`**（错误码在 `.code` 属性上），不能比 `str(e)`。
40. 改动页面必须跑集成测试（丢了全站 `<h1>` 会让 `test_api_real_http.py` 红）；重启服务要确认
    日志有 `Uvicorn running` 再验证。偶发红排查：**完整捕获输出写文件再 grep FAIL**，别 `tail -3`。
41. **起子进程/开连接的测试，`terminate()` 后必须 `wait()`**，否则残留进程连着 PG
    → 下次 `DROP DATABASE` 被拒 → 偶发红一条无关的灯。
42. **迁移铁律**：只增不改，每份只跑一次，**不写 `IF NOT EXISTS`**（sqlite 不认，照 009 写裸
    `ADD COLUMN`）。新迁移必须在 ≥1 个单测的 schema 列表里被引用，否则卫生测试红。
43. **sqlite 替身做等价翻译，不是跳过**（`interval` → `datetime(x,'+N units')`、
    `jsonb_typeof` → `json_type`）。别写 `COMMENT ON`。PG 连接要带 `row_factory=dict_row`。
44. ⚠️⚠️ **变红脚本会真的改源文件 —— 三条纪律**（M87 栽最惨）。第一版还原写在 `try/finally`，
    跑到 M4 被**中途 kill** → `finally` 没跑 → **变异留在 `loop.py` 里**。连锁：全套 89 errors
    （像"我的改动弄红了测试"）→ 备份是**污染之后**拷的 → `git checkout HEAD` 验"HEAD 绿"
    （判断对、结论错）→ 从备份恢复把变异装回去。
    ⇒ **原文件先落盘** + 子进程加 `timeout=`；**备份要在确认干净之后才拷**；
    **看到"既有测试红了"先 grep 变异串**（串要挑独一无二的）。
45. **stdout 重定向到文件是带缓冲的**（日志空着 ≠ 没在跑）→ 用 `python -u` / `flush=True`。
46. ⭐ **"超时"的变异可能指向另一个洞**（M87 的 M4 超时 120s → 修完 I-17 变成红 301 条）。
47. **`probe<N>.py` / `red<N>.py` 提交进仓库** —— 基线文档点名引用，文件不在那句话没法复验。

## 部署编排 / 命令行 / 配置层

```bash
python -m apps.migrate apply                    # 上线前（initContainer 已在跑）
python -m apps.probe live|ready                 # liveness / readiness
AGENTOS_MANIFEST=deploy/config/agentos.toml python -m apps.api
AGENTOS_AGENT_REGISTRY=deploy/config/agents.toml python -m apps.api
python -m apps.cli health|start|status|step|drive|cancel|approvals|decide|trace
python -m apps.cli manifest check|env <file.toml>
python -m apps.eval run ds.json [--out base.json] [--against base.json]
```

- **DSN 唯一来源** = `apps/_dsn.py: resolve_dsn()`（`--dsn` > 清单 > `AGENTOS_PG_DSN`）
- 两个 Kafka 进程 `replicas: 0`（刻意不删，单独文件写明 blocked-on-kafka 原因）
- `deploy/k8s/` 镜像 tag 跟冻结版本走（**14 处**），`test_deploy_manifest.py` 盯着；
  **同 tag 重建 + `IfNotPresent` 会用旧镜像**
- liveness `DEFAULT_LIVE_MAX_AGE=90`（曾设 30，与 sweeper 退避相等 → CrashLoop）
- 真部署暴露的 bug（M73/M74）：① HTTP 推进不落快照 ② `restore()` 不给 `AgentRun` 投影 status
- CLI 地址 `--base` > `$AGENTOS_API_BASE` > `http://127.0.0.1:8011`；退出码 0/2/3/4；零依赖、
  **刻意绕过系统代理**。SDK `request()` 返回 `(status, parsed, raw)`，**status=0 表示连不上**
- 评估平台：**评行为轨迹不评答案**；⭐ **两次都失败 = unchanged，不算 regression**
- **部署清单**（`packages.agent_manifest`，TOML）：环境变量的**唯一声明源**；未知键/缺必填/
  类型错一律**点名拒绝**；与环境变量模式**二选一不叠加**；错在启动前死
- **agent 注册表**（`packages.agent_registry`）：**配了才校验**，没配维持透传
- `deploy/k8s/02-secret.yaml` 含明文 demo 口令，文件头已写明上线前必须替换

## 端口与可选能力

- 能力探测统一 `getattr(port, "method", None)`。先例：`cancel_child` / `registry` / `attempts` / `ProgressBearing`
- **`ProgressBearing`**（M86）：`progress()` + `resume(p)` **成对**；`_try_resume` 里必须有**第二个
  动作**（调完 `resume()` 再自述一次、再比一次），否则"空 `resume()`"就是后门

## 版本库状态（2026-09-22）

- **当前冻结基线 v2.1.78**（M90 收尾）。三处落点同源：文档名 / `apps/api/app.py:91` /
  `deploy/k8s/` **14 处** `agentos:2.1.78-b1`
- 目录 `C:\Users\19644\socialbook\agentos`，远端 `git@github.com:todochenxi/Asuka.git`（private，SSH）
- **目录改名尚未做**：`mv` 报 WinError 32 —— 锁的持有者是**自己的工具链会话**，会话活着就改不了。
  正确做法：退出应用后**在会话之外** `ren agentos Asuka`。⚠️ **只改目录名，不要批量替换内容里的
  `agentos`**（包名/模块/文档标题都是内容）
- 坑：① `git init -b main` 对已初始化的 repo **不改分支** → 用 `git branch -M main`
  ② Windows `failed to execute prompt script` = GCM 弹不出窗（走 SSH / PAT）
