"""asr.py — ASR extension CLI: 调火山引擎语音识别 + 持久化结果到本地。

通过 `exec_cli mono_asr <subcmd> [args]` 调用，由 CLI server 分发。

工作流：
  1. 接收音频文件（mp3/m4a/wav/opus/ogg 等）
  2. ffprobe 时长 → 若 > 10 分钟，ffmpeg 切成 10min PCM 块
  3. 每块独立通过 WebSocket 二进制协议调火山引擎 doubao-seed-asr-2.0
     （asyncio.gather + Semaphore 限并发 3，单块 3 次指数退避重试）
  4. 结果按块顺序拼接，失败块用 [chunk N failed: <err>] 占位
  5. 持久化到 <workspace>/asr/<ts>_<原文件名>.asr.txt
  6. 元信息（每块状态、估算费用、reqid）存到同目录 .asr.meta.json
  7. 原始 API 响应存到 .monox/traces/asr/<ts>_<reqid>_chunkN.json（用于复盘）

环境变量：
  HUOSHAN_API_KEY    Agent Plan 专属 API key（X-Api-Key header）

用法（agent 调用）：
  exec_cli mono_asr transcribe <audio_path> [--language en-US] [--keep-chunks]
                                                [--chunk-sec 600] [--concurrency 3]
  exec_cli mono_asr list                          # 列所有已转写
  exec_cli mono_asr show <basename>               # 按 basename 子串查最新

workspace 默认 = .monox/workspace/（MonoX 约定），可被 ASR_WORKSPACE_DIR 覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import json
import os
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
    print("ERROR: 'websockets' library required. Install: pip install websockets",
          file=sys.stderr)
    sys.exit(1)

from extensions.cli.inner import common_util as cu


# ---- 路径常量 ----

SKILL_DIR = Path(__file__).resolve().parent
_DEFAULT_MONOX_ROOT = SKILL_DIR.parent.parent.parent   # extensions/cli/asr → repo root

DEFAULT_WORKSPACE = Path(os.environ.get(
    "MONOX_WORKSPACE", _DEFAULT_MONOX_ROOT / ".monox" / "workspace"))
OUTPUT_DIR = Path(os.environ.get("ASR_WORKSPACE_DIR", DEFAULT_WORKSPACE / "asr"))
TRACES_DIR = Path(os.environ.get(
    "ASR_TRACES_DIR", _DEFAULT_MONOX_ROOT / ".monox" / "traces" / "asr"))


# ---- 协议常量（Volcengine 豆包 ASR V3 大模型二进制协议） ----

WSS_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
RESOURCE_ID = "volc.seedasr.sauc.duration"

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

PRICE_PER_HOUR_CNY = 0.8
PCM_CHUNK_BYTES = 32000


# ---- 长音频切分 + 并发参数 ----

DEFAULT_CHUNK_SEC = 600
DEFAULT_CONCURRENCY = 3
MAX_RETRY = 3
RETRY_BACKOFF_SEC = (2, 4, 8)
CHUNK_FAIL_MARKER = "[chunk {idx} failed: {err}]"


# ---- 工具 ----

def _api_key() -> Optional[str]:
    return os.environ.get("HUOSHAN_API_KEY")


def _info(msg: str) -> None:
    """Progress → stderr (在 server 模式被忽略；调试时从 server log 看)。"""
    print(msg, file=sys.stderr)


def _ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TRACES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_basename(path: Path) -> str:
    stem = path.stem
    safe = "".join(c for c in stem if c.isalnum() or c in "-_ ")
    safe = safe.strip().replace(" ", "_")[:60]
    return safe or "audio"


def _pcm_duration_seconds(pcm_bytes: int, rate: int = 16000, bits: int = 16, channel: int = 1) -> float:
    return pcm_bytes / (rate * bits / 8 * channel)


def _convert_to_pcm(src: Path, dst: Path) -> Optional[str]:
    """用 ffmpeg 转 PCM mono 16kHz 16-bit，返回 None 成功 / error message 失败。"""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
           "-f", "s16le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return "ffmpeg not found in PATH; install with: brew install ffmpeg"
    except subprocess.TimeoutExpired:
        return f"ffmpeg timeout after 300s converting {src}"
    if r.returncode != 0:
        return f"ffmpeg failed: {r.stderr[-500:]}"
    return None


def _probe_duration(src: Path) -> float:
    """ffprobe 探测音频时长（秒）。失败返回 0（按 0 处理 = 不切分）。"""
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(src)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 0.0
    except subprocess.TimeoutExpired:
        return 0.0
    if r.returncode != 0 or not r.stdout.strip():
        return 0.0
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _split_audio(src: Path, chunk_dir: Path, chunk_seconds: int) -> Optional[list[Path]]:
    """切分音频到 PCM 块。失败返回 None + error 由调用方处理。"""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(src)
    if duration <= 0:
        return None  # caller 检测到 0 时报错
    n_chunks = int(duration // chunk_seconds) + (1 if duration % chunk_seconds > 0 else 0)
    if n_chunks < 1:
        n_chunks = 1
    chunks: list[Path] = []
    for idx in range(n_chunks):
        start = idx * chunk_seconds
        chunk_path = chunk_dir / f"chunk_{idx:03d}.pcm"
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src),
               "-t", str(chunk_seconds), "-ac", "1", "-ar", "16000",
               "-f", "s16le", str(chunk_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            return None  # 同上 caller 处理
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        if chunk_path.stat().st_size == 0:
            chunk_path.unlink(missing_ok=True)
            break
        chunks.append(chunk_path)
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

async def _transcribe_async(pcm_bytes: bytes, language: str, req_id: str,
                             trace_path: Optional[Path]) -> str:
    """通过 WS 调豆包 ASR，返回完整转写文本。失败抛异常由上层重试。"""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("HUOSHAN_API_KEY not set")

    extra_headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    _info(f"  WS {WSS_ENDPOINT} req_id={req_id[:8]}…")
    _info(f"  audio: {len(pcm_bytes)} bytes "
          f"({_pcm_duration_seconds(len(pcm_bytes)):.1f}s)")

    frames_sent = []
    frames_recv = []

    async with websockets.connect(
        WSS_ENDPOINT, additional_headers=extra_headers, max_size=20 * 1024 * 1024,
        ping_interval=None, ping_timeout=None,
    ) as ws:
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

        n_chunks = (len(pcm_bytes) + PCM_CHUNK_BYTES - 1) // PCM_CHUNK_BYTES
        for idx in range(n_chunks):
            chunk = pcm_bytes[idx * PCM_CHUNK_BYTES:(idx + 1) * PCM_CHUNK_BYTES]
            is_last = (idx == n_chunks - 1)
            seq = idx + 2
            frame = _build_audio_request(chunk, sequence=seq, final=is_last, compress=False)
            frames_sent.append(("audio", len(frame)))
            await ws.send(frame)

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
                        full_text = text
                if r.get("is_last"):
                    break
        except asyncio.TimeoutError:
            _info(f"  req_id={req_id[:8]}… timeout waiting for final response")

    full_text = full_text.strip()

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
        trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    return full_text


async def _transcribe_chunk_with_retry(
    chunk_path: Path, chunk_idx: int, language: str,
    sem: asyncio.Semaphore, trace_dir: Path,
) -> tuple[int, str, dict]:
    """单块转写：受 sem 限并发，最多重试 MAX_RETRY 次。返回 (idx, text, meta)。"""
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
                "chunk_idx": chunk_idx, "status": "ok",
                "req_id": req_id, "duration_sec": duration_sec,
                "attempt": attempt, "trace_path": str(trace_path),
            }
        except (websockets.exceptions.WebSocketException, ConnectionError,
                asyncio.TimeoutError, OSError, RuntimeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRY:
                backoff = RETRY_BACKOFF_SEC[attempt - 1]
                _info(f"  chunk {chunk_idx} attempt {attempt} failed ({last_err}); "
                      f"retry in {backoff}s")
                await asyncio.sleep(backoff)

    _info(f"  chunk {chunk_idx} FAILED after {MAX_RETRY} attempts: {last_err}")
    return chunk_idx, "", {
        "chunk_idx": chunk_idx, "status": "error",
        "error": last_err, "duration_sec": duration_sec, "attempts": MAX_RETRY,
    }


async def _transcribe_chunks_parallel(
    chunks: list[Path], language: str, concurrency: int, trace_dir: Path,
) -> list[tuple[int, str, dict]]:
    sem = asyncio.Semaphore(concurrency)
    coros = [_transcribe_chunk_with_retry(p, idx, language, sem, trace_dir)
             for idx, p in enumerate(chunks)]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[tuple[int, str, dict]] = []
    for idx, r in enumerate(results):
        if isinstance(r, Exception):
            out.append((idx, "", {
                "chunk_idx": idx, "status": "error",
                "error": f"{type(r).__name__}: {r}",
                "duration_sec": 0.0, "attempts": 0,
            }))
        else:
            out.append(r)
    out.sort(key=lambda x: x[0])
    return out


def _assemble_text(results: list[tuple[int, str, dict]]) -> str:
    parts: list[str] = []
    for idx, text, meta in results:
        if meta["status"] == "ok":
            if text:
                parts.append(text)
        else:
            err = meta.get("error", "unknown")
            parts.append(CHUNK_FAIL_MARKER.format(idx=idx, err=err))
    return "\n".join(parts).strip()


# ---- 子命令 ----

def cmd_transcribe(args: argparse.Namespace) -> dict:
    """`transcribe <audio>` → 转写音频并持久化结果。"""
    _ensure_dirs()
    api_key = _api_key()
    if not api_key:
        return cu.err("HUOSHAN_API_KEY not set",
                      hint="export HUOSHAN_API_KEY=ark-<uuid> before invoking mono_asr")

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        return cu.err(f"audio file not found: {audio_path}")

    chunk_sec = int(args.chunk_sec)
    concurrency = int(args.concurrency)

    duration_sec = _probe_duration(audio_path)
    if duration_sec <= 0:
        use_chunked = False
    else:
        use_chunked = duration_sec > chunk_sec

    _info(f"audio: {audio_path.name} ({audio_path.stat().st_size} bytes)")
    if duration_sec > 0:
        _info(f"duration: {duration_sec:.1f}s, "
              f"{'chunked' if use_chunked else 'single'}")

    tmpdir: Optional[tempfile.TemporaryDirectory] = None
    try:
        if use_chunked:
            tmpdir = tempfile.TemporaryDirectory(prefix="asr_chunks_")
            chunk_dir = Path(tmpdir.name)
            chunks = _split_audio(audio_path, chunk_dir, chunk_sec)
            if chunks is None:
                return cu.err(f"failed to split audio: probe_duration or ffmpeg failed",
                              audio=str(audio_path))
            _info(f"split into {len(chunks)} chunks of ~{chunk_sec}s")
        else:
            tmpdir = tempfile.TemporaryDirectory(prefix="asr_pcm_")
            chunk_dir = Path(tmpdir.name)
            tmp_pcm = chunk_dir / "single.pcm"
            err = _convert_to_pcm(audio_path, tmp_pcm)
            if err is not None:
                return cu.err(err, audio=str(audio_path))
            chunks = [tmp_pcm]

        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        req_id_master = str(uuid.uuid4())
        results = asyncio.run(_transcribe_chunks_parallel(
            chunks, args.language, concurrency, TRACES_DIR,
        ))

        full_text = _assemble_text(results)
        actual_total_duration = sum(m["duration_sec"] for _, _, m in results)
        if actual_total_duration == 0 and duration_sec > 0:
            actual_total_duration = duration_sec
        duration_hr = actual_total_duration / 3600.0
        est_cost_cny = duration_hr * PRICE_PER_HOUR_CNY

        ok_chunks = sum(1 for _, _, m in results if m["status"] == "ok")
        err_chunks = len(results) - ok_chunks
        if not full_text:
            return cu.err("API returned empty text for all chunks "
                          "(audio may be silent or format mismatch)")

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
                {"idx": idx, "status": m["status"],
                 "duration_sec": m["duration_sec"],
                 "req_id": m.get("req_id"),
                 "attempt": m.get("attempt") or m.get("attempts"),
                 "error": m.get("error"),
                 "trace_path": m.get("trace_path")}
                for idx, _, m in results
            ],
            "ok_chunks": ok_chunks,
            "err_chunks": err_chunks,
            "char_count": len(full_text),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        return cu.ok({
            "txt_path": str(txt_path),
            "meta_path": str(meta_path),
            "trace_paths": [m.get("trace_path") for _, _, m in results if m.get("trace_path")],
            "ok_chunks": ok_chunks,
            "err_chunks": err_chunks,
            "duration_sec": actual_total_duration,
            "estimated_cost_cny": est_cost_cny,
            "language": args.language,
            "req_id_master": req_id_master,
            "text": full_text,
        })
    finally:
        if tmpdir is not None and not args.keep_chunks:
            try:
                tmpdir.cleanup()
            except Exception as e:
                _info(f"failed to clean up {tmpdir.name}: {e}")


def cmd_list(_args: argparse.Namespace) -> dict:
    """`list` → 所有已转写结果摘要。"""
    _ensure_dirs()
    files = sorted(OUTPUT_DIR.glob("*.asr.txt"), reverse=True)
    entries = []
    for f in files:
        meta_path = f.with_suffix(".meta.json")
        entry = {"txt_path": str(f), "name": f.name,
                 "duration_sec": None, "ok_chunks": None, "err_chunks": None}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry["duration_sec"] = meta.get("duration_sec")
                entry["ok_chunks"] = meta.get("ok_chunks")
                entry["err_chunks"] = meta.get("err_chunks")
            except Exception:
                pass
        entries.append(entry)
    return cu.ok({"count": len(entries), "entries": entries})


def cmd_show(args: argparse.Namespace) -> dict:
    """`show <basename>` → 查最近一份匹配的 .asr.txt。"""
    _ensure_dirs()
    matches = sorted(OUTPUT_DIR.glob(f"*{args.basename}*.asr.txt"), reverse=True)
    if not matches:
        return cu.err(f"no ASR result for basename: {args.basename}",
                      output_dir=str(OUTPUT_DIR))
    selected = matches[0]
    meta_path = selected.with_suffix(".meta.json")
    meta = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    body = selected.read_text(encoding="utf-8")
    return cu.ok({
        "name": selected.name,
        "txt_path": str(selected),
        "meta_path": str(meta_path) if meta_path.exists() else None,
        "meta": meta,
        "multiple_matches": len(matches) > 1,
        "body": body,
    })


# ---- argparse + handler entry ----

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mono_asr",
        description="ASR extension CLI: Volcengine Doubao ASR + persist to local",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_trans = sub.add_parser("transcribe", help="transcribe an audio file")
    p_trans.add_argument("audio", help="input audio path (mp3/m4a/wav/opus/ogg etc)")
    p_trans.add_argument("--language", default="zh-CN")
    p_trans.add_argument("--keep-chunks", action="store_true")
    p_trans.add_argument("--chunk-sec", type=int, default=DEFAULT_CHUNK_SEC)
    p_trans.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

    sub.add_parser("list", help="list all ASR results")
    p_show = sub.add_parser("show", help="show ASR text by basename")
    p_show.add_argument("basename", help="substring of original filename")

    return ap


def main(args: list[str]) -> dict:
    """Registry handler entry point. Receives argv tail, returns envelope."""
    parser = _build_parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit:
        return cu.err("invalid arguments",
                      hint="pass --help to see usage", prog="mono_asr")

    handlers = {"transcribe": cmd_transcribe, "list": cmd_list, "show": cmd_show}
    return handlers[parsed.cmd](parsed)


if __name__ == "__main__":
    """Standalone usage: `python -m extensions.cli.asr.asr <subcmd> [args]`."""
    sys.stdout.write(json.dumps(main(sys.argv[1:]), ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
