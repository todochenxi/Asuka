# AgentOS 项目长期笔记

## 定位与判据

企业级 Agent 平台。价值不在"多智能"，在**可靠 / 可观测 / 可审计**。
40+ 条已冻结不变量（B/R/S/D/X/A/E/I/L/C/PR 系列）都朝这个方向。
反复出现的判据：**宁可拒绝，不许编造**。

## 规模（v2.1.67，2026-09-22）

| 项 | 数量 |
|---|---|
| Python 源文件 | 255 个 |
| PG 迁移 | 17 份 |
| 单元测试 | **1,207 条全绿** |
| 集成测试（真 PG） | **201 条全绿** |

已完成：基础设施、领域、内核、运行时、控制面、Contracts、部署编排（M67~M75）。
**未实现**：智能层 M12（Decision Engine / Planner 只有 Protocol，由 DemoPlanner 顶着）、
认知运行 M13（依赖 M12）。部分：开发者平台 M11（SDK/CLI/Manifest 已做，缺 GitOps）。

## 里程碑流程

skill `agentos-milestone`：空洞 → 实现 → 测试 → **变红验证** → **冻结基线**。
缺后两步不算完。**接手时先查：代码里的 M 编号是不是大于文档里的 M 编号。**

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

- 目录 `C:\Users\19644\socialbook\agentos` 原本**不是 git 仓库**，已加 `.gitignore`
- 计划推到 `https://github.com/todochenxi/Asuka`（private），目录改名 `Asuka`
- ⚠️ **只改目录名，不要批量替换内容里的 `agentos`**：包名 `packages/agent_*`、
  模块 `apps.*`、文档标题都是内容，批量替换会打红 1200+ 测试
- `deploy/k8s/02-secret.yaml` 含明文 demo 口令 `password: "agentos"`，
  文件头已写明上线前必须用 `kubectl create secret` 替换
