# 本地模型：bge-m3

Asuka 的 embedding 用 **BAAI/bge-m3**（1024 维，中英双语，8k 上下文）。

## 放在哪

权重**不进版本库**（2.1GB，可重新下载）。约定目录：

```
<repo>/.asuka-models/bge-m3/
```

用环境变量指过去：

```bash
export ASUKA_EMBED_MODEL_PATH=.asuka-models/bge-m3
```

不设也能跑 —— `sentence-transformers` 会自己去 HuggingFace 找。
但**在本机跑不通**，见下面的"为什么用 ModelScope"。

⚠️ `ASUKA_EMBED_MODEL_PATH` **不影响 embedder 的签名**。
签名是 `BAAI/bge-m3@1024` —— 身份是**模型**，不是"权重放在哪"。
否则把权重从 A 目录挪到 B 目录，索引就会被判成"换过模型"而拒绝查询，
这是把**部署细节**混进了**语义身份**。

## 怎么下

小文件（`config.json` / `tokenizer.json` / `sentencepiece.bpe.model` …）用下面这段就够：

```bash
D=.asuka-models/bge-m3
B=https://modelscope.cn/models/BAAI/bge-m3/resolve/master
mkdir -p "$D/1_Pooling"
for f in config.json config_sentence_transformers.json modules.json \
         sentence_bert_config.json special_tokens_map.json tokenizer.json \
         tokenizer_config.json sentencepiece.bpe.model; do
  curl -sL --retry 3 -o "$D/$f" "$B/$f"
done
curl -sL --retry 3 -o "$D/1_Pooling/config.json" "$B/1_Pooling/config.json"
```

大文件（2.1GB）用脚本 **`asuka/models/fetch_bge_m3.sh`** —— 它做三件事：

1. 把已有的 `pytorch_model.bin` 当 **part0** 复用（不重头下）
2. 剩下的字节切 **8 段 `curl -r`** 并行拉，每段独立重试
3. 合并后**用 `stat` 核对总字节数**，不等于 `2271145830` 就拒绝合并

```bash
bash asuka/models/fetch_bge_m3.sh
```

脚本里两条硬规矩（都是踩出来的，别删）：

- **只认 HTTP 206**：`-w "%{http_code}"` 单独取码，不是 206 就丢弃那段 ——
  代理有可能忽略 `Range` 直接回 200（整份文件），那样按偏移追加会**静默写坏**
- **不用 `--retry`**：重试由外层循环做。curl 自己重试一个 range 请求会**重复追加**字节

### 并行**不提速** —— 限速是按 IP 全局的

实测（同一时刻、同一 URL）：

| 方式 | 聚合速度 |
|---|---|
| 单连接 | 548 KB/s |
| 8 段并行 | 492 KB/s |
| 绕过代理直连 | 68 KB/s（更差） |

也就是说瓶颈**不是**每连接限速，而是 ~500–620 KB/s 的**全局限速**。
分片的价值不在提速，而在**单段断了只损失一段**。别指望 8 倍。

### 校验

`pytorch_model.bin` 应为 **2271145830** 字节（ModelScope 与 HuggingFace 完全一致）。

还要有 `sentencepiece` —— bge-m3 是 XLM-RoBERTa 系，
缺了它 `transformers` 可能加载不了 tokenizer：

```bash
python -m pip install sentencepiece
```

## 为什么用 ModelScope 而不是 HuggingFace

三条路都试过，按时间顺序：

| 路 | 结果 |
|---|---|
| `huggingface.co` | **不通**（curl 返回空） |
| `hf-mirror.com` | 小文件可以；**大文件在 ~34MB 处被掐断**（`schannel: server closed abruptly`），2.1GB 要重连 ~70 次 |
| **ModelScope** | 通，~510 KB/s，断点续传循环可完成 |

另外 `huggingface_hub` 即使设了 `HF_ENDPOINT=https://hf-mirror.com` 也会
**写出 0 字节的 config.json 并报"成功"**。根因见下一条（沙箱写入限制），
不是 HF 的问题。顺带：`HF_HUB_DISABLE_XET=1` 能消掉 Xet 那条路径的报错。

## ⚠️ 两个会浪费你半小时的环境陷阱

### 1. 沙箱只允许写**项目目录内**，而且失败是静默的

```bash
curl -sL -o /tmp/x.json <url>     # 报告 http=200 size=687，但 /tmp/x.json **根本不存在**
curl -sL -o ./x.json  <url>       # 正常
```

`curl -w` 会说 `size_download=687`、退出码 0 —— **看起来完全成功**。
所以：**任何下载都要落到项目目录内**，并在下载后**用 `stat` 核对字节数**，
不要相信 curl 的退出码。（`huggingface_hub` 的 0 字节 config.json 就是这么来的。）

顺带：**`/dev/null` 也写不了**。测速时 `curl -o /dev/null` 会返回
`size=0 speed=0` + 退出码 23（write error），**看起来像"服务器不给数据"**。
测速要写到项目目录里的临时文件，否则会得出完全错误的结论。

### 2. 环境里有一条 HTTP 代理，大响应会卡死

```
http_proxy=http://127.0.0.1:64216
```

- 小响应正常
- **大响应**（PyPI 的 `torch` 索引页、2GB 的 wheel）会**卡住不报错** ——
  pip 会静默地停在那里，既不超时也不失败。实测 pip 缓存 25 秒零增长。
- 大文件下载要**断点续传 + 循环重试**，单次 curl 一定会被掐断

## pip 源

**用默认源**（`%APPDATA%\pip\pip.ini`）：

```bash
python -m pip config set global.index-url https://pypi.org/simple
```

曾经配过清华源，实测在本环境**不可靠**：`pip install sentence-transformers` 返回
`from versions: none`，`pip install sentencepiece` 报
`ReadTimeoutError (read timeout=15)`。而同一时刻用 curl 直接拉清华源的索引页
是 **200 / 248KB / 3s** —— 源本身没问题，是**代理在 ~250KB 的响应上卡住**（见陷阱 2）。

结论：装不上先怀疑**代理**，再怀疑源，最后才怀疑包名。

## 依赖分工

| 用途 | 解释器 | 依赖 |
|---|---|---|
| 单测 | `.../python/versions/3.13.12/python.exe` | **零第三方**（含 `asuka/` 核心层） |
| 建索引 / 跑模型 | `.../python/envs/default/Scripts/python.exe` | langchain-text-splitters / qdrant-client / sentence-transformers |

实测可用版本：`torch 2.14.0+cpu` / `sentence-transformers 6.0.1` / `transformers 5.17.0`
/ `sentencepiece 0.2.2` / `qdrant-client 1.19.1`。
