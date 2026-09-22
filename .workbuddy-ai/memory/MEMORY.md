# AgentOS 项目长期笔记

## 定位与判据

企业级 Agent 平台。价值不在"多智能"，在**可靠 / 可观测 / 可审计**。
50+ 条已冻结不变量（B/R/S/D/X/A/E/I/L/C/PR/O 系列）都朝这个方向。
反复出现的判据：**宁可拒绝，不许编造**。

> 详细内容在基线文档（`AgentOS_企业级Agent平台架构_v2.1.75_最终冻结版.md`）
> 与当日日志（`.workbuddy-ai/memory/YYYY-MM-DD.md`）里。本文件只做索引。

## 规模（v2.1.75，2026-09-22）

| 项 | 数量 |
|---|---|
| Python 源文件 | 258 个 |
| PG 迁移 | 18 份 |
| 单元测试 | **1,334 条全绿** |
| 集成测试（真 PG） | **209 条全绿** |

已完成：基础设施、领域、内核、运行时、控制面、Contracts、部署编排（M67~M75）、
可恢复性收口（M85/M86）、计划消费语义（M87）。
**未实现**：智能层 M12（Decision Engine / Planner 只有 Protocol，由 DemoPlanner 顶着）、
认知运行 M13（依赖 M12）。部分：M11（SDK/CLI/Manifest 已做，缺 GitOps）。

## 里程碑流程

skill `agentos-milestone`：空洞 → 实现 → 测试 → **变红验证** → **冻结基线**。
缺后两步不算完。**接手时四条检查**：
1. 代码里的 M 编号是否大于文档里的？（编号对齐）
2. 有没有**已诊断但未固化**的结论？（翻任务账本，看"根因是 X"却没有对应测试的条目）
3. 版本号三处落点是否同源？（文档名 / `app.py` / k8s 14 处 tag，见 PR-33）
4. 冻结动作与测试运行**不许重叠**（M84/M85 各栽一次）

## 活跃空洞

| # | 形状 | 处置 |
|---|---|---|
| 225 | 不存在的 Run 叫停返回 200 unknown | 架构限制（无 runs 表）。页面诚实显示"已落下意图" |
| 234 | `executions` 的取消归因无人读 | 登记不治：Run 级归因已在账本 payload |
| 236 | migrate/probe 无 CLI 子命令 | 登记不治：调用方是编排层不是人 |
| 237 | 部署清单指向 `examples.demo_stack` | **M12 落地时必须回来改这三行** |
| 238 | 预算耗尽不给 State 留痕 | 登记不治；Intelligence 要读"我为什么停了"时必须补 |
| 239 | 子 Run `result` 是自由 JSON，`reason` 无 schema | 登记不治：`_reason` 侧已有诚实降级 |
| 240 | 集成版本断言做了跨时代比较 → 改文件与跑测试重叠就必红 | 登记不治；纪律：冻结与测试串行 |
| 243 | `_ensure_step()` 的 ad-hoc 步序号用 `len(steps_of_run)` → 编号会跳过 | 登记不治：名字只是标签 |

（231 已闭合；241/242 本轮闭合。）

## 已冻结的关键不变量（近几轮）

| 不变量 | 内容 |
|---|---|
| **I-6** | 判据住 `Observation.__post_init__`，**双向**：来源 `EXECUTION_RESULT` ⟹ 必绑非空 `execution_id` + `attempt_no>=1`；非执行来源**不许**绑 |
| **I-10** | 重规划**必须吃预算**（`steps += 1`），否则一直 REPLAN 的 Run 永远跑下去 |
| **I-11** | 完成前必须没有"自上次规划以来"未处理的失败，有则 REPLAN。判据钉在 **Loop 的 FINISH 分支** |
| **I-12** | 重规划必须产出**形状不同**的计划（比 `(node_id,name,kind)+constraints`，**不比 plan_id**） |
| **I-13** | 委派失败必须进 State（`execution.failed`，绑真实 Execution） |
| **I-14** | 查不出来的失败也算 → `execution_unresolved` 进 State（**不许冒充 `failed`**）。I-11 判据 = "这一步没有被证明成功" |
| **I-15** | 三个终态父侧信息量必须分得开：`failed`／`cancelled`（谁+理由）／`unknown` |
| **I-16** | Plan 依赖图是**可执行约束**：进 Step 的依据是"**它准备好了**"（依赖都完成 + 没做过），不是"下标轮到它了"。游标属于**这份计划**。**卡住**（有节点没做但没一个就绪）→ **不编造 ad-hoc**，走 REPLAN；与"**用完**"（常态，允许 ad-hoc）分得开。只认 `COMPLETED` 算满足 |
| **I-17** | `run()` 停止条件 = "**这条 Run** 到了终态"，**不是**"这一步的结果属于某张白名单"。`FAILED` 作为"这一步的结果"不该进白名单（会弄坏 I-11），作为"Run 终态"必须让它停 |
| **R-7** | 有状态注入实现必须 `progress()` 自述 + 能 `resume(p)` 接上；**先接上、接不上才点名拒绝**。不许静默重来。`progress()` 返回 `None` = 实现有 bug，当场拒 |
| **B-2** | `AgentRun.status` 是派生值，只能经 `sync()` 写 |
| **B-7** | 一个事实一处定义。Intelligence 的知识不许泄漏进 Runtime |
| **B-8** | 取消/审批归因必填（`reason` + `by`） |
| **B-12** | 终态声明**必须带原因**，落 `run.finished` payload。`reason` 是必填关键字参数 |
| **D-37** | 子 Run 交给父 Run 的必须是它**真正的死因**。缺失时说"没记录到"，**不许用 summary 顶替** |
| **PR-14/15** | 客户端**惰性** import；`packages/` `apps/` 零第三方依赖 |
| **PR-24** | 对外报的版本必须等于冻结基线 |
| **PR-33** | 版本号三处落点必须**同源**，不允许中间状态 |

## ⭐ 反复踩到的坑

### A. 判据住在哪、覆盖谁

1. ⭐⭐ **承诺句不是机制**（M85）。`@dataclass(frozen=True)` 的 `__init__` **是公开的**
   —— frozen 只保证"不可变"，不保证"构造受控"。
   ⇒ 判据要住**所有构造路径的汇合处**（`__post_init__`）；补的时候问"除工厂外还有几条路"。
2. ⭐⭐ **一句"这是全部"漏了不在自己手里的那一半，就不是保证是错觉**（M86）。
   `RunSnapshot` 自称装"可恢复点的全部数据"，却只管住 Runtime 自己的内存。
   ⇒ 先问：**这个"全部"的边界，是谁划的？**
3. ⭐⭐ **判据读什么，就决定了它能看见什么**（M81）。I-11 数 `execution.failed` 把
   "什么算失败"外包给了写 observation 的人。⇒ 还要问：**它依赖的事实，每扇门都在写吗？**
4. ⭐⭐ **判据要挡的是"坏的那条路"，不是"那条路"**（M86）。新判据打红既有测试时，
   先问打红的这条是不是在描述**正常行为**。R-7 第一版只拒绝不恢复 → 打红了真生产流程。
5. ⭐⭐ **判据读的是「这件事本身」，还是「它在某个序列里的位置」**（M87）。
   读**位置**（下标/序号/枚举成员）的判据，会在"位置"与"意义"不重合处**静默**失效：
   - `plan.nodes[len(steps_of_run)]` → 该读"依赖完成了没有"。① 计划说"n2 等 n1"
     而 n2 排前面 → 先跑 n2；② 重规划换了新计划、游标还停在"已跑步数" →
     **新计划前 N 个节点被跳过**。
   - `outcome in {FINISHED,…,CANCELLED}` → 该读"这条 Run 还在不在"。白名单没有
     `FAILED` → Run 已终态时 `run()` **永远转下去**。
   ⇒ 问：**这个量是「问题本身」还是「问题的编号」？** 配套：**同一个量身兼两职最危险**；
   **派生量优先现算不另存**（`_consumed_plan_nodes()` 从 `steps_of_run` 现算 → 恢复后自动对）。
6. ⭐ **"存在"不等于"被处理"**（M82）。`reducer.py` 末尾有兜底分支。
7. ⭐ **不要为不存在的能力写区分**（M83）。**账本上每一列都该对应一条真实路径。**

### B. 断言与变异

8. ⭐⭐ **断言隔离**（M85 栽两次）：除"被测那一项"外其他字段一律给足，
   且拒绝理由**点名**被测字段（`assertIn("execution_id", str(exc))`）。
   否则验证的是"某处会拦"而不是"这一处会拦" → 变异假绿。
9. ⚠️ **注释不是变异**（M86）。加 `# noqa: M1` 注释字段还在，什么都没变。
10. **变红脚本优先单行替换**；**锚点命中次数必须 == 1**（否则静默 SKIP）；
    **每条变异后立刻还原**（否则计数被前一条污染）。
11. **"红了 0 条"或"基线就不绿"先怀疑脚本，不要先查代码。**
    曾栽：`ROOT = Path(__file__).parent.parent`（脚本在仓库根，该是 `.parent`）。
12. ⭐ **判断不能当结论用**（M83）。"父 Run 自己知道"是关于系统状态的断言，而状态可测。
13. ⭐ **"零代码改动"的一轮是正当的里程碑**（M83）。
14. ⭐ **补完判据跑既有测试一条不红 = 此前没被守过**（M85 1287 / M87 1319，同因：
    既有用例恰好只走"判据成立"那一半的路）。⇒ **专门写一条"让两种取法给出不同答案"的用例。**

### C. 信息与状态

15. **"内存里分得清" ≠ "账本上分得清"**（M79）。内存字段 / 快照 / 账本是三件事。
16. ⭐ **加了字段 ≠ 送达**。沿它实际走的路读到终点那一份。
17. ⭐⭐ **`None` 有两义性**（M86）："我没有这个能力"与"我有这个能力但说不出值"混为一谈时，
    第二种会**静默退化成第一种**。⇒ 先 `getattr` 判"有没有"，再判返回值"说没说出值"。
18. ⭐⭐ **两个现成选项都不对时，缺的是第三个**（M82）。**"不确定"是必须被记录的状态。**
19. ⭐ **一个 Store 两种实现（内存 + PG）时，保证必须两边同源。**
20. **保证住在哪一层，就在哪一层验证**。PG 物理约束必须写 `*_real_pg.py`。
21. **并发判胜负唯一可靠写法 = 带条件的 UPDATE + rowcount**，不是"先读再写"。

### D. 流程与纪律

22. ⭐ **诊断的归宿是「基线」，不是「任务卡」**（M84）。任务卡是过程，基线是结论。
23. ⭐ **"偶发红"先假定它是真的，但也先翻账本**（M84）。**撞不出来 ≠ 问题不存在。**
24. **交付的判据是"被用上"，不是"被写出来"**。栽过五次。验收 = **改坏它看谁红**。
25. ⚠️ **盘点用"有没有目录/文件"代替"能力是否落地"** —— 栽三次（M4/M7/M14 全判错）。
    正确问法：**这条里程碑描述的那件事，代码里做得到吗？**
26. **空洞登记会过期**，动手前先实证。

### E. 环境与工具链

27. ⚠️ **改版本号必须在跑测试之前做完，且不许与跑测试并发**（M84 + M85）。
    集成起真 uvicorn 读 `app.py`（进程启动时的快照），基线版本执行断言时**现读文档文件名**
    → 两边是**两个时代**的值。⇒ 红灯不一定是被测代码红，也可能是你在它背后改掉了它测的世界。
28. ⭐ **声明点在哪一层，就在哪一层读它**（M84）。版本声明在**文件名**上。
29. ⚠️ **一致性检查只认"声明点"，不扫全文**（M84）。扫全文会把历史记录判成漂移。
30. ⚠️ **两个 python 不是同一个**。单测用 `.../python/versions/3.13.12/python.exe`；
    集成必须用 `.../python/envs/default/Scripts/python.exe`（系统 python 没装 psycopg/httpx）。
    本项目用 **`unittest`**（不是 pytest）：`PYTHONPATH=. <py> -m unittest discover -s tests/<dir> -t .`
31. **`real_pg()` 默认 `fresh=True` 会重建库**！附加连接必须 `fresh=False`。
32. **断言 API 错误必须比 `.code`，不能比 `str(e)`**。
33. **改动页面必须跑集成测试**（丢了全站 `<h1>` 会让 `test_api_real_http.py` 红）。
34. 重启服务要确认日志里有 `Uvicorn running` 再验证（端口被占会测到旧进程）。
35. 偶发红排查：**完整捕获输出写文件再 grep FAIL**，别 `tail -3`。
36. **起子进程/开连接的测试，`terminate()` 后必须 `wait()`**，否则残留进程连着 PG
    → 下次 `DROP DATABASE` 被拒 → 偶发红一条无关的灯。
37. **迁移铁律**：只增不改，每份只跑一次，**不写 `IF NOT EXISTS`**（sqlite 不认，
    照 009 先例写裸 `ADD COLUMN`）。新迁移必须在 ≥1 个单测的 schema 列表里被引用。
38. **sqlite 替身要做等价翻译，不是跳过**（先例：`interval` → `datetime(x,'+N units')`、
    `jsonb_typeof` → `json_type`）。迁移里别写 `COMMENT ON`。
39. PG 连接要带 `row_factory=dict_row`。
40. ⚠️⚠️ **变红脚本会真的改源文件 —— 三条纪律**（M87 栽得最惨的一次）。
    第一版把还原写在 `try/finally` 里，跑到 M4 时被**中途 kill** → `finally` 没执行
    → **变异留在 `loop.py` 里**。连锁：跑全套 89 errors（像"我的改动弄红了测试"）→
    备份是**污染之后**拷的（装着变异）→ `git checkout HEAD` 验证"HEAD 绿"（判断对、结论错）
    → 从备份恢复把变异装回去。
    ⇒ **原文件先落盘一份** + 子进程加 `timeout=`；**备份要在确认干净之后才拷**；
    **看到"既有测试红了"先 grep 变异串**（挑独一无二的串才好 grep）。
41. **Python 把 stdout 重定向到文件是带缓冲的**（日志空着 ≠ 没在跑）→ 用 `python -u`。
42. ⭐ **"超时"的变异可能指向另一个洞。** M87 的 M4 第一次超时 120s，I-17 修完后
    同一条变异不再超时、而是**红 301 条** —— 那次超时是在说"你刚撞见了第二个真 bug"。
43. **`probe<N>.py` / `red<N>.py` 要提交进仓库** —— 基线文档点名引用它们，
    文件不在那句话就没法复验。

## 部署编排（M67~M75，已真跑在 Docker Desktop 2 节点）

```bash
python -m apps.migrate apply                    # 上线前跑（initContainer 已在跑）
python -m apps.probe live|ready                 # liveness / readiness
AGENTOS_MANIFEST=deploy/config/agentos.toml python -m apps.api
AGENTOS_AGENT_REGISTRY=deploy/config/agents.toml python -m apps.api
```

- **DSN 唯一来源** = `apps/_dsn.py: resolve_dsn()`（`--dsn` > 清单 > `AGENTOS_PG_DSN`）
- **两个 Kafka 进程 `replicas: 0`**（刻意不删），单独文件写明 blocked-on-kafka 原因
- `deploy/k8s/` 镜像 tag 必须跟冻结版本走（14 处），`test_deploy_manifest.py` 盯着
- **镜像 tag 陷阱**：同 tag 重建 + `IfNotPresent` 会用旧镜像
- liveness `DEFAULT_LIVE_MAX_AGE=90`（曾设 30，与 sweeper 退避相等 → CrashLoop）
- 真部署暴露的 bug（M73/M74）：① HTTP 推进不落快照 ② `restore()` 不给 `AgentRun` 投影 status

## 命令行与 SDK

```bash
python -m apps.cli health|start|status|step|drive|cancel|approvals|decide|trace
python -m apps.cli manifest check|env <file.toml>
python -m apps.eval run ds.json [--out base.json] [--against base.json]
```

- CLI 地址：`--base` > `$AGENTOS_API_BASE` > `http://127.0.0.1:8011`；
  退出码 0/2/3/4；零第三方依赖、**刻意绕过系统代理**
- SDK `request()` 返回 `(status, parsed, raw)`，**status=0 表示连不上**；opener 是模块级共享的
- 评估平台：**评行为轨迹不评答案**；⭐ **两次都失败 = unchanged，不算 regression**

## 配置层

- **部署清单**（TOML，`packages.agent_manifest`）：环境变量的**唯一声明源**
  （`to_env()` 翻译给既有 bootstrap）。未知键/缺必填/类型错一律**点名拒绝**。
  与环境变量模式**二选一不叠加**；配置错在启动前死，统一 `ConfigurationError`
- **agent 注册表**（`packages.agent_registry`）：**配了才校验**；**没配就维持透传**

## 端口与可选能力（Port 惯例）

- 可选 Port 能力探测统一 `getattr(port, "method", None)`。
  先例：`cancel_child` / `registry` / `attempts` / **`ProgressBearing`**
- **`ProgressBearing`**（M86）：`progress()` + `resume(p)` **成对**，Optional Protocol 风格。
  `_try_resume` 里必须有**第二个动作**（调完 `resume()` 再自述一次、再比一次），
  否则"空 `resume()`"就是后门。

## 版本库状态（2026-09-22）

- 目录 `C:\Users\19644\socialbook\agentos` 已 init + `.gitignore` + 首推成功
- 远端 `git@github.com:todochenxi/Asuka.git`（**private**，SSH 形式）
- **目录改名尚未做**：`mv` 报 WinError 32 —— 锁的持有者是**自己的工具链会话**，
  会话活着就改不了。正确做法：退出应用后**在会话之外** `ren agentos Asuka`
- ⚠️ **只改目录名，不要批量替换内容里的 `agentos`**（包名/模块/文档标题都是内容）
- 坑：① `git init -b main` 对已初始化的 repo **不改分支** → 用 `git branch -M main`
  ② Windows `failed to execute prompt script` = GCM 弹不出窗（走 SSH / PAT）
- `deploy/k8s/02-secret.yaml` 含明文 demo 口令，文件头已写明上线前必须替换
