"""HTTP /health 端点（stdlib only）。

Runtime 进程在 `:8767` 暴露一个最简 HTTP endpoint：
- `GET /health` → `200 OK` + JSON `{"sessions": [...]}`
- 其他路径 → `404 Not Found`

不需要 aiohttp / starlette 等依赖——`asyncio.start_server` + 手写 HTTP/1.1
request line + Content-Length response。够用且零依赖。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_log = logging.getLogger("monox.health_server")

SessionProvider = Callable[[], list[str]]


@dataclass(frozen=True)
class HealthServerConfig:
    host: str = "127.0.0.1"
    port: int = 8767


class HealthServer:
    def __init__(
        self,
        cfg: HealthServerConfig,
        *,
        session_provider: SessionProvider,
    ) -> None:
        self._cfg = cfg
        self._session_provider = session_provider
        self._stop = asyncio.Event()
        self._server: asyncio.base_events.Server | None = None

    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._cfg.host, self._cfg.port
        )
        try:
            async with self._server:
                await self._stop.wait()
        finally:
            self._server = None

    async def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # 读 request line + headers（最多 8KB 防止恶意客户端耗内存）
            raw = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError, Exception):
            writer.close()
            return

        try:
            request_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        except Exception:
            await self._write_404(writer)
            return

        parts = request_line.split()
        if len(parts) < 2:
            await self._write_404(writer)
            return
        method, path = parts[0], parts[1]

        if method == "GET" and path == "/health":
            sessions = self._session_provider()
            body = json.dumps({"sessions": sessions}, ensure_ascii=False).encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            writer.write(header + body)
            await writer.drain()
        else:
            await self._write_404(writer)
        writer.close()

    async def _write_404(self, writer: asyncio.StreamWriter) -> None:
        body = b'{"error":"not found"}'
        header = (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(header + body)
        try:
            await writer.drain()
        except Exception:
            pass