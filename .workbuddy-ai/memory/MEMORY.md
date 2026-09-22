# AgentOS 项目长期笔记

## 定位与判据

企业级 Agent 平台。价值不在"多智能"，在**可靠 / 可观测 / 可审计**。
50+ 条已冻结不变量（B/R/S/D/X/A/E/I/L/C/PR/O 系列）都朝这个方向。
反复出现的判据：**宁可拒绝，不许编造**。

## 规模（v2.1.74，2026-09-22）

| 项 | 数量 |
|---|---|
| Python 源文件 | 258 个 |
| PG 迁移 | 18 份 |
| 单元测试 | **1,319 条全绿** |
| 集成测试（真 PG） | **209 条全绿** |

已完成：基础设施、领域、内核、运行时、控制面、Contracts、部署编排（M67~M75）、
可恢复性收口（M85/M86）。
**未实现**：智能层 M12（Decision Engine / Planner 只有 Protocol，由 DemoPlanner 顶着）、
认知运行 M13（依赖 M12）。部分：开发者平台 M11（SDK/CLI/Manifest 已做，缺 GitOps）。

## 里程碑流程

skill `agentos-milestone`：空洞 → 实现 → 测试 → **变红验证** → **冻结基线**。
缺后两步不算完。**接手时四条检查**：
1. 代码里的 M 编号是不是大于文档里的 M 编号？
2. 有没有**已诊断但未固化**的结论？（翻已完成任务的 description，看"根因是 X"
   却**没有对应测试**的条目 —— M84 就是这么查到的）
3. 版本号三处落点是否同源？（文档名 / `app.py` / k8s 14 处 tag，见 PR-33）
4. 冻结动作与测试运行**不许重叠**（M85 又栽了一次，见 PR-33 末段）

## 活跃空洞

| # | 形状 | 处置 |
|---|---|---|
| 225 | 不存在的 Run 叫停返回 200 unknown | 架构限制（无 runs 表）。页面诚实显示"已落下意图" |
| 231 | 不校验 agent 存在性 | 已由注册表闭合（配了才校验） |
| 234 | `executions` 的取消归因无人读 | 登记不治：Run 级归因已在账本 payload |
| 236 | migrate/probe 无 CLI 子命令 | 登记不治：调用方是编排层不是人 |
| 237 | 部署清单指向 `examples.demo_stack` | **M12 落地时必须回来改这三行** |
| 238 | 预算耗尽不给 State 留痕 | 登记不治；Intelligence 要读"我为什么停了"时必须补 |
| 239 | 子 Run `result` 是自由 JSON，`reason` 无 schema | 登记不治：`_reason` 侧已有诚实降级 |
| 240 | 集成那条版本断言做了**跨时代比较**（左=进程启动快照／右=运行时现读文件名）→ 改文件与跑测试重叠就必红 | 登记不治；纪律：冻结与测试串行 |

## 已冻结的关键不变量（近几轮）

| 不变量 | 内容 |
|---|---|
| **I-6**（细则） | 判据搬进 `Observation.__post_init__`（所有构造路径的汇合处），**双向**：来源是 `EXECUTION_RESULT` ⟹ 必绑非空 `execution_id` + `attempt_no>=1`；非执行来源**不许**绑 `execution_id` |
| **I-10** | 重规划**必须吃预算**（`steps += 1`）。否则一直 REPLAN 的 Run 永远跑下去 |
| **I-11** | 完成前必须没有"**自上次规划以来**"未处理的失败，有则 REPLAN。判据钉在 **Loop 的 FINISH 分支**（不钉引擎） |
| **I-12** | 重规划必须产出**形状不同**的计划（比 `(node_id,name,kind)+constraints`，**不比 plan_id**）。换不出来 → FAILED |
| **I-13** | 委派失败就是一次执行失败，**必须进 State**（`execution.failed`，绑真实 Execution） |
| **I-14** | 查不出来的失败也算。委派等不到回音 → `execution_unresolved` 进 State（**不许冒充 `failed`** —— PR-19）。I-11 判据 = "**这一步没有被证明成功**" |
| **I-15** | 三个终态在父侧**信息量**不同必须分得开：`failed`／`cancelled`（**为什么**，谁+理由）／`unknown`。取消**不许记成"不知道"** |
| **R-7** | 有状态的注入实现（Planner/DecisionEngine）必须 `progress()` 自述 + 能被 `resume(p)` 接上；**恢复时先接上、接不上才点名拒绝**。不许静默重来 —— 那是真实副作用（花钱/外部留痕）。`progress()` 返回 `None` = 实现有 bug，当场拒 |
| **B-12** | 终态声明**必须带原因**，落 `run.finished` payload。`reason` 是必填关键字参数 |
| **D-37** | 子 Run 交给父 Run 的失败原因，必须是它**真正的死因**（不是最后一句 observation）。缺失时说"没记录到"，**不许用 summary 顶替** |
| **PR-33** | 版本号三处落点（文档名 / `apps/api/app.py` / `deploy/k8s` 14 处 tag）必须**同源**，不允许中间状态。由两条不依赖 PG 的单测守着 |
| **B-2** | `AgentRun.status` 是派生值，只能经 `sync()` 写 |
| **B-7** | 一个事实一处定义（清单 vs 环境变量、HTTP 封装只许一份）。Intelligence 的知识不许泄漏进 Runtime |
| **B-8** | 取消/审批归因必填（`reason` + `by`） |
| **PR-14/15** | 客户端必须**惰性** import；`packages/` `apps/` 零第三方依赖 |
| **PR-24** | 对外报的版本必须等于冻结基线 |

## ⭐ 反复踩到的坑

### A. 判据住在哪、覆盖谁

1. ⭐⭐ **承诺句不是机制**（M85）。I-6 docstring 写"只能由这个工厂构造"，
   而 `@dataclass(frozen=True)` 的 `__init__` **是公开的** ——
   **frozen 只保证"不可变"，不保证"构造受控"**。
   ⇒ 判据要住在**所有构造路径的汇合处**（`__post_init__`）；
   补的时候问"除工厂外还有几条路能造出它"（**快照恢复最容易漏**）。
2. ⭐⭐ **一句"这是全部"漏了不在自己手里的那一半，就不是保证是错觉**（M86）。
   `RunSnapshot` 自称装"一次可恢复点的全部数据"，却只管住 Runtime 自己的内存。
   **承诺只管住"跨 Run"，管不住"同一 Run 的一次恢复"。**
   ⇒ 先问：**这个"全部"的边界，是谁划的？**
3. ⭐⭐ **判据读什么，就决定了它能看见什么**（M81）。I-11 写"数 `execution.failed`
   observation" —— 把"什么算失败"**外包给了写 observation 的人**。
   ⇒ 补完不变量后除了问"还能被绕过吗"，还要问：
   **它依赖的那些事实，是不是每一扇门都在写？**
4. ⭐⭐ **判据要挡的是"坏的那条路"，不是"那条路"**（M86）。新判据打红了既有测试时，
   先问打红的这条是不是在描述**正常行为**。R-7 第一版只拒绝、不恢复 →
   打红了 `test_a_suspended_run_is_still_suspended_after_a_restart`（真生产流程）。
   判据的目标是"消灭静默重来"，不是"禁止恢复"。
5. ⭐ **断言"最终不是 X"常常是错的断言**（M81）。写前先问：
   **这条不变量禁止的是"最终状态"还是"直达路径"？**
6. ⭐ **"存在"不等于"被处理"**（M82）。`reducer.py` 末尾有兜底分支，
   任何 observation 都会留在 State 上 → `assertIn(kind, kinds)` 守不住分支。
7. ⭐ **不要为不存在的能力写区分**（M83）。**账本上每一列都该对应一条真实路径。**

### B. 断言与变异

8. ⭐⭐ **断言隔离：除"被测那一项"外，其他字段一律给足**（M85 栽第二次）。
   用 `assertIn("execution_id", str(exc))` 让**拒绝理由点名被测字段**。
   否则多道校验叠加 → 验证的是"某处会拦"而不是"这一处会拦" → 变异假绿。
   与 M82 的"把存在当成被处理"**同一族：断言边界没划在守点上**。
9. ⚠️ **注释不是变异**（M86）。给字段加 `# noqa: M1` 注释，字段还在，什么都没变。
   变异必须**真的改变行为**（改捕获实参、改分支、删校验）。
10. **变红脚本优先用单行替换**：heredoc 多行替换会把 `\n` 转成 `/n` → 语法错 → 计数 0。
11. **变红脚本要断言"替换命中次数 == 1"** —— 锚点不唯一时静默 SKIP。
    曾栽：`"reason": reason,` 在 loop.py 里出现 4 次。
12. **"红了 0 条"或"基线就不绿"先怀疑脚本，不要先查代码。**
    曾栽：`ROOT = Path(__file__).parent.parent`（脚本就在仓库根，应是 `.parent`）
    → 所有子进程跑在仓库外 → 报"基线红了 4 条"。
13. ⭐ **判断不能当结论用**（M83）。"父 Run 自己知道"是**关于系统状态的断言**，
    而系统状态可测。写成有变异守着的测试，它才是一份能承重的结论。
14. ⭐ **"零代码改动"的一轮是正当的里程碑**（M83）。把"读过代码觉得对"
    变成"有变异守着的结论"，本身就值得单独一轮。

### C. 信息与状态

15. **"内存里分得清" ≠ "账本上分得清"**（M79）。查"为什么"先确认字段活在哪层：
    内存字段 / 快照 / 账本，是三件事。
16. ⭐ **加了字段 ≠ 送达**。判断信息到没到：**沿它实际走的路读到终点那一份**。
17. ⭐⭐ **`None` 有两义性**（M86）。"我没有这个能力"与"我有这个能力但说不出值"
    混为一谈时，第二种会**静默退化成第一种** —— 一个疏漏看起来像一次合法的选择。
    ⇒ `getattr(port, "method", None)` 判"有没有"，方法返回值再判"说没说出值"。
18. ⭐⭐ **两个现成选项都不对时，缺的是第三个**（M82）。**"不确定"是一种必须被
    记录的状态，不是"没有状态"。**
19. ⭐ **一个 Store 两种实现（内存 + PG）时，保证必须两边同源**。
    只在一种实现里成立的保证 = 在生产上不成立。
20. **保证住在哪一层，就在哪一层验证**。PG 物理约束必须写 `*_real_pg.py`。
21. **并发判胜负唯一可靠写法 = 带条件的 UPDATE + rowcount**，不是"先读再写"。
    S-4 / S-15 / A-11（`AND status='pending'`）、E-25（`AND version=`）、
    PR-3（`AND (claimed_until IS NULL OR <=%s)`）。

### D. 流程与纪律

22. **诊断也是有归宿的：「写进任务卡」≠「写进基线」**（M84）。
    任务卡是**过程**，基线是**结论**。**这比红灯本身贵得多。**
23. ⭐ **"偶发红"先假定它是真的，但也先翻账本**（M84）。
    **撞不出来 ≠ 问题不存在**，可能只是触发条件不在"跑测试"这个动作里。
24. **交付的判据是"被用上"，不是"被写出来"**。同一坑栽过五次
    （没人用它启动 / 没人读 stack / 页面没调用 / 实现了没冻结）。
    验收方式 = **改坏它看谁红**。
25. **盘点用"有没有目录/文件"代替"能力是否落地"** —— 栽过三次（M4/M7/M14 全判错）。
    正确问法：**这条里程碑描述的那件事，代码里做得到吗？**
26. **空洞登记会过期**，动手前先实证。

### E. 环境与工具链（踩一次就够）

27. ⚠️ **改版本号必须在跑测试之前做完，且不许与跑测试并发**（M84 + M85）。
    集成起真 uvicorn 读 `app.py`（**进程启动时的快照**），
    而基线版本**执行断言时现读文档文件名** → 两边是**两个时代**的值。
    排队现象：测试启动于 19:44:42，我在 19:44:46 改文件 → 红。
    ⇒ **红灯不一定是被测代码红，也可能是你在它背后改掉了它测的那个世界。**
    定位法：用 `stat` 列**文件时间戳**建时间线（别推算），
    `log 的 mtime - 测试耗时` = 测试启动时刻。
28. ⭐ **声明点在哪一层，就在哪一层读它**（M84）。文档的版本声明在**文件名**上，
    我却对它 `read_text()` → 永远匹到 0 次。
29. ⚠️ **一致性检查只认"声明点"，不扫全文**（M84）。扫全文会把**历史记录**
    判成**漂移**，造出一条永远红的噪声，**比没有更坏**。
30. ⚠️ **两个 python 不是同一个**：系统 python 没装 `psycopg` / `httpx`。
    跑集成会得到 `FAILED (errors=1, skipped=188)` —— 188 条**全被跳过**。
    单测用 `.../python/versions/3.13.12/python.exe`；
    集成必须用 `.../python/envs/default/Scripts/python.exe`。
31. ⚠️ 本项目用 **`unittest`**（不是 pytest）。集成命令：
    `PYTHONPATH=. <venv-python> -m unittest discover -s tests/integration -t .`
32. **`real_pg()` 默认 `fresh=True` 会重建库**！附加连接必须 `fresh=False`。
33. **断言 API 错误必须比 `.code`，不能比 `str(e)`** —— 错误码在 `.code` 属性上。
34. **改动页面必须跑集成测试**：页面丢了全站 `<h1>` 会让 `test_api_real_http.py` 红。
35. 重启服务要确认日志里有 `Uvicorn running` 再验证（端口被占会测到旧进程）。
36. 偶发红排查：**完整捕获输出写文件再 grep FAIL**，别 `tail -3`。
37. **起子进程/开连接的测试，`terminate()` 后必须 `wait()`**，否则残留进程连着 PG
    → 下次 `DROP DATABASE` 被拒 → 偶发红一条无关的灯。
38. **迁移铁律**：只增不改，每份只跑一次，**不写 `IF NOT EXISTS`**（sqlite 不认，
    照 009 先例写裸 `ADD COLUMN`）。新迁移必须在 ≥1 个单测的 schema 列表里被引用，
    否则 `test_every_migration_is_exercised_by_some_test` 红。
39. **sqlite 替身要做等价翻译，不是跳过**。先例：`interval` → `datetime(x,'+N units')`、
    `jsonb_typeof` → `json_type`。迁移里别写 `COMMENT ON`（PG 方言）。
40. PG 连接要带 `row_factory=dict_row`。

## 部署编排（M67~M75，已真跑在 Docker Desktop 2 节点）

```bash
python -m apps.migrate apply                    # 上线前跑（initContainer 已在跑）
python -m apps.probe live|ready                 # liveness / readiness
AGENTOS_MANIFEST=deploy/config/agentos.toml python -m apps.api
AGENTOS_AGENT_REGISTRY=deploy/config/agents.toml python -m apps.api
```

- **DSN 唯一来源** = `apps/_dsn.py: resolve_dsn()`（`--dsn` > 清单 > `AGENTOS_PG_DSN`）
- **两个 Kafka 进程 `replicas: 0`**（刻意不删），单独文件并写明 blocked-on-kafka 原因
- `deploy/k8s/` 镜像 tag 必须跟冻结版本走（14 处），`test_deploy_manifest.py` 盯着
- **镜像 tag 陷阱**：同 tag 重建 + `IfNotPresent` 会用旧镜像
- liveness `DEFAULT_LIVE_MAX_AGE=90`（曾设 30，与 sweeper 退避相等 → CrashLoop）
- 真部署暴露的 bug（M73/M74）：① HTTP 推进不落快照 ② `restore()` 不给
  `AgentRun` 投影 status。修法：`_persist_after_advance()` + `loop.restore()` 末尾 `sync()`

## 命令行与 SDK

```bash
python -m apps.cli health|start|status|step|drive|cancel|approvals|decide|trace
python -m apps.cli manifest check|env <file.toml>
python -m apps.eval run ds.json [--out base.json] [--against base.json]
```

```python
from packages.agent_sdk import AgentOSClient   # CLI 与评估平台共用它（B-7）
```

- CLI 地址：`--base` > `$AGENTOS_API_BASE` > `http://127.0.0.1:8011`；
  退出码 0/2/3/4；零第三方依赖、**刻意绕过系统代理**
- SDK `request()` 返回 `(status, parsed, raw)`，**status=0 表示连不上**
- ⚠️ SDK opener 是**模块级共享**的（反复建 SSL context 会拖垮 OpenSSL）
- 评估平台：**评行为轨迹不评答案**；⭐ **两次都失败 = unchanged，不算 regression**

## 配置层

- **部署清单**（TOML，`packages.agent_manifest`）：环境变量的**唯一声明源**
  （`to_env()` 翻译给既有 bootstrap）。未知键/缺必填/类型错一律**点名拒绝**。
  与环境变量模式**二选一不叠加**；配置错在启动前死，统一 `ConfigurationError`
- **agent 注册表**（`packages.agent_registry`）：**配了才校验**（未登记 → 404
  `AGENT_NOT_FOUND`）；**没配就维持透传**（刻意的向后兼容）。
  坑：`stack` 声明了没人读

## 端口与可选能力（Port 惯例）

- 可选的 Port 能力探测统一用 `getattr(port, "method", None)`。
  先例：`cancel_child` / `registry` / `attempts` / **`ProgressBearing`**
- **`ProgressBearing`**（M86）：`progress()`（我在哪，捕获时问）
  + `resume(p)`（接到这，恢复时问）**成对**。Optional Protocol 风格，
  `ProgressBearing(Protocol)` 不强制实现，只在 Loop 侧探测。
  `_try_resume` 里有**第二个动作**：调完 `resume()` 再自述一次、再比一次 ——
  否则"空 `resume()`"会变成后门。

## 版本库状态（2026-09-22）

- 目录 `C:\Users\19644\socialbook\agentos` 已 init + `.gitignore` + 首推成功
- 远端 `git@github.com:todochenxi/Asuka.git`（**private**，SSH 形式）
- **目录改名尚未做**：`mv` 报 WinError 32 —— 锁的持有者是**自己的工具链会话**
  （WorkBuddyAI / node / sandbox-cli / **bash** 的 CWD 都在该目录里），
  会话活着就改不了。正确做法：退出应用后**在会话之外** `ren agentos Asuka`
- ⚠️ **只改目录名，不要批量替换内容里的 `agentos`**：包名 `packages/agent_*`、
  模块 `apps.*`、文档标题都是内容，批量替换会打红 1319 条测试
- 坑：① `git init -b main` 对已初始化的 repo **不改分支** → 用 `git branch -M main`
  ② Windows `failed to execute prompt script` = GCM 弹不出窗（走 SSH / PAT）
- `deploy/k8s/02-secret.yaml` 含明文 demo 口令 `password: "agentos"`，
  文件头已写明上线前必须用 `kubectl create secret` 替换
