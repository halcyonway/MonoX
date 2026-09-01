---
description: 火山引擎豆包 ASR（语音识别）skill。基于 doubao-seed-asr-2.0 大模型，支持 mp3/m4a/wav/opus/ogg 等输入，单次支持≤2小时音频，自动 ffmpeg 转 PCM、调 WebSocket 二进制协议、把转写结果持久化到本地 .asr.txt 供后续针对该音频的操作直接读取，避免重复调 API。
tier: 1
---

# ASR skill — 火山引擎豆包语音识别

调 `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream` 的 WebSocket
二进制协议，把任意音频转写成中文文本，结果持久化到本地。

## 重要：API key

环境变量名是 **`HUOSHAN_API_KEY`**（不是 `ARK_API_KEY`，也不是 `VOLC_API_KEY`）。
这个 key 必须是 **Agent Plan 专属 API key**，普通方舟 key 或 AK/SK 都无效。

Agent Plan key 的格式是 `ark-<uuid>` 前缀，在火山方舟控制台 → 开通管理 → Agent Plan
页签获取。

skill 脚本只从环境变量读 key，**不在任何地方写明 key 值**。请在 `~/.zshrc` 设置：

```sh
export HUOSHAN_API_KEY=ark-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

如果 key 没设，脚本会 `ERROR: HUOSHAN_API_KEY not set` 直接退出。

## 何时用

- 用户传一段录音（mp3/m4a/wav/opus/ogg…），要求"转写"/"听写"/"翻译"/"总结"/"提取要点" 等
- 用户要求对音频做内容分析（"这段录音说了什么"/"哪里提到张三"）
- MonoDesk 上传的语音文件 → 一律走这个 skill → 落本地 → 后续操作直接读本地文件

**不要**对同一段音频重复调 API。skill 默认把转写结果存 `<workspace>/asr/<ts>_<原文件名>.asr.txt`，
后续 agent 要基于这段音频做操作，**先 `cat .asr.txt`**，不要再调 `transcribe`。

## 工作流（agent 调用）

### 步骤 1：转写

用户提供音频 path（可能在 `~/Downloads`、`~/Desktop`、MonoDesk 上传目录等）。
MonoX 找到这个 path 后调：

```sh
python <skill_dir>/asr.py transcribe /path/to/audio.m4a
# → stderr: 进度日志（WS 连接、chunk 发送、估算费用）
# → stdout:
#   saved <workspace>/asr/<ts>_<原文件名>.asr.txt
#   saved <workspace>/asr/<ts>_<原文件名>.asr.meta.json
#   saved <traces>/asr/<ts>_<reqid>.json
#   ---
#   <完整转写文本>
```

支持的音频格式：`mp3` / `m4a` / `wav` / `opus` / `ogg` / `flac` / `aac` 等（脚本内部统一
ffmpeg 转 PCM mono 16kHz 16-bit 再调 API）。

可选参数：
- `--language en-US` 等（默认 `zh-CN`）
- `--keep-original` 保留中间 PCM 文件（debug 用）

### 步骤 2：拿到结果后

agent 收到 `<完整转写文本>` 后，可以**直接基于它**回答用户问题（如总结要点、找某个
关键词、提取待办事项）。不要再调 API。

如果用户后续又问"这段录音 30 分钟那里说了什么"，**先 cat `.asr.txt` 看看有没有**：
- 有 → 直接读 txt 回答
- 没有 → 重新 `transcribe`（可能是新音频）

### 步骤 3：本地持久化文件约定

```
.monox/workspace/asr/
├── 20260901_223045_meeting_recording.asr.txt          # 人类可读：转写 + 元信息头
├── 20260901_223045_meeting_recording.asr.meta.json    # 结构化：时长、reqid、估算费用
.monox/traces/asr/
├── 20260901_223045_<reqid>.json                       # 原始 API 响应帧（debug 用）
```

`.asr.txt` 顶部有 5 行 `#` 元信息头：
```
# ASR 转写结果
# 原始文件: meeting_recording.m4a
# 时长: 1827.4s
# 语言: zh-CN
# ReqID: abc-123-...
# 生成时间: 2026-09-01T22:30:45
# 估算费用: 0.4062 元（按 0.8 元/小时计）

<完整转写文本>
```

## 查询已有结果

```sh
python <skill_dir>/asr.py list                   # 列所有 .asr.txt + 时长
python <skill_dir>/asr.py show meeting_recording  # 按原文件名子串查最新的
```

## API key 配额 / 限制

- Agent Plan quota 在方舟控制台查看；本 skill **不**查 quota（避免凭据滥用），如果
  调用频繁失败 `HTTP 429` 或返回 quota exhausted，请到控制台充值或减频率。
- 单次音频时长限制：API 支持 ≤2 小时；超过会被 ffmpeg 拒收或 API 报错。建议超过 2h
  的音频先用 ffmpeg 切段再分次调用（脚本暂未自动切分，因为单次 2h 已覆盖大部分场景）。
- 计费：0.8 元/小时（豆包录音文件识别标准版，2026 年价格）。
  skill 在 `.asr.meta.json` 里写 `estimated_cost_cny` 字段，**仅**按音频时长估算，
  真实扣费以方舟账单为准。

## 错误处理

| 现象 | 原因 | 处理 |
|------|------|------|
| `ERROR: HUOSHAN_API_KEY not set` | zshrc 没设 | 检查 `echo $HUOSHAN_API_KEY` |
| `HTTP 401` 或 `401 Unauthorized` | key 不是 Agent Plan key | 重新从控制台 Agent Plan 页签拿 |
| `ERROR: ffmpeg not found` | 系统没装 ffmpeg | `brew install ffmpeg` |
| `API returned empty text` | 音频静音 / 格式异常 | 用 ffmpeg 单独检查音频能量 |
| 偶发 timeout | 网络抖动 | 重试一次；连续失败查控制台 quota |
| API 返回 `error_msg: "unsupported format"` | 罕见格式 | 脚本已 ffmpeg 转 PCM 一般不会触发 |

## 输出路径

- 默认 `<MONOX_WORKSPACE 或 .monox/workspace>/asr/`
- 可用 `ASR_WORKSPACE_DIR` 整体覆盖
- trace 默认 `<MONOX_TRACES 或 .monox/traces>/asr/`
- 可用 `ASR_TRACES_DIR` 覆盖