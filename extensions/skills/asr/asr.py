#!/usr/bin/env python3
"""asr.py — ASR skill helper: 调火山引擎语音识别 + 持久化结果到本地。

Skill 目录约定（由 SKILL.md 描述）：
  <skills_root>/asr/
    SKILL.md                # 主入口（LLM 读这个）
    asr.py                  # 本文件

工作流：
  1. 接收音频文件（mp3/m4a/wav/opus/ogg 等）
  2. ffmpeg 转 PCM (mono, 16kHz, 16-bit) — 豆包 ASR 大模型实测最稳的输入
  3. 通过 WebSocket 二进制协议调火山引擎 doubao-seed-asr-2.0
  4. 结果持久化到 <workspace>/asr/<ts>_<原文件名>.asr.txt
  5. 元信息（时长、估算费用、reqid）存到同目录 .asr.meta.json
  6. 原始 API 响应存到 .monox/traces/asr/<ts>.json（用于复盘）

后续调用：
  LLM/agent 后续要"针对这个语音做操作"时，**先 cat 本地 .asr.txt**，
  不要重复调 ASR API。

环境变量（key 等敏感信息在 SKILL.md 里只写变量名，不写值）：
  HUOSHAN_API_KEY    Agent Plan 专属 API key
                     （zshrc 里设的，调用 wss endpoint 时发 X-Api-Key header）

用法（agent 调用）：
  python asr.py transcribe <audio_path>                 # 默认 zh-CN
  python asr.py transcribe <audio_path> --language en-US
  python asr.py transcribe <audio_path> --keep-original  # 不删 ffmpeg 中间文件
  python asr.py show <basename>                         # 查已转写的文本
  python asr.py list                                    # 列所有已转写文件

workspace 默认 = .monox/workspace/（MonoX 约定），可被 ASR_WORKSPACE_DIR 覆盖。
"""

import argparse
import asyncio
import datetime as dt
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' library required. Install: pip install websockets", file=sys.stderr)
    sys.exit(1)


# ---- 路径常量 ----

SKILL_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = SKILL_DIR.parent

# workspace 默认 = .monox/workspace/（MonoX 约定）
# 派生逻辑：从 SKILLS_ROOT 往上找，直到看到 pyproject.toml（= MonoX 根）
def _find_monox_root(start: Path) -> Path:
    """从 start 向上找，直到找到包含 pyproject.toml 的目录。"""
    cur = start
    for _ in range(6):
        if (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start  # fallback：找不到就用原值

# 优先级：
#   1. 环境变量 MONOX_WORKSPACE / ASR_WORKSPACE_DIR / ASR_TRACES_DIR（用户可覆盖）
#   2. MONOX_ROOT 环境变量（用户指定 MonoX 根）
#   3. 自动探测 pyproject.toml 路径
_DEFAULT_MONOX_ROOT = Path(os.environ.get("MONOX_ROOT", _find_monox_root(SKILLS_ROOT)))
DEFAULT_WORKSPACE = Path(os.environ.get("MONOX_WORKSPACE", _DEFAULT_MONOX_ROOT / ".monox" / "workspace"))
OUTPUT_DIR = Path(os.environ.get("ASR_WORKSPACE_DIR", DEFAULT_WORKSPACE / "asr"))

# MonoX trace root (same as config.toml traces_root), used for raw API dumps
TRACES_DIR = Path(os.environ.get("ASR_TRACES_DIR", _DEFAULT_MONOX_ROOT / ".monox" / "traces" / "asr"))


# ---- 协议常量（Volcengine 豆包 ASR V3 大模型二进制协议） ----

WSS_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
RESOURCE_ID = "volc.seedasr.sauc.duration"  # doubao-seed-asr-2.0

PROTOCOL_VERSION = 0x1
HEADER_SIZE_WORDS = 0x1
MSG_FULL_REQ = 0x1
MSG_AUDIO_ONLY = 0x2
MSG_FULL_RESP = 0x9
MSG_SERVER_ACK = 0xB
MSG_ERROR = 0xF
FLAG_NO_SEQ = 0x0
FLAG_POS_SEQ = 0x1
FLAG_NEG_SEQ = 0x2
FLAG_NEG_WITH_SEQ = 0x3
SER_RAW = 0x0
SER_JSON = 0x1
COMP_NONE = 0x0
COMP_GZIP = 0x1

# 豆包 ASR 录音文件识别 0.8 元/小时（agent plan 走小时计费）
PRICE_PER_HOUR_CNY = 0.8

# PCM 切块大小：~1 秒音频（16000 采样率 × 2 字节 × 1 声道）
PCM_CHUNK_BYTES = 32000


# ---- 工具 ----

def _api_key() -> Optional[str]:
    return os.environ.get("HUOSHAN_API_KEY")


def _err(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TRACES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_basename(path: Path) -> str:
    """从音频路径派生 basename：剥后缀、保留可读性、避免路径注入。"""
    stem = path.stem
    # ASCII 字母数字 + 中文 + - _
    safe = "".join(c for c in stem if c.isalnum() or c in "-_ ")
    safe = safe.strip().replace(" ", "_")[:60]
    return safe or "audio"


def _pcm_duration_seconds(pcm_bytes: int, rate: int = 16000, bits: int = 16, channel: int = 1) -> float:
    return pcm_bytes / (rate * bits / 8 * channel)


def _convert_to_pcm(src: Path, dst: Path) -> int:
    """用 ffmpeg 转 PCM mono 16kHz 16-bit，返回输出字节数。失败 exit。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-f", "s16le",
        str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        _err("ffmpeg not found in PATH; install with: brew install ffmpeg")
    except subprocess.TimeoutExpired:
        _err(f"ffmpeg timeout after 300s converting {src}")
    if r.returncode != 0:
        _err(f"ffmpeg failed: {r.stderr[-500:]}")
    return dst.stat().st_size


def _make_header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE_WORDS,
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0,
    ])


def _build_full_request(payload: dict, sequence: int = 1) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    gz = gzip.compress(raw)
    flags = FLAG_POS_SEQ if sequence >= 0 else FLAG_NEG_WITH_SEQ
    h = _make_header(MSG_FULL_REQ, flags, SER_JSON, COMP_GZIP)
    return h + struct.pack(">i", sequence) + struct.pack(">I", len(gz)) + gz


def _build_audio_request(audio: bytes, sequence: int, final: bool = False, compress: bool = False) -> bytes:
    payload = gzip.compress(audio) if compress and audio else audio
    final_seq = -abs(sequence) if final else sequence
    flags = FLAG_NEG_WITH_SEQ if final else FLAG_POS_SEQ
    comp = COMP_GZIP if compress and audio else COMP_NONE
    h = _make_header(MSG_AUDIO_ONLY, flags, SER_RAW, comp)
    return h + struct.pack(">i", final_seq) + struct.pack(">I", len(payload)) + payload


def _parse_response(msg: bytes) -> dict:
    if len(msg) < 4:
        return {"error": "too short"}
    h_size = (msg[0] & 0x0F) * 4
    msg_type = msg[1] >> 4
    flags = msg[1] & 0x0F
    serialization = msg[2] >> 4
    compression = msg[2] & 0x0F
    payload = msg[h_size:]
    result: dict[str, Any] = {"type": msg_type, "is_last": bool(flags & FLAG_NEG_SEQ)}
    if msg_type == MSG_FULL_RESP:
        if flags & FLAG_POS_SEQ:
            seq = struct.unpack(">i", payload[:4])[0]
            result["seq"] = seq
            if seq < 0:
                result["is_last"] = True
            payload = payload[4:]
        payload_size = struct.unpack(">i", payload[:4])[0]
        result["size"] = payload_size
        body = payload[4:4 + abs(payload_size)]
    elif msg_type == MSG_SERVER_ACK:
        if len(payload) >= 4:
            seq = struct.unpack(">i", payload[:4])[0]
            result["seq"] = seq
            if seq < 0:
                result["is_last"] = True
            payload = payload[4:]
        if len(payload) >= 4:
            payload_size = struct.unpack(">I", payload[:4])[0]
            body = payload[4:4 + payload_size]
        else:
            return result
    elif msg_type == MSG_ERROR:
        result["code"] = struct.unpack(">I", payload[:4])[0]
        payload_size = struct.unpack(">I", payload[4:8])[0]
        body = payload[8:8 + payload_size]
    else:
        return result
    if compression == COMP_GZIP:
        body = gzip.decompress(body)
    if serialization == SER_JSON:
        body = json.loads(body.decode("utf-8"))
    result["body"] = body
    return result


# ---- 核心：调 ASR API ----

async def _transcribe_async(pcm_bytes: bytes, language: str, req_id: str, trace_path: Optional[Path]) -> str:
    """通过 WS 调豆包 ASR，连接→发 config→分块发 PCM→收结果。返回完整转写文本。"""
    api_key = _api_key()
    if not api_key:
        _err("HUOSHAN_API_KEY not set")

    extra_headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    print(f"WS {WSS_ENDPOINT}", file=sys.stderr)
    print(f"audio: {len(pcm_bytes)} bytes ({_pcm_duration_seconds(len(pcm_bytes)):.1f}s)", file=sys.stderr)

    frames_sent = []
    frames_recv = []

    async with websockets.connect(
        WSS_ENDPOINT, additional_headers=extra_headers, max_size=20 * 1024 * 1024
    ) as ws:
        # 1) FullClientRequest
        full_req = {
            "user": {"uid": "monox-skill"},
            "audio": {"format": "pcm", "rate": 16000, "bits": 16, "channel": 1},
            "request": {
                "reqid": req_id,
                "language": language,
                "enable_punc": True,
                "enable_itn": True,
                "nbest": 1,
            },
        }
        frame1 = _build_full_request(full_req, sequence=1)
        frames_sent.append(("full_req", len(frame1)))
        await ws.send(frame1)

        # 2) 分块发 PCM
        n_chunks = (len(pcm_bytes) + PCM_CHUNK_BYTES - 1) // PCM_CHUNK_BYTES
        for idx in range(n_chunks):
            chunk = pcm_bytes[idx * PCM_CHUNK_BYTES:(idx + 1) * PCM_CHUNK_BYTES]
            is_last = (idx == n_chunks - 1)
            seq = idx + 2  # server treats config as seq 1
            frame = _build_audio_request(chunk, sequence=seq, final=is_last, compress=False)
            frames_sent.append(("audio", len(frame)))
            await ws.send(frame)

        # 3) 收响应直到 last
        full_text_parts = []
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                frames_recv.append(len(msg))
                r = _parse_response(msg)
                body = r.get("body", "")
                if isinstance(body, dict):
                    text = body.get("result", {}).get("text", "")
                    if text:
                        full_text_parts.append(text)
                if r.get("is_last"):
                    break
        except asyncio.TimeoutError:
            print("WARN: timeout waiting for final response", file=sys.stderr)

    full_text = "".join(full_text_parts).strip()

    # 存原始 trace
    if trace_path:
        trace = {
            "endpoint": WSS_ENDPOINT,
            "resource_id": RESOURCE_ID,
            "req_id": req_id,
            "language": language,
            "pcm_bytes": len(pcm_bytes),
            "frames_sent": frames_sent,
            "frames_recv": frames_recv,
            "result_text": full_text,
            "ts": dt.datetime.now().isoformat(),
        }
        trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")

    return full_text


def cmd_transcribe(args: argparse.Namespace) -> int:
    """主入口：转写单个音频文件。"""
    _ensure_dirs()

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        _err(f"audio file not found: {audio_path}")

    # 1) ffmpeg → PCM
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
        tmp_pcm = Path(tmp.name)
    try:
        pcm_size = _convert_to_pcm(audio_path, tmp_pcm)
        pcm_bytes = tmp_pcm.read_bytes()
        if not args.keep_original:
            tmp_pcm.unlink()
    except Exception:
        tmp_pcm.unlink(missing_ok=True)
        raise

    duration_sec = _pcm_duration_seconds(pcm_size)
    duration_hr = duration_sec / 3600.0
    est_cost_cny = duration_hr * PRICE_PER_HOUR_CNY

    print(f"audio: {audio_path.name} ({audio_path.stat().st_size} bytes)", file=sys.stderr)
    print(f"duration: {duration_sec:.1f}s (~{est_cost_cny:.4f} 元)", file=sys.stderr)

    # 2) 调 API
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    req_id = str(uuid.uuid4())
    trace_path = TRACES_DIR / f"{ts}_{req_id}.json"

    try:
        full_text = asyncio.run(
            _transcribe_async(pcm_bytes, args.language, req_id, trace_path)
        )
    except websockets.exceptions.WebSocketException as e:
        _err(f"WebSocket error: {e}")
    except Exception as e:
        _err(f"transcribe failed: {e}")

    if not full_text:
        _err("API returned empty text (audio may be silent or format mismatch)")

    # 3) 持久化文本
    safe_base = _safe_basename(audio_path)
    txt_path = OUTPUT_DIR / f"{ts}_{safe_base}.asr.txt"
    meta_path = OUTPUT_DIR / f"{ts}_{safe_base}.asr.meta.json"

    txt_content = f"# ASR 转写结果\n# 原始文件: {audio_path.name}\n# 时长: {duration_sec:.1f}s\n# 语言: {args.language}\n# ReqID: {req_id}\n# 生成时间: {dt.datetime.now().isoformat()}\n# 估算费用: {est_cost_cny:.4f} 元（按 {PRICE_PER_HOUR_CNY} 元/小时计）\n\n{full_text}\n"
    txt_path.write_text(txt_content, encoding="utf-8")

    meta = {
        "source_audio": str(audio_path),
        "source_size_bytes": audio_path.stat().st_size,
        "pcm_size_bytes": pcm_size,
        "duration_sec": duration_sec,
        "estimated_cost_cny": est_cost_cny,
        "language": args.language,
        "req_id": req_id,
        "model": "doubao-seed-asr-2.0",
        "endpoint": WSS_ENDPOINT,
        "resource_id": RESOURCE_ID,
        "ts": dt.datetime.now().isoformat(),
        "txt_path": str(txt_path),
        "trace_path": str(trace_path),
        "char_count": len(full_text),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 输出给 agent 调用者
    print(f"saved {txt_path}")
    print(f"saved {meta_path}")
    print(f"saved {trace_path}")
    print(f"---")
    print(full_text)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """列所有 .asr.txt 结果（按时间倒序）。"""
    _ensure_dirs()
    files = sorted(OUTPUT_DIR.glob("*.asr.txt"), reverse=True)
    if not files:
        print("(no ASR results yet)")
        return 0
    for f in files:
        meta_path = f.with_suffix(".meta.json")
        dur = "?"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                dur = f"{meta.get('duration_sec', 0):.1f}s"
            except Exception:
                pass
        print(f"{f.name:50s}  {dur}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """按 basename（前缀）查 .asr.txt 并打印。"""
    _ensure_dirs()
    # basename 允许部分匹配（取最近的）
    matches = sorted(OUTPUT_DIR.glob(f"*{args.basename}*.asr.txt"), reverse=True)
    if not matches:
        _err(f"no ASR result for basename: {args.basename}")
    if len(matches) > 1:
        print(f"WARN: multiple matches, using newest: {matches[0].name}", file=sys.stderr)
    sys.stdout.write(matches[0].read_text(encoding="utf-8"))
    return 0


# ---- argparse ----

def main() -> int:
    ap = argparse.ArgumentParser(description="ASR skill helper: Volcengine Doubao ASR + persist to local")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_trans = sub.add_parser("transcribe", help="transcribe an audio file")
    p_trans.add_argument("audio", help="input audio path (mp3/m4a/wav/opus/ogg etc)")
    p_trans.add_argument("--language", default="zh-CN", help="zh-CN (default), en-US, ja-JP, etc")
    p_trans.add_argument("--keep-original", action="store_true", help="keep converted PCM file (debug)")

    p_list = sub.add_parser("list", help="list all ASR results")
    p_show = sub.add_parser("show", help="show ASR text by basename")
    p_show.add_argument("basename", help="substring of original filename")

    args = ap.parse_args()
    return {
        "transcribe": cmd_transcribe,
        "list": cmd_list,
        "show": cmd_show,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())