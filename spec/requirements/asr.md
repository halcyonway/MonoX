# asr：火山引擎豆包 ASR skill

## 目标与边界

在 MonoX 里落一个**语音识别** skill（ASR = Automatic Speech Recognition）：

- 接受用户提供的本地音频文件（mp3/m4a/wav/opus/ogg/flac/aac 等）
- 调火山引擎方舟 Agent Plan 的 **doubao-seed-asr-2.0** 大模型
- 转写结果**持久化到本地** `.monox/workspace/asr/`，后续 agent 操作直接读 txt
- 支持 ≤ 2 小时单次音频

**非目标**：

- 不做实时流式识别（双流 WebSocket 接口有但本 skill 不实现，单次调用足够）
- 不做说话人分离、情感识别、字幕对齐（如果后续需要再扩 model params）
- 不在本 skill 里查 Agent Plan quota（避免凭据滥用；quota 走方舟控制台）

## 服务接入信息

| 项 | 值 |
|---|---|
| 模型 | `doubao-seed-asr-2.0` |
| 资源 ID | `volc.seedasr.sauc.duration` |
| Endpoint | `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream` |
| 协议 | WebSocket 自定义二进制帧（v1 protocol, 4-byte header） |
| API key 格式 | `ark-<uuid>`（Agent Plan 专属，**不**是方舟通用 key 或 AK/SK） |
| Header | `X-Api-Key` / `X-Api-Resource-Id` / `X-Api-Connect-Id` |
| 计费 | 0.8 元/小时（2026 年价格，豆包录音文件识别标准版） |
| 时长限制 | 单次 ≤ 2 小时 |
| 音频格式 | 服务端接受 `pcm` raw + rate/bits/channel 参数最稳；mp3/ogg 也支持但有 edge case |

## 二进制协议关键点

帧结构：

```
header (4 bytes):
  byte[0] = (version << 4) | headerSize     # 0x11 = v1, 4-byte header
  byte[1] = (msgType << 4) | flags
  byte[2] = (serialization << 4) | compression
  byte[3] = 0 (reserved)

sequence (4 bytes, signed int BE) — 仅当 flags != NoSeq
payload_size (4 bytes, uint BE)
payload (JSON GZIP / raw bytes)
```

消息类型 / 标志位：

| 常量 | 值 | 用途 |
|------|----|------|
| `MSG_FULL_REQ` | `0x1` | 客户端完整请求（带 JSON 配置） |
| `MSG_AUDIO_ONLY` | `0x2` | 纯音频数据 |
| `MSG_FULL_RESP` | `0x9` | 服务端完整响应 |
| `MSG_SERVER_ACK` | `0xB` | 服务端 ACK |
| `MSG_ERROR` | `0xF` | 错误响应 |
| `FLAG_NO_SEQ` | `0x0` | 无 seq 字段 |
| `FLAG_POS_SEQ` | `0x1` | 正 seq |
| `FLAG_NEG_SEQ` | `0x2` | 负 seq（标记 last） |
| `FLAG_NEG_WITH_SEQ` | `0x3` | 负 seq + 有 seq 字段 |
| `SER_RAW` | `0x0` | raw bytes（audio 用） |
| `SER_JSON` | `0x1` | JSON（config/response 用） |
| `COMP_NONE` | `0x0` | 不压缩 |
| `COMP_GZIP` | `0x1` | GZIP 压缩 |

调用序列（按时间顺序）：

```
1. connect WS + send headers
2. send FullClientRequest (type=0x1, seq=1, JSON+GZIP)
   → 服务端 ACK (seq=1, no payload)
3. send AudioOnly chunks (type=0x2, seq=2..N, raw PCM, last seq=-N)
4. receive FullServerResponse (seq=N, JSON result.text) until is_last
```

参考实现：`extensions/skills/asr/asr.py` 的 `_build_full_request` / `_build_audio_request` / `_parse_response`。

## 工作流

### 1. 用户上传音频

- MonoDesk 上传 → 落到 MonoDesk 的 attachments 目录
- 用户手动 copy 到某个 path
- agent 拿到 path，调 `python <skill_dir>/asr.py transcribe <path>`

### 2. 内部处理

`transcribe` 子命令：

1. **ffmpeg 转 PCM**：把任意音频 → mono 16kHz 16-bit signed little-endian raw PCM
   - 这是豆包 ASR 实测最稳的输入
   - 失败立即报错，提示用户检查 ffmpeg
2. **WS 连接 + 鉴权**：用 `X-Api-Key` + `X-Api-Resource-Id` + `X-Api-Connect-Id` header
3. **FullClientRequest**：JSON 配置（含 `reqid` UUID / `language` / `enable_punc` / `enable_itn` / `nbest`）
4. **分块发 PCM**：每块 32000 bytes（~1 秒音频），最后一块用 `FLAG_NEG_WITH_SEQ` 标记结束
5. **收响应**：累积 `result.text` 字段直到 `is_last`
6. **持久化**：
   - `<workspace>/asr/<ts>_<原文件名>.asr.txt`（人类可读，5 行元信息头 + 转写文本）
   - `<workspace>/asr/<ts>_<原文件名>.asr.meta.json`（结构化元数据）
   - `<traces>/asr/<ts>_<reqid>.json`（原始 API 帧记录，debug 用）

### 3. 后续 agent 复用

- 用户问"那段录音说了什么" → agent 先 `cat .asr.txt`
- 不重复调 ASR API（即使是同一个 reqid 也不行，Agent Plan 按调用时长计费）
- 只有用户传新音频时才重新 `transcribe`

## 本地持久化格式

### `<ts>_<name>.asr.txt`

```
# ASR 转写结果
# 原始文件: <name>.<ext>
# 时长: <duration_sec>s
# 语言: zh-CN
# ReqID: <uuid>
# 生成时间: <iso8601>
# 估算费用: <0.0000 元（按 0.8 元/小时计）>

<完整转写文本>
```

人类可读，header 5 行 + 空行 + 文本。直接喂给 LLM 当 context 也行。

### `<ts>_<name>.asr.meta.json`

```json
{
  "source_audio": "/abs/path/to/audio",
  "source_size_bytes": 69731,
  "pcm_size_bytes": 321536,
  "duration_sec": 10.048,
  "estimated_cost_cny": 0.0022,
  "language": "zh-CN",
  "req_id": "uuid",
  "model": "doubao-seed-asr-2.0",
  "endpoint": "wss://...",
  "resource_id": "volc.seedasr.sauc.duration",
  "ts": "2026-09-01T22:46:25",
  "txt_path": "/abs/.../asr/<ts>_<name>.asr.txt",
  "trace_path": "/abs/.../traces/asr/<ts>_<reqid>.json",
  "char_count": 26
}
```

结构化元数据，给 agent 用：
- `duration_sec` → 是否要做摘要/分段
- `req_id` → 关联到 trace
- `estimated_cost_cny` → 给用户看账单估算
- `source_audio` → 原音频 path（用户可能忘在哪了）

### `<ts>_<reqid>.json`（trace）

```json
{
  "endpoint": "wss://...",
  "resource_id": "volc.seedasr.sauc.duration",
  "req_id": "uuid",
  "language": "zh-CN",
  "pcm_bytes": 321536,
  "frames_sent": [["full_req", 188], ["audio", 321536], ...],
  "frames_recv": [122, ...],
  "result_text": "<转写文本>",
  "ts": "2026-09-01T22:46:25"
}
```

raw API 帧记录 + 最终转写文本。debug 用：API 出问题时看是不是服务端返回了空 text / 错误码。

## API key 处理

**不在任何代码里 hardcode key 值**。脚本只读 `os.environ["HUOSHAN_API_KEY"]`，
如果没设 → `ERROR: HUOSHAN_API_KEY not set` 退出。

`SKILL.md` 写的是变量名 + key 格式说明（`ark-<uuid>` 前缀、Agent Plan 专属），
告诉用户从控制台 → 开通管理 → Agent Plan 拿。

**zshrc 模板**（用户自行设置）：

```sh
export HUOSHAN_API_KEY=ark-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

## 子命令

| 命令 | 用途 |
|------|------|
| `python asr.py transcribe <audio_path> [--language xx]` | 主入口：转写一个音频 |
| `python asr.py list` | 列所有 `.asr.txt`（按时间倒序，含时长） |
| `python asr.py show <basename>` | 按原文件名子串查最新的转写文本 |

参数：

- `--language` 默认 `zh-CN`，可选 `en-US` / `ja-JP` 等
- `--keep-original` 保留中间 PCM 文件（debug，正常不要）

## 错误处理

| 现象 | 原因 | 处理 |
|------|------|------|
| `ERROR: HUOSHAN_API_KEY not set` | zshrc 没设 | 检查 `echo $HUOSHAN_API_KEY` |
| `ERROR: ffmpeg not found` | 没装 ffmpeg | `brew install ffmpeg` |
| `WebSocket error: InvalidStatus 401` | key 不是 Agent Plan key | 重新从控制台 Agent Plan 拿 |
| 服务端返回 `error_msg` | quota / 限流 / 协议错误 | 看 `<traces>` 里的 `frames_recv` 详情 |
| `API returned empty text` | 音频静音 / 采样率不对 | 用 ffmpeg 单独检查音量；脚本已强制 16kHz mono 一般不会 |

## 后续扩展（未实现）

- **2h+ 音频自动切分**：当前 API 单次 ≤ 2h，超过会被拒。需要切分时按 VAD 静音段切，再合并结果
- **说话人分离**：豆包 ASR 支持 `diarization` 参数，开启后 result 里有 speaker_id
- **字幕生成**：用 `subtitle` 参数生成 SRT/VTT
- **MonoDesk 集成**：上传 UI → 自动调 ASR skill → 结果回写到对话历史

## 调试 cheatsheet

```sh
# 1. 直接 curl/WS 测试认证
HUOSHAN_API_KEY=xxx
python3 -c "
import asyncio, json, uuid, struct, gzip, websockets
... (见 asr.py 的实现)
"

# 2. 看所有转写结果
python <skill>/asr.py list

# 3. 看某个具体结果
python <skill>/asr.py show meeting_recording

# 4. 看原始 API trace（debug 用）
cat .monox/traces/asr/<ts>_<reqid>.json
```

## 测试用例

- ✅ 10s mp3/m4a 转写正常（已实测：`嗯，看过一些。呃...`）
- 待测：>1h 音频、英文、噪音环境、静音文件
- 待测：错误 API key 报错信息友好度
- 待测：>2h 音频切分