# AgentOS 项目长期笔记

## 定位与判据

企业级 Agent 平台。价值不在"多智能"，在**可靠 / 可观测 / 可审计**。
40+ 条已冻结不变量（B/R/S/D/X/A/E/I/L/C/PR 系列）都朝这个方向。
反复出现的判据：**宁可拒绝，不许编造**。

## 规模（v2.1.72，2026-09-22）

| 项 | 数量 |
|---|---|
| Python 源文件 | 255 个 |
| PG 迁移 | 17 份 |
| 单元测试 | **1,287 条全绿** |
| 集成测试（真 PG） | **201 条全绿** |

已完成：基础设施、领域、内核、运行时、控制面、Contracts、部署编排（M67~M75）。
**未实现**：智能层 M12（Decision Engine / Planner 只有 Protocol，由 DemoPlanner 顶着）、
认知运行 M13（依赖 M12）。部分：开发者平台 M11（SDK/CLI/Manifest 已做，缺 GitOps）。

## 里程碑流程

skill `agentos-milestone`：空洞 → 实现 → 测试 → **变红验证** → **冻结基线**。
缺后两步不算完。**接手时三条检查**：
1. 代码里的 M 编号是不是大于文档里的 M 编号？
2. 有没有**已诊断但未固化**的结论？（翻已完成任务的 description，看"根因是 X"
   却**没有对应测试**的条目 —— M84 就是这么查到的）
3. 版本号三处落点是否同源？（文档名 / `app.py` / k8s 14 处 tag，见 PR-33）

## 活跃空洞

| # | 形状 | 处置 |
|---|---|---|
| 225 | 不存在的 Run 叫停返回 200 unknown | 架构限制（无 runs 表）。页面诚实显示"已落下意图" |
| 231 | 不校验 agent 存在性 | 已由注册表闭合（配了才校验） |
| 234 | `executions` 的取消归因无人读 | 登记不治：Run 级归因已在账本 payload |
| 236 | migrate/probe 无 CLI 子命令 | 登记不治：调用方是编排层不是人 |
| 237 | 部署清单指向 `examples.demo_stack` | **M12 落地时必须回来改这三行** |
| 238 | 预算耗尽不给 State 留痕 | 登记不治；Intelligence 要读"我为什么停了"时必须补 |

## 已冻结的关键不变量（近几轮）

| 不变量 | 内容 |
|---|---|
| **I-10** | 重规划**必须吃预算**（`steps += 1`）。否则一直 REPLAN 的 Run 永远跑下去 |
| **I-11** | 完成前必须没有"**自上次规划以来**"未处理的失败，有则 REPLAN。判据钉在 **Loop 的 FINISH 分支**（不钉引擎） |
| **I-12** | 重规划必须产出**形状不同**的计划（比 `(node_id,name,kind)+constraints`，**不比 plan_id**）。换不出来 → FAILED |
| **B-12** | 终态声明**必须带原因**，落 `run.finished` payload。`reason` 是必填关键字参数 |
| **I-13** | 委派失败就是一次执行失败，**必须进 State**（`execution.failed`，绑真实 Execution）。否则父 Run 可以带着委派失败宣布 completed |
| **I-14** | 查不出来的失败也算。委派等不到回音 → `execution_unresolved` 进 State（**不许冒充 `failed`** —— PR-19）。I-11 判据 = "**这一步没有被证明成功**" |
| **I-15** | 三个终态在父侧**信息量**不同必须分得开：`failed`（它失败了）／`cancelled`（**为什么**被取消，谁+理由）／`unknown`（**什么都不知道**）。取消必须在父侧留下可读原因，且**不许记成"不知道"** |
| **D-37** | 子 Run 交给父 Run 的失败原因，必须是它**真正的死因**（不是最后一句 observation）。死因缺失时说"没记录到"，**不许拿 summary 顶替** —— 那是用过程冒充结论，而它可能是反的 |
| **PR-33** | 版本号的三处落点（文档名 / `apps/api/app.py` / `deploy/k8s` 14 处 tag）必须**同源**，不允许"只有部分落点更新"的中间状态。中间状态产出的红灯**指向错的地方**（PR-19 反过来用）。由两条不依赖 PG 的单测守着 |
| **B-2** | `AgentRun.status` 是派生值，只能经 `sync()` 写，不能赋值 |
| **B-7** | 一个事实一处定义（清单 vs 环境变量、HTTP 封装只许一份） |
| **B-8** | 取消/审批归因必填（`reason` + `by`） |
| **PR-14/15** | 客户端必须**惰性** import；`packages/` `apps/` 零第三方依赖 |
| **PR-8** | alive ≠ ready，liveness 与 readiness 分开 |

## ⭐ 反复踩到的坑（按价值排序）

1. **"内存里分得清" ≠ "账本上分得清"**（M79）。
   查"为什么"类问题先确认字段活在哪层：内存字段 / 快照 / 账本，是三件事。
2. **交付的判据是"被用上"，不是"被写出来"**。同一坑栽过五次
   （没人用它启动 / 没人读 stack / 页面没调用 / 实现了没冻结）。
   验收方式 = **改坏它看谁红**，不是"它能跑"。
3. **一个 Store 两种实现（内存 + PG）时，保证必须两边同源**。
   只在一种实现里成立的保证 = 在生产上不成立，而内存单测全绿会让你以为成立。
4. **保证住在哪一层，就在哪一层验证**。PG 物理约束必须写 `*_real_pg.py`。
5. **盘点用"有没有目录/文件"代替"能力是否落地"** —— 栽过三次（M4/M7/M14 全判错）。
   正确问法：**这条里程碑描述的那件事，代码里做得到吗？**
6. **并发判胜负唯一可靠写法 = 带条件的 UPDATE + rowcount**，不是"先读再写"。
   五条保证同一种写法：S-4 / S-15 / A-11（`AND status='pending'`）、E-25（`AND version=`）、
   PR-3（`AND (claimed_until IS NULL OR <=%s)`）。
7. **空洞登记会过期**，动手前先实证。
8. **变红脚本优先用单行替换**：heredoc 多行替换会把 `\n` 转成 `/n` → 语法错 → 计数 0。
   看到"红了 0 条"先怀疑脚本。
9. **起子进程/开连接的测试，`terminate()` 后必须 `wait()`**，否则残留进程连着 PG
   → 下次 `DROP DATABASE` 被拒 → 偶发红一条无关的灯。
10. **改版本号必须在跑测试之前做完**：集成测试起真 uvicorn 读 `app.py`，
    而基线版本从**文档文件名**读，测试期间改会两边不同步。
11. **`real_pg()` 默认 `fresh=True` 会重建库**！附加连接必须 `fresh=False`，
    否则竞态测试假绿（通过的原因是库空了）。
12. **断言 API 错误必须比 `.code`，不能比 `str(e)`** —— 错误码在 `.code` 属性上，不在消息里。
13. **改动页面必须跑集成测试**：页面丢了全站 `<h1>` 会让 `test_api_real_http.py` 红。
14. 重启服务要确认日志里有 `Uvicorn running` 再验证（端口被占会测到旧进程）。
15. 偶发红排查：**完整捕获输出写文件再 grep FAIL**，别 `tail -3`，别靠猜。
16. sqlite 替身默认只吃 001；迁移里别写 `COMMENT ON`（PG 方言，sqlite 语法错）。
17. PG 连接要带 `row_factory=dict_row`，否则适配器取不到列名。
18. ⚠️ **两个 python 不是同一个**：系统 python 没装 `psycopg` / `httpx`。
    跑集成会得到 `FAILED (errors=1, skipped=188)` —— 188 条**全被跳过**，
    看着像"只红 1 条"，其实**一条都没真跑**。集成必须用
    `C:/Users/19644/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`。
19. **变红脚本要断言"替换命中次数 == 1"** —— 锚点不唯一时静默 SKIP，
    看起来像"这条变异不用验"。曾栽：`"reason": reason,` 在 loop.py 里出现 4 次。
20. ⭐ **加了字段 ≠ 送达**。B-12 把死因写进子 Run 账本，而父 Run 读的是另一条路
    （`mark_finished` 的 result → 事件 → `_reason`）。中间不接力，死因就停在半路，
    而系统照样"有原因"。**判断信息到没到：沿它实际走的路读到终点那一份。**
21. ⭐⭐ **判据读什么，就决定了它能看见什么**（M81）。I-11 判据写"数
    `execution.failed` observation" —— 这句话本身没错，但它把"什么算失败"
    **外包给了写 observation 的人**。委派那扇门没写 → 那条失败从判据视野里
    消失，而判据自己毫不知情（从 M77 到 M80 四轮没人发现）。
    ⇒ 补完一条不变量后，除了问"还能被绕过吗"，还要问：
    **它依赖的那些事实，是不是每一扇门都在写？**
    查法：找出所有能产生该事实的路径，看每条有没有写。
22. ⭐ **断言"最终不是 X"常常是错的断言**（M81 栽过）。I-11 按"上次规划"划界，
    换了形状不同的计划之后完成是**设计允许的**（失败可以被救回来）。
    于是"委派失败 → 不许完成"真正的可观测后果是
    **不能从失败直接走到 FINISH，中间必须隔着一次 REPLAN**。
    写这类断言前先问：**这条不变量禁止的是"最终状态"还是"直达路径"？**
23. ⭐ **"存在"不等于"被处理"**（M82 栽过，与 M75 同族）。`reducer.py` 末尾有
    兜底分支 `state.variables[f"obs:{id}"] = obs.summary` —— 任何 observation
    都会留在 State 上。所以断言 `assertIn(kind, kinds)` **守不住 reducer 的分支**，
    变异不红。要断言"它把这一步当结束了"：`execution_id in state.completed_tasks`。
    ⇒ **变异没红先怀疑测试**：问"我这条断言真能区分改坏前/后吗"。
24. ⭐⭐ **两个现成选项都不对时，缺的是第三个**（M82）。委派等不到回音那条
    Execution：写 `failed` 违反 PR-19，不写就是让父 Run 谎报完成。
    ⇒ 新增 `execution_unresolved`。**"不确定"是一种必须被记录的状态，
    不是"没有状态"。**
25. ⭐ **判断不能当结论用**（M83）。"父 Run 自己知道"这种话听起来不证自明，
    但它是一个**关于系统状态的断言** —— 而系统状态是可测的。
    写成判断留在文档里，下一轮会被当**前提**使用；
    写成有变异守着的测试，它才是一份能承重的结论。
26. ⭐ **不要为不存在的能力写区分**（M83，方向相反但同样要紧）。
    探针发现 A/B（父侧主动取消 / 被动得知）无法区分时，第一反应可能是
    "补个字段分开" —— 但那会造出一个**谁也填不了的维度**。
    **账本上每一列都该对应一条真实路径。** 正确做法：验清楚
    "能走到这里的只有哪一条"，然后把结论和"另一条是未实现的能力"一起登记。
27. ⭐ **"零代码改动"的一轮是正当的里程碑**（M83）。13 条测试全绿 + 4 条变异
    全红、`packages/` 一行没改 ⇒ 既有行为本来就对，缺的只是"有东西守着它"。
    把"读过代码觉得对"变成"有变异守着的结论"，本身就值得单独一轮。
28. ⭐ **诊断也是有归宿的：「写进任务卡」≠「写进基线」**（M84）。
    一条正确的结论如果没落进"下一个人一定会读到的地方"，就等于没做过。
    任务卡是**过程**，基线是**结论**；把结论留在过程里，
    下一个接手的人得到的不是"已知问题"而是"疑似新问题" ——
    他会再花一遍同样的时间得到同样的结论。**这比红灯本身贵得多。**
29. ⭐ **"偶发红"先假定它是真的，但也先翻账本**（M84）。
    按纪律撞了 13 次（9 串行 + 4 **并发双跑**）全绿 → 换方向翻任务账本，
    发现 M66 早已诊断过同一现象。⇒ **撞不出来 ≠ 问题不存在，
    可能只是触发条件不在"跑测试"这个动作里**（这里是个多步手动动作的时间窗）。
30. ⭐ **声明点在哪一层，就在哪一层读它**（M84 自己写测试时踩的）。
    文档的版本声明在**文件名**上，我却对它 `read_text()` → 永远匹到 0 次。
    **把"存在"当成了"被处理"** —— 与第 23 条同一种错换了个位置。
31. ⚠️ **一致性检查只认"声明点"，不扫全文**（M84）。基线文档正文有整节
    「版本变更」列着历代版本 → 扫全文会把**历史记录**判成**漂移**，
    造出一条永远红的噪声，**比没有更坏**。
32. ⚠️ 本项目用 **`unittest`**（不是 pytest）。集成命令：
    `PYTHONPATH=. <venv-python> -m unittest discover -s tests/integration -t .`

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
- **镜像 tag 陷阱**：同 tag 重建 + `IfNotPresent` 会用旧镜像 → 用带构建号的 tag
- liveness `DEFAULT_LIVE_MAX_AGE=90`（曾设 30，与 sweeper 退避相等 → CrashLoop）
- 真部署暴露的状态丢失 bug（M73/M74）：① HTTP 推进不落快照 ② `restore()` 不给
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
- ⚠️ SDK opener 是**模块级共享**的，别改成每次新建（反复建 SSL context 会拖垮 OpenSSL）
- 评估平台：**评行为轨迹不评答案**；⭐ **两次都失败 = unchanged，不算 regression**

## 部署清单（TOML，`packages.agent_manifest`）

- 清单是环境变量的**唯一声明源**（`to_env()` 翻译给既有 bootstrap），不是第二套配置
- 未知键 / 缺必填 / 类型错一律**点名拒绝**，不静默回退默认
- 与环境变量模式**二选一不叠加**；配置错在启动前死，统一 `ConfigurationError`

## agent 注册表（`packages/agent_registry`）

- **配了才校验**：未登记 → 404 `AGENT_NOT_FOUND`
- **没配就维持透传**（刻意的向后兼容）
- 坑：`stack` 声明了没人读 → 属于"交付判据"第 2 条

## 版本库状态（2026-09-22）

- 目录 `C:\Users\19644\socialbook\agentos` 原本不是 git 仓库 → 已 init +
  加 `.gitignore` + 首推成功（commit `6403400`）
- 远端 `git@github.com:todochenxi/Asuka.git`（**private**，SSH 形式）
- 用户想推到 private 并改名 `Asuka`，**目录改名尚未做**
- 踩到的坑：① `git init -b main` 对已初始化的 repo **不改分支** → 停在 master
  → `src refspec main does not match any`，用 `git branch -M main` 修
  ② Windows `failed to execute prompt script` = GCM 弹不出窗，绕过去（SSH / PAT 塞 URL）
- ⚠️ **只改目录名，不要批量替换内容里的 `agentos`**：包名 `packages/agent_*`、
  模块 `apps.*`、文档标题都是内容，批量替换会打红 1200+ 测试
- `deploy/k8s/02-secret.yaml` 含明文 demo 口令 `password: "agentos"`，
  文件头已写明上线前必须用 `kubectl create secret` 替换
