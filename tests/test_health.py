"""core/health_server.py 单测。

stdlib HTTP via asyncio.start_server，GET /health 返回 JSON {sessions: [...]}。
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest

from core.health_server import HealthServer, HealthServerConfig


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http_get(host: str, port: int, path: str, timeout: float = 2.0) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.split(b"\r\n")
    parts = status_line.decode().split(" ", 2)
    status = int(parts[1])
    headers: dict[str, str] = {}
    for line in header_lines:
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


class _Harness:
    def __init__(self, provider):
        self.port = _free_port()
        self.h = HealthServer(HealthServerConfig(host="127.0.0.1", port=self.port), session_provider=provider)
        self._task = asyncio.create_task(self.h.run())

    async def wait_ready(self):
        for _ in range(50):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                try: await w.wait_closed()
                except Exception: pass
                return
            except OSError:
                await asyncio.sleep(0.02)
        raise RuntimeError("health server did not start")

    async def stop(self):
        await self.h.stop()
        try:
            await asyncio.wait_for(self._task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self._task.cancel()


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_get_health_returns_active_sessions(self):
        h = _Harness(lambda: ["default", "chat-42"])
        await h.wait_ready()
        try:
            status, headers, body = await _http_get("127.0.0.1", h.port, "/health")
            assert status == 200
            assert headers.get("content-type", "").startswith("application/json")
            assert json.loads(body.decode()) == {"sessions": ["default", "chat-42"]}
        finally:
            await h.stop()

    @pytest.mark.asyncio
    async def test_other_paths_return_404(self):
        h = _Harness(lambda: [])
        await h.wait_ready()
        try:
            status, _, body = await _http_get("127.0.0.1", h.port, "/")
            assert status == 404
            status, _, _ = await _http_get("127.0.0.1", h.port, "/whatever")
            assert status == 404
        finally:
            await h.stop()

    @pytest.mark.asyncio
    async def test_empty_sessions_returns_empty_list(self):
        h = _Harness(lambda: [])
        await h.wait_ready()
        try:
            status, _, body = await _http_get("127.0.0.1", h.port, "/health")
            assert status == 200
            assert json.loads(body.decode()) == {"sessions": []}
        finally:
            await h.stop()

    @pytest.mark.asyncio
    async def test_reflects_provider_changes(self):
        cur = ["a"]
        h = _Harness(lambda: list(cur))
        await h.wait_ready()
        try:
            _, _, body = await _http_get("127.0.0.1", h.port, "/health")
            assert json.loads(body.decode()) == {"sessions": ["a"]}

            cur.append("b")
            _, _, body = await _http_get("127.0.0.1", h.port, "/health")
            assert json.loads(body.decode()) == {"sessions": ["a", "b"]}
        finally:
            await h.stop()
