#!/usr/bin/env python3
"""asr.py — ASR skill helper: 调火山引擎语音识别 + 持久化结果到本地。

Skill 目录约定（由 SKILL.md 描述）：
  <skills_root>/asr/
    SKILL.md                # 主入口（LLM 读这个）
    asr.py                  # 本文件

工作流：
  1. 接收音频文件（mp3/m4a/wav/opus/ogg 等）
  2. ffprobe 时长 → 若 > 10 分钟，ffmpeg -f segment 切成 10min PCM 块
  3. 每块独立通过 WebSocket 二进制协议调火山引擎 doubao-seed-asr-2.0
     （asyncio.gather + Semaphore 限并发 3，单块 3 次指数退避重试）
  4. 结果按块顺序拼接，失败块用 [chunk N failed: <err>] 占位
  5. 持久化到 <workspace>/asr/<ts>_<原文件名>.asr.txt
  6. 元信息（每块状态、估算费用、reqid）存到同目录 .asr.meta.json
  7. 原始 API 响应存到 .monox/traces/asr/<ts>_<reqid>_chunkN.json（用于复盘）

后续调用：
  LLM/agent 后续要"针对这个语音做操作"时，**先 cat 本地 .asr.txt**，
  不要重复调 ASR API。

环境变量（key 等敏感信息在 SKILL.md 里只写变量名，不写值）：
  HUOSHAN_API_KEY    Agent Plan 专属 API key
                     （zshrc 里设的，调用 wss endpoint 时发 X-Api-Key header）

用法（agent 调用）：
  python asr.py transcribe <audio_path>                 # 默认 zh-CN
  python asr.py transcribe <audio_path> --language en-US
  python asr.py transcribe <audio_path> --keep-chunks   # 不删 ffmpeg 切分中间文件
  python asr.py transcribe <audio_path> --chunk-sec 600 # 切分粒度（默认 600s=10min）
  python asr.py transcribe <audio_path> --concurrency 3 # 并发 WS 数（默认 3）
  python asr.py show <basename>                         # 查已转写的文本
  python asr.py list                                    # 列所有已转写文件

workspace 默认 = .monox/workspace/（MonoX 约定），可被 ASR_WORKSPACE_DIR 覆盖。
"""
from __future__ import annotations

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
    import websockets.exceptions
except ImportError:
    print("ERROR: 'websockets' library required. Install: pip install websockets", file=sys.stderr)
    sys.exit(1)


# ---- 路径常量 ----

SKILL_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = SKILL_DIR.parent


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


# ---- 长音频切分 + 并发参数 ----

DEFAULT_CHUNK_SEC = 600           # 10 分钟/段（按用户需求）
DEFAULT_CONCURRENCY = 3            # 同时打开的 WS 连接数（保守起见；Volcengine 速率未知）
MAX_RETRY = 3                      # 单块最大重试次数
RETRY_BACKOFF_SEC = (2, 4, 8)      # 第 1/2/3 次失败后的等待（指数退避）
CHUNK_FAIL_MARKER = "[chunk {idx} failed: {err}]"  # 失败块在拼接文本里的占位


# ---- 工具 ----

def _api_key() -> Optional[str]:
    return os.environ.get("HUOSHAN_API_KEY")


def _err(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(msg, file=sys.stderr)


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


def _probe_duration(src: Path) -> float:
    """ffprobe 探测音频时长（秒）。失败返回 0（调用方按 0 处理 = 不切分）。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        _warn("ffprobe not found; falling back to single-call path (no chunking)")
        return 0.0
    except subprocess.TimeoutExpired:
        _warn("ffprobe timeout; falling back to single-call path")
        return 0.0
    if r.returncode != 0 or not r.stdout.strip():
        return 0.0
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _split_audio(src: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
    """用 ffmpeg 把音频切成 ~chunk_seconds 一段的 PCM。

    实现：每次调一次 ffmpeg，用 `-ss <start> -t <chunk_seconds>` 抽一段。
    不用 `-f segment` 是因为 ffmpeg 8.0 segment muxer 对 raw PCM 输出有 bug
    （实测 segment_time=20s 输出只有 ~15s，segment_time=600s 60s 输入也只有 15s 输出），
    `-ss + -t` 多次调用稳定可靠。

    返回按 idx 排序的 PCM 路径列表（chunk_000.pcm, chunk_001.pcm, ...）。
    失败 → _err 退出。
    """
    chunk_dir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration(src)
    if duration <= 0:
        _err(f"cannot split: probe_duration returned 0 for {src}")
    n_chunks = int(duration // chunk_seconds) + (1 if duration % chunk_seconds > 0 else 0)
    if n_chunks < 1:
        n_chunks = 1

    chunks: list[Path] = []
    for idx in range(n_chunks):
        start = idx * chunk_seconds
        chunk_path = chunk_dir / f"chunk_{idx:03d}.pcm"
        # -ss BEFORE -i = fast seek（不解码之前的帧，AAC 直接 seek 到最近 keyframe）
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t", str(chunk_seconds),
            "-ac", "1", "-ar", "16000", "-f", "s16le",
            str(chunk_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            _err("ffmpeg not found in PATH; install with: brew install ffmpeg")
        except subprocess.TimeoutExpired:
            _err(f"ffmpeg chunk {idx} timeout after 120s")
        if r.returncode != 0:
            _err(f"ffmpeg chunk {idx} failed: {r.stderr[-500:]}")
        # Past end of audio → ffmpeg produces 0-byte file → 停止
        if chunk_path.stat().st_size == 0:
            chunk_path.unlink(missing_ok=True)
            break
        chunks.append(chunk_path)
    if not chunks:
        _err("ffmpeg produced no chunks")
    return chunks


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
    """通过 WS 调豆包 ASR，连接→发 config→分块发 PCM→收结果。返回完整转写文本。

    失败抛 WebSocketException / asyncio.TimeoutError / OSError —— 上层重试逻辑处理。
    """
    api_key = _api_key()
    if not api_key:
        _err("HUOSHAN_API_KEY not set")

    extra_headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    _info(f"  WS {WSS_ENDPOINT} req_id={req_id[:8]}…")
    _info(f"  audio: {len(pcm_bytes)} bytes ({_pcm_duration_seconds(len(pcm_bytes)):.1f}s)")

    frames_sent = []
    frames_recv = []

    async with websockets.connect(
        WSS_ENDPOINT, additional_headers=extra_headers, max_size=20 * 1024 * 1024,
        # ping_interval=None 禁止自动 keepalive ping。
        # 原因：长 chunk（≤10min PCM）或并发路径（Semaphore(3) 3 个 WS 同时竞争事件循环）下，
        # 事件循环繁忙时客户端无法在 ping_timeout 内回 pong → 服务端断开连接。
        # 关掉主动 ping 后，audio 上传 + transcript 接收本身就让连接保持活跃，
        # 不需要额外的 keepalive 心跳。
        ping_interval=None, ping_timeout=None,
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
        # 重要：server 每个 frame 返回的都是「到目前为止识别到的累积文本」（增量发送），
        # 不能用 append 拼接 — 那样会把同一段文字重复 N 次（实测 600s chunk 拼出 929K 字，
        # 包含 580 次重复子串）。正确做法：保留**最后一个非空 text**（server 最终态最完整）。
        full_text = ""
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
                frames_recv.append(len(msg))
                r = _parse_response(msg)
                body = r.get("body", "")
                if isinstance(body, dict):
                    text = body.get("result", {}).get("text", "")
                    if text:
                        full_text = text  # 覆盖：最后一个非空 text 就是最终态
                if r.get("is_last"):
                    break
        except asyncio.TimeoutError:
            _warn(f"  req_id={req_id[:8]}… timeout waiting for final response")

    full_text = full_text.strip()

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


# ---- 多块并发 + 重试 ----

async def _transcribe_chunk_with_retry(
    chunk_path: Path,
    chunk_idx: int,
    language: str,
    sem: asyncio.Semaphore,
    trace_dir: Path,
) -> tuple[int, str, dict]:
    """单块转写：受 sem 限并发，最多重试 MAX_RETRY 次。

    返回 (chunk_idx, text, meta_dict)。meta 包含 status="ok"/"error"，
    调用方按 idx 排序后拼接。
    """
    pcm_bytes = chunk_path.read_bytes()
    duration_sec = _pcm_duration_seconds(len(pcm_bytes))
    last_err: Optional[str] = None

    for attempt in range(1, MAX_RETRY + 1):
        try:
            async with sem:
                req_id = str(uuid.uuid4())
                trace_path = trace_dir / f"{req_id}_chunk{chunk_idx:03d}.json"
                text = await _transcribe_async(pcm_bytes, language, req_id, trace_path)
            _info(f"  chunk {chunk_idx} ok ({duration_sec:.1f}s, attempt {attempt})")
            return chunk_idx, text, {
                "chunk_idx": chunk_idx,
                "status": "ok",
                "req_id": req_id,
                "duration_sec": duration_sec,
                "attempt": attempt,
                "trace_path": str(trace_path),
            }
        except (websockets.exceptions.WebSocketException,
                ConnectionError,
                asyncio.TimeoutError,
                OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRY:
                backoff = RETRY_BACKOFF_SEC[attempt - 1]
                _warn(f"  chunk {chunk_idx} attempt {attempt} failed ({last_err}); retry in {backoff}s")
                await asyncio.sleep(backoff)

    # MAX_RETRY 次都失败
    _warn(f"  chunk {chunk_idx} FAILED after {MAX_RETRY} attempts: {last_err}")
    return chunk_idx, "", {
        "chunk_idx": chunk_idx,
        "status": "error",
        "error": last_err,
        "duration_sec": duration_sec,
        "attempts": MAX_RETRY,
    }


async def _transcribe_chunks_parallel(
    chunks: list[Path],
    language: str,
    concurrency: int,
    trace_dir: Path,
) -> list[tuple[int, str, dict]]:
    """并发跑所有 chunk，按 chunk_idx 排序返回。

    单块失败不影响其他块；失败块 text 为 ""，调用方决定占位文本。
    """
    sem = asyncio.Semaphore(concurrency)
    coros = [
        _transcribe_chunk_with_retry(p, idx, language, sem, trace_dir)
        for idx, p in enumerate(chunks)
    ]
    # return_exceptions=True 把异常也吞掉（理论上 _transcribe_chunk_with_retry 已经内部捕获）
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[tuple[int, str, dict]] = []
    for idx, r in enumerate(results):
        if isinstance(r, Exception):
            # 兜底：理论上不该走到这里，但保险
            out.append((idx, "", {
                "chunk_idx": idx, "status": "error",
                "error": f"{type(r).__name__}: {r}",
                "duration_sec": 0.0, "attempts": 0,
            }))
        else:
            out.append(r)
    # 按 idx 排序（gather 已经按 coros 顺序返回，但保险起见再排一次）
    out.sort(key=lambda x: x[0])
    return out


def _assemble_text(results: list[tuple[int, str, dict]]) -> str:
    """把每块结果按 idx 拼成完整文本。失败块用占位 marker。"""
    parts: list[str] = []
    for idx, text, meta in results:
        if meta["status"] == "ok":
            if text:
                parts.append(text)
        else:
            err = meta.get("error", "unknown")
            parts.append(CHUNK_FAIL_MARKER.format(idx=idx, err=err))
    return "\n".join(parts).strip()


def cmd_transcribe(args: argparse.Namespace) -> int:
    """主入口：转写音频文件。

    流程：
      1. ffprobe 时长
      2. ≤ chunk_sec → 单块走 _convert_to_pcm + _transcribe_async
         > chunk_sec → 切分到 temp dir，asyncio.gather 并发转写，cleanup
      3. 拼结果 → .asr.txt + .asr.meta.json（meta 含 chunks 数组）
    """
    _ensure_dirs()

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        _err(f"audio file not found: {audio_path}")

    chunk_sec = int(args.chunk_sec)
    concurrency = int(args.concurrency)

    # 1) 探测时长 → 决定切分策略
    duration_sec = _probe_duration(audio_path)
    if duration_sec <= 0:
        # ffprobe 失败 / 拿不到 → 走单块路径（向后兼容）
        use_chunked = False
    else:
        use_chunked = duration_sec > chunk_sec

    print(f"audio: {audio_path.name} ({audio_path.stat().st_size} bytes)", file=sys.stderr)
    if duration_sec > 0:
        print(f"duration: {duration_sec:.1f}s, "
              f"{'chunked' if use_chunked else 'single'}", file=sys.stderr)
    if use_chunked:
        n_chunks_est = int(duration_sec // chunk_sec) + 1
        print(f"chunk_sec={chunk_sec}s concurrency={concurrency} est_n_chunks={n_chunks_est}",
              file=sys.stderr)

    # 2) 准备 PCM 块
    tmpdir: Optional[tempfile.TemporaryDirectory] = None
    try:
        if use_chunked:
            tmpdir = tempfile.TemporaryDirectory(prefix="asr_chunks_")
            chunk_dir = Path(tmpdir.name)
            chunks = _split_audio(audio_path, chunk_dir, chunk_sec)
            _info(f"split into {len(chunks)} chunks of ~{chunk_sec}s")
        else:
            # 单块路径：先转 PCM 到临时文件
            tmpdir = tempfile.TemporaryDirectory(prefix="asr_pcm_")
            chunk_dir = Path(tmpdir.name)
            tmp_pcm = chunk_dir / "single.pcm"
            _convert_to_pcm(audio_path, tmp_pcm)
            chunks = [tmp_pcm]

        # 4) 并发转写
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        req_id_master = str(uuid.uuid4())  # 主 ID（多块 trace 关联用）
        results = asyncio.run(_transcribe_chunks_parallel(
            chunks, args.language, concurrency, TRACES_DIR,
        ))

        # 5) 拼结果
        full_text = _assemble_text(results)
        # 重算总时长（按 chunk 实际 duration 求和，更准）
        actual_total_duration = sum(m["duration_sec"] for _, _, m in results)
        if actual_total_duration == 0 and duration_sec > 0:
            actual_total_duration = duration_sec
        duration_hr = actual_total_duration / 3600.0
        est_cost_cny = duration_hr * PRICE_PER_HOUR_CNY

        # 成功块数
        ok_chunks = sum(1 for _, _, m in results if m["status"] == "ok")
        err_chunks = len(results) - ok_chunks
        if not full_text:
            _err("API returned empty text for all chunks (audio may be silent or format mismatch)")

        # 6) 持久化
        safe_base = _safe_basename(audio_path)
        txt_path = OUTPUT_DIR / f"{ts}_{safe_base}.asr.txt"
        meta_path = OUTPUT_DIR / f"{ts}_{safe_base}.asr.meta.json"

        header = (
            f"# ASR 转写结果\n"
            f"# 原始文件: {audio_path.name}\n"
            f"# 时长: {actual_total_duration:.1f}s\n"
            f"# 语言: {args.language}\n"
            f"# 切分: {'chunked' if use_chunked else 'single'} "
            f"({len(results)} 块, {ok_chunks} ok / {err_chunks} failed)\n"
            f"# ReqID (master): {req_id_master}\n"
            f"# 生成时间: {dt.datetime.now().isoformat()}\n"
            f"# 估算费用: {est_cost_cny:.4f} 元（按 {PRICE_PER_HOUR_CNY} 元/小时计）\n\n"
        )
        txt_path.write_text(header + full_text + "\n", encoding="utf-8")

        meta = {
            "source_audio": str(audio_path),
            "source_size_bytes": audio_path.stat().st_size,
            "duration_sec": actual_total_duration,
            "estimated_cost_cny": est_cost_cny,
            "language": args.language,
            "req_id_master": req_id_master,
            "model": "doubao-seed-asr-2.0",
            "endpoint": WSS_ENDPOINT,
            "resource_id": RESOURCE_ID,
            "ts": dt.datetime.now().isoformat(),
            "txt_path": str(txt_path),
            "chunked": use_chunked,
            "chunk_sec": chunk_sec if use_chunked else None,
            "concurrency": concurrency if use_chunked else 1,
            "chunks": [
                {
                    "idx": idx,
                    "status": m["status"],
                    "duration_sec": m["duration_sec"],
                    "req_id": m.get("req_id"),
                    "attempt": m.get("attempt") or m.get("attempts"),
                    "error": m.get("error"),
                    "trace_path": m.get("trace_path"),
                }
                for idx, _, m in results
            ],
            "ok_chunks": ok_chunks,
            "err_chunks": err_chunks,
            "char_count": len(full_text),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # 7) stdout 给 agent
        print(f"saved {txt_path}")
        print(f"saved {meta_path}")
        for _, _, m in results:
            if m.get("trace_path"):
                print(f"saved {m['trace_path']}")
        print(f"---")
        print(f"# {ok_chunks}/{len(results)} chunks ok, est {est_cost_cny:.4f} 元")
        print(full_text)
        return 0

    finally:
        # 清理临时 chunk PCM（除非 --keep-chunks）
        if tmpdir is not None and not args.keep_chunks:
            try:
                tmpdir.cleanup()
            except Exception as e:
                _warn(f"failed to clean up {tmpdir.name}: {e}")


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
        chunks = "?"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                dur = f"{meta.get('duration_sec', 0):.1f}s"
                if meta.get("chunked"):
                    chunks = f"{meta.get('ok_chunks', '?')}/{len(meta.get('chunks', []))}"
                else:
                    chunks = "1/1"
            except Exception:
                pass
        print(f"{f.name:50s}  {dur:>10s}  {chunks:>7s}")
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
    p_trans.add_argument("--keep-chunks", action="store_true",
                         help="keep ffmpeg chunk PCM files (debug)")
    p_trans.add_argument("--chunk-sec", type=int, default=DEFAULT_CHUNK_SEC,
                         help=f"chunk size in seconds (default {DEFAULT_CHUNK_SEC})")
    p_trans.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                         help=f"parallel WS connections (default {DEFAULT_CONCURRENCY})")

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