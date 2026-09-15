---
description: 火山引擎豆包 ASR（语音识别）skill。基于 doubao-seed-asr-2.0 大模型，支持 mp3/m4a/wav/opus/ogg 等输入，>10min 自动切成 10min/段并并发调 WS API（限 3 并发 + 单段 3 次指数退避重试），结果顺序拼接、单段失败不阻断、其余段正常出文本，所有转写持久化到本地 .asr.txt 供后续针对该音频的操作直接读取，避免重复调 API。通过 exec_cli mono_asr 调用。
tier: 1
---

# ASR skill — 火山引擎豆包语音识别

调 `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream` 的 WebSocket
二进制协议，把任意音频转写成中文文本，结果持久化到本地。

## 调用方式

通过 `exec_cli` 调 CLI server（端口 8769，本机常驻）：

```sh
exec_cli mono_asr transcribe <audio_path> [--language en-US] [--keep-chunks]
                                      [--chunk-sec 600] [--concurrency 3]
exec_cli mono_asr list                          # 列所有已转写
exec_cli mono_asr show <basename>               # 按 basename 子串查最新
```

## 长音频策略（> 10 分钟）

脚本会自动：
1. `ffprobe` 探测总时长
2. **> 10 min** → 多次调 `ffmpeg -ss <start> -i <src> -t 600 -f s16le ...` 切成 10min PCM 块
   > 不用 `-f segment` 是因为 ffmpeg 8.0 segment muxer 对 raw PCM 输出有 bug（实测 60s 输入只输出 15s）。
   > `-ss` 在 `-i` 之前 = fast seek，AAC/m4a 直接 seek 到最近 keyframe，单次 ffmpeg <1s。
3. `asyncio.gather` + `Semaphore(3)` 并发调 WS（同时 ≤3 个连接）
4. 每块最多 **3 次重试**，指数退避 2s → 4s → 8s
5. 按 idx 顺序拼接，失败块用 `[chunk N failed: <err>]` 占位（其他块照样出文本）
6. 临时 chunk PCM 在脚本退出时自动清掉

## 重要：API key

环境变量名是 **`HUOSHAN_API_KEY`**（不是 `ARK_API_KEY`，也不是 `VOLC_API_KEY`）。
这个 key 必须是 **Agent Plan 专属 API key**，普通方舟 key 或 AK/SK 都无效。

Agent Plan key 的格式是 `ark-<uuid>` 前缀，在火山方舟控制台 → 开通管理 → Agent Plan
页签获取。

skill 脚本只从环境变量读 key，**不在任何地方写明 key 值**。请在 `~/.zshrc` 设置：

```sh
export HUOSHAN_API_KEY=ark-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

如果 key 没设，CLI 会返回 `{"ok":false,"error":{"message":"HUOSHAN_API_KEY not set","hint":"..."}}`。

## 何时用

- 用户传一段录音（mp3/m4a/wav/opus/ogg…），要求"转写"/"听写"/"翻译"/"总结"/"提取要点" 等
- 用户要求对音频做内容分析（"这段录音说了什么"/"哪里提到张三"）
- MonoDesk 上传的语音文件 → 一律走这个 skill → 落本地 → 后续操作直接读本地文件

**不要**对同一段音频重复调 API。skill 默认把转写结果存 `<workspace>/asr/<ts>_<原文件名>.asr.txt`，
后续 agent 要基于这段音频做操作，**先 `cat .asr.txt`**，不要再调 `transcribe`。

## CLI 输出格式

`transcribe` 成功：

```json
{
  "ok": true,
  "data": {
    "txt_path": "/path/to/.asr.txt",
    "meta_path": "/path/to/.asr.meta.json",
    "trace_paths": ["/path/to/<reqid>_chunk000.json", ...],
    "ok_chunks": 3,
    "err_chunks": 0,
    "duration_sec": 1800.0,
    "estimated_cost_cny": 0.4,
    "language": "zh-CN",
    "req_id_master": "...",
    "text": "<完整转写文本>"
  }
}
```

`text` 字段含拼接好的完整文本（失败块用 `[chunk N failed: ...]` 占位）。

### 步骤 2：拿到结果后

agent 收到 `data.text` 后，可以**直接基于它**回答用户问题（如总结要点、找某个
关键词、提取待办事项）。不要再调 API。

如果用户后续又问"这段录音 30 分钟那里说了什么"，**先 cat `.asr.txt`**：

```sh
exec_cli mono_asr show <basename>
# 或直接读本地文件
cat <txt_path>
```

**注意**：若 `.asr.txt` 里出现 `[chunk N failed: ...]` 占位，说明第 N 段转写没成功
（网络 / 限流等）。如果用户问的内容恰好在那段时间，先告知用户「这部分没转写出来，
听不了」。**不要**为了「补」而重跑整个音频（成本叠加）——除非用户明确同意。

### 步骤 3：本地持久化文件约定

```
.monox/workspace/asr/
├── 20260901_223045_meeting_recording.asr.txt          # 人类可读：转写 + 元信息头
├── 20260901_223045_meeting_recording.asr.meta.json    # 结构化：时长、reqid、chunks 数组
.monox/traces/asr/
├── <reqid_master>_chunk000.json                       # 每块一个原始 API 帧 dump
├── <reqid_master>_chunk001.json
└── ...
```

`.asr.txt` 顶部有 8 行 `#` 元信息头（多段时多两行）：
```
# ASR 转写结果
# 原始文件: meeting_recording.m4a
# 时长: 1827.4s
# 语言: zh-CN
# 切分: chunked (12 块, 12 ok / 0 failed)
# ReqID (master): abc-123-...
# 生成时间: 2026-09-01T22:30:45
# 估算费用: 0.4062 元（按 0.8 元/小时计）

<完整转写文本>
```

## API key 配额 / 限制

- Agent Plan quota 在方舟控制台查看；本 skill **不**查 quota（避免凭据滥用），如果
  调用频繁失败 `HTTP 429` 或返回 quota exhausted，请到控制台充值或减频率。
- 单次音频时长限制：API 支持 ≤2 小时。脚本自动切分覆盖到 ≤2h（最长 12 段）。
- 计费：0.8 元/小时（豆包录音文件识别标准版，2026 年价格）。
  skill 在 envelope `estimated_cost_cny` 字段估算（按音频时长），真实扣费以方舟账单为准。

## 错误处理

| 现象 | 处理 |
|---|---|
| `HUOSHAN_API_KEY not set` | zshrc 没设，检查 `echo $HUOSHAN_API_KEY` |
| `HTTP 401` 或 `401 Unauthorized` | key 不是 Agent Plan key，重新从控制台 Agent Plan 页签拿 |
| `ffmpeg not found` | 系统没装 ffmpeg，`brew install ffmpeg` |
| API 返回空 text | 音频静音 / 格式异常，用 ffmpeg 单独检查音频能量 |
| 单块失败 | 自动 retry 最多 3 次（指数退避） |
| `.asr.txt` 里出现 `[chunk N failed: ...]` | 该块 3 次都失败，看 `meta.chunks[N].error`；其他块照常用 |
| `exec_cli: cannot reach CLI server at ...` | 先 `uv run python -m extensions.cli.inner.server &` 起 server |

## 输出路径

- 默认 `<MONOX_WORKSPACE 或 .monox/workspace>/asr/`
- 可用 `ASR_WORKSPACE_DIR` 整体覆盖
- trace 默认 `<MONOX_TRACES 或 .monox/traces>/asr/`
- 可用 `ASR_TRACES_DIR` 覆盖
