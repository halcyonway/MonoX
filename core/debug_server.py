"""DebugServer — MonoX 可观测性的 HTTP 入口（:8768，独立端口）。

跟 HealthServer 一样用 stdlib asyncio.start_server，避免 aiohttp 依赖。

路由：
- GET /health
    → {"sessions": [...]}            （同 :8767/health，方便 monoDesk 复用）
- GET /debug/runs/recent?session_key=X&limit=20
    → {"runs": [RunSummary, ...]}    （按 start_ts 倒序）
- GET /debug/runs/<run_id>?session_key=X
    → Run JSON（完整 turns + spans）
- 其他 → 404

MonoDesk dev 走 vite proxy 转 `/debug/*` → `http://127.0.0.1:8768`；
prod Tauri 模式下由于是 desktop app 直接 fetch localhost，没跨域问题。
简单 `Access-Control-Allow-Origin: *` 兜底（仅 /debug/*）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.observability import JsonlTraceStore, TraceStore

_log = logging.getLogger("monox.debug_server")

# 单 process 内一组 per-session JsonlTraceStore（按 session_key 懒加载并缓存）。
TraceProvider = Callable[[str], Awaitable[TraceStore]]


@dataclass(frozen=True)
class DebugServerConfig:
    host: str = "127.0.0.1"
    port: int = 8768


class DebugServer:
    def __init__(
        self,
        cfg: DebugServerConfig,
        *,
        trace_provider: TraceProvider,
    ) -> None:
        self._cfg = cfg
        self._trace_provider = trace_provider
        self._stop = asyncio.Event()
        self._server: asyncio.base_events.Server | None = None

    async def run_server(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._cfg.host, self._cfg.port
        )
        try:
            async with self._server:
                await self._stop.wait()
        finally:
            self._server = None

    async def stop(self) -> None:
        # run_server 自己会在 _stop 触发后通过 async with self._server 关闭；
        # 不在这里再 close，否则 wait_closed 会 hang（asyncio server 已被关闭的状态）。
        self._stop.set()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError, Exception):
            writer.close()
            return
        try:
            request_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        except Exception:
            await _write_404(writer)
            return
        parts = request_line.split()
        if len(parts) < 2:
            await _write_404(writer)
            return
        method, path_q = parts[0], parts[1]
        if method != "GET":
            await _write_404(writer)
            return

        split = urlsplit(path_q)
        path = split.path
        qs = parse_qs(split.query)

        cors = path.startswith("/debug/")

        if path == "/health":
            # debug server 自己也提供 /health，方便 MonoDesk 单一来源
            await _send_json(writer, 200, {"sessions": [], "debug": True}, extra_cors=cors)
            return
        if path == "/debug/runs/recent":
            session_key = _first(qs, "session_key")
            if not session_key:
                await _send_json(writer, 400, {"error": "missing session_key"}, extra_cors=cors)
                return
            try:
                limit = int(_first(qs, "limit") or "20")
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 200))
            store = await self._trace_provider(session_key)
            runs = await store.list_runs(session_key, limit=limit)
            payload = {
                "runs": [
                    {
                        "run_id": r.run_id,
                        "session_key": r.session_key,
                        "user_text": r.user_text,
                        "start_ts": r.start_ts,
                        "end_ts": r.end_ts,
                        "status": r.status,
                        "turn_count": r.turn_count,
                    }
                    for r in runs
                ]
            }
            await _send_json(writer, 200, payload, extra_cors=cors)
            return
        if path.startswith("/debug/runs/"):
            run_id = path[len("/debug/runs/"):]
            if not run_id or run_id == "recent":
                await _write_404(writer, extra_cors=cors)
                return
            session_key = _first(qs, "session_key")
            if not session_key:
                await _send_json(writer, 400, {"error": "missing session_key"}, extra_cors=cors)
                return
            store = await self._trace_provider(session_key)
            run = await store.get_run(session_key, run_id)
            if run is None:
                await _send_json(writer, 404, {"error": "run not found", "run_id": run_id}, extra_cors=cors)
                return
            await _send_json(writer, 200, run.to_dict(), extra_cors=cors)
            return
        await _write_404(writer, extra_cors=cors)


def _first(qs: dict[str, list[str]], key: str) -> str:
    vals = qs.get(key)
    if not vals:
        return ""
    return vals[0]


async def _send_json(
    writer: asyncio.StreamWriter,
    status: int,
    body_obj: dict[str, Any],
    *,
    extra_cors: bool,
) -> None:
    body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
    status_text = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {status_text}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if extra_cors:
        headers.insert(2, "Access-Control-Allow-Origin: *")
    header_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
    writer.write(header_bytes + body)
    try:
        await writer.drain()
    except Exception:
        pass
    # 关键：handler 里没有显式 close writer，否则客户端读不到 EOF。
    try:
        writer.close()
    except Exception:
        pass


async def _write_404(
    writer: asyncio.StreamWriter, *, extra_cors: bool = False
) -> None:
    await _send_json(writer, 404, {"error": "not found"}, extra_cors=extra_cors)


# ----------------------------------------------------------------------
# 默认 TraceProvider：在给定的 traces_root 下，per-session 一个 JsonlTraceStore。
# 用于 run.py 把 DebugServer 接起来；测试可注入别的 provider。
# ----------------------------------------------------------------------

class FsTraceProvider:
    """Per-session JsonlTraceStore 的懒加载 + 缓存。"""

    def __init__(self, traces_root) -> None:
        self._root = Path(traces_root)
        self._cache: dict[str, TraceStore] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, session_key: str) -> TraceStore:
        cached = self._cache.get(session_key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(session_key)
            if cached is not None:
                return cached
            store: TraceStore = JsonlTraceStore(
                self._root / session_key / "traces.jsonl"
            )
            self._cache[session_key] = store
            return store