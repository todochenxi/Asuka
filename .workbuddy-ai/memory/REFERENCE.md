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
44. ⚠️⚠️ **变红脚本会真的改源文件 —— 五条纪律**（M87 / Asuka 第六轮各栽一次）。
    第一版还原写在 `try/finally`，跑到 M4 被**中途 kill** → `finally` 没跑 → **变异留在 `loop.py` 里**。
    连锁：全套 89 errors（像"我的改动弄红了测试"）→ 备份是**污染之后**拷的 →
    `git checkout HEAD` 验"HEAD 绿"（判断对、结论错）→ 从备份恢复把变异装回去。
    ⇒ ① **原文件先落盘** + 子进程加 `timeout=`；② **备份要在确认干净之后才拷**；
    ③ **看到"既有测试红了"先 grep 变异串**（串要挑独一无二的）；
    ④ **`finally` 挡不住信号** —— SIGTERM/SIGINT 时 Python 不跑 `finally`。
    还要装 `signal.signal(SIGTERM|SIGINT, ...)` 处理。Asuka 第六轮实锤：
    前台跑 `red91.py` 被 Bash 的 120s 超时杀掉，`answers.py` 里 `grounded_rate`
    的漂移检查被换成"和自己比"，**下一次跑的基线因此报红，白查十分钟**。
    ⇒ **变红脚本一律后台跑**（一轮 = 变异数 × 全套件，20+ 条就是四五分钟）。
    ⑤ **启动时比对备份与源码，不一致就拒绝启动**（Asuka 的 `redkit.py` 退出码 3）。
    **不自动还原** —— "备份 ≠ 源码"有两种原因（上次被杀 / 有人改过源码），
    两者在文件上长得一模一样，自动挑一种就是**替操作者猜**，猜错就静默改掉别人的代码。
    处置：确认源码是你想要的那份之后删掉备份目录再跑。
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

## Asuka：沙箱 / 代理 / 模型下载（2026-09-22 实测）

三条都会**静默失败**，各浪费过一次半小时以上，记下来：

1. ⚠️⚠️ **沙箱只允许写项目目录内，失败是静默的。**
   `curl -sL -o /tmp/x.json <url>` 报告 `http=200 size_download=687`、退出码 0，
   而 `/tmp/x.json` **根本不存在**。写项目目录内正常。
   ⇒ **任何下载都落到项目目录内**，下完用 `stat` 核对字节数，**不信 curl 的退出码**。
   （`huggingface_hub` 写出 0 字节 `config.json` 并报"成功"就是这么来的。）
   ⚠️ **`/dev/null` 也写不了**：`curl -o /dev/null` 返回 `size=0 speed=0` + 退出码 23，
   **看起来像"服务器不给数据"** ⇒ 测速必须写到项目目录里的临时文件，否则结论全错。

2. ⚠️ **环境有 HTTP 代理 `http_proxy=http://127.0.0.1:64216`；大响应会卡死不报错。**
   小响应正常；PyPI 的 `torch` 索引页（~10MB HTML）与 2GB wheel 会**静默挂住**
   （pip 缓存 25 秒零增长，既不超时也不失败）。
   ⇒ 大文件必须**断点续传 + 循环重试**，单次 curl 一定被掐断
   （`schannel: server closed abruptly`，实测 34MB 处断）。
   排查手段：`du -sm <缓存目录>` **采样两次**看有没有增长 —— 比等 pip 报错快得多。

   **带宽是全局限速，不是每连接限速**（2026-09-22 实测，同一 URL 同一时刻）：

   | 方式 | 聚合速度 |
   |---|---|
   | 单连接 | 548 KB/s |
   | 8 段 `curl -r` 并行 | 492 KB/s |
   | `--noproxy '*'` 直连 | 68 KB/s（更差，代理是帮手不是瓶颈） |

   ⇒ **分片不提速**，只降低"断一段损失一段"的风险。别指望 8 倍。
   2.1GB 大约 1 小时，按这个预算安排。

3. **源的选择**：
   * pip → **默认源** `https://pypi.org/simple`（用户 2026-09-22 开代理后指定）。
     曾配清华源，实测**在本环境不可靠**：`sentence-transformers` 返回
     `from versions: none`、`sentencepiece` 报 `ReadTimeoutError (read timeout=15)`；
     而同一时刻 curl 直拉清华索引页是 **200 / 248KB / 3s** ⇒
     **源没问题，是代理在 ~250KB 的响应上卡住**（见第 2 条）。
     结论：装不上先怀疑**代理**，再怀疑源，最后才怀疑包名。
   * 模型 → **ModelScope**（`https://modelscope.cn/models/<org>/<name>/resolve/master/<file>`）。
     HuggingFace 不通；`hf-mirror.com` 小文件可以、**大文件必断**。
     ModelScope 的 `bge-m3` 与 HF 的 `pytorch_model.bin` **字节数完全一致**（2271145830）。
     ⚠️ ModelScope **支持 `Range`**（实测 `curl -r 100-1099` → `206` + 1000 字节），
     所以分片下载可行；但必须**只认 206**，200 表示 Range 被忽略，按偏移追加会写坏文件。
   * `HF_HUB_DISABLE_XET=1` 能消掉 Xet 路径的报错（但 HF 本身不通，治不了根）。

4. 依赖分工（沿用既有约定）：
   * 单测 `.../python/versions/3.13.12/python.exe` —— **保持零依赖**
   * 建索引/跑模型 `.../python/envs/default/Scripts/python.exe`
     （实测 `torch 2.14.0+cpu` / `sentence-transformers 6.0.1` / `transformers 5.17.0`
     / `sentencepiece 0.2.2` / `qdrant-client 1.19.1`）
   * ⚠️ **`sentencepiece` 是必装的** —— bge-m3 是 XLM-RoBERTa 系，
     缺了 `transformers` 可能加载不了 tokenizer

5. ⚠️⚠️ **`--embedder local` 不设 `ASUKA_EMBED_MODEL_PATH` ⇒ 静默退化到**损坏的** HF 缓存**
   （2026-09-23 真踩）。`embedding.py` 里 `model_path or self.model`：环境变量为空 ⇒
   退化成模型名 `BAAI/bge-m3` ⇒ 走 HF 缓存，而那份
   `~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/<hash>/config.json`
   正是上面第 1 条那个 **0 字节**文件 ⇒
   `OSError: config file ... is not a valid JSON file`。
   ⚠️ **报错里一个字都不提环境变量** —— 看起来像"模型坏了/要重下 2.2GB"，
   而本地权重 `.asuka-models/bge-m3/` 一直是**完好的**（config.json 687B + 2.2GB bin）。
   ⇒ 跑 dense 前**必须**显式给：
   `ASUKA_EMBED_MODEL_PATH='C:/Users/19644/socialbook/agentos/.asuka-models/bge-m3'`
   （用绝对路径）。判别法：日志出现 `Loading weights: 391/391` 就是走了本地权重；
   出现 `sending unauthenticated requests to the HF Hub` 就是在走坏缓存。
   ⇒ **别去删 HF 缓存重下** —— 本地有，设变量即可（省 2.2GB / 1 小时）。
