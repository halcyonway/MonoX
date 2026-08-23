"""DebugServer HTTP 端点 + CORS。"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from core.debug_server import DebugServer, DebugServerConfig
from core.observability import JsonlTraceStore, TraceCollector


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
    """每个 test 一个 harness（不同 port）；start 在 async 上下文里起 server。

    默认 pre-create `default` session 的 collector，让测试代码可以直接操作。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.port = _free_port()
        self.collectors: dict[str, TraceCollector] = {}

        async def provider(session_key: str):
            if session_key not in self.collectors:
                store = JsonlTraceStore(tmp_path / session_key / "traces.jsonl")
                self.collectors[session_key] = TraceCollector(store, session_key)
            return self.collectors[session_key]._store  # type: ignore[attr-defined]

        self.h = DebugServer(
            DebugServerConfig(host="127.0.0.1", port=self.port),
            trace_provider=provider,
        )
        self._task: asyncio.Task | None = None

    def ensure_collector(self, tmp_path: Path, session_key: str = "default") -> TraceCollector:
        if session_key not in self.collectors:
            store = JsonlTraceStore(tmp_path / session_key / "traces.jsonl")
            self.collectors[session_key] = TraceCollector(store, session_key)
        return self.collectors[session_key]

    async def start(self, tmp_path: Path) -> None:
        # pre-create default so test code can use h.collectors["default"]
        self.ensure_collector(tmp_path, "default")
        self._task = asyncio.create_task(self.h.run_server())
        await self.wait_ready()

    async def wait_ready(self) -> None:
        for _ in range(100):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                return
            except OSError:
                await asyncio.sleep(0.02)
        raise RuntimeError("debug server did not start")

    async def stop(self) -> None:
        await self.h.stop()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()


class TestDebugServer:
    async def test_health(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            status, _, body = await _http_get("127.0.0.1", h.port, "/health")
            assert status == 200
            assert json.loads(body.decode()) == {"sessions": [], "debug": True}
        finally:
            await h.stop()

    async def test_recent_empty(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            status, _, body = await _http_get(
                "127.0.0.1", h.port, "/debug/runs/recent?session_key=default"
            )
            assert status == 200
            assert json.loads(body.decode()) == {"runs": []}
        finally:
            await h.stop()

    async def test_recent_returns_summary(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            c = h.collectors["default"]
            rid = await c.begin_run("hi")
            await c.end_run("done", status="ok")
            status, _, body = await _http_get(
                "127.0.0.1", h.port, "/debug/runs/recent?session_key=default&limit=10"
            )
            assert status == 200
            payload = json.loads(body.decode())
            assert len(payload["runs"]) == 1
            assert payload["runs"][0]["run_id"] == rid
            assert payload["runs"][0]["user_text"] == "hi"
            assert payload["runs"][0]["status"] == "ok"
        finally:
            await h.stop()

    async def test_get_run_full(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            c = h.collectors["default"]
            rid = await c.begin_run("hi")
            tid = await c.begin_turn(0)
            await c.record_llm_span(
                tid, model="m1",
                messages=[{"role": "user", "content": "hi"}],
                response_text="hello", reasoning_content=None,
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="stop", latency_ms=10,
            )
            await c.end_run("hello", status="ok")
            status, _, body = await _http_get(
                "127.0.0.1", h.port, f"/debug/runs/{rid}?session_key=default"
            )
            assert status == 200
            payload = json.loads(body.decode())
            assert payload["run_id"] == rid
            assert len(payload["turns"]) == 1
            assert payload["turns"][0]["spans"][0]["attributes"]["model"] == "m1"
        finally:
            await h.stop()

    async def test_get_run_missing_returns_404(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            status, _, body = await _http_get(
                "127.0.0.1", h.port, "/debug/runs/t_nope?session_key=default"
            )
            assert status == 404
            payload = json.loads(body.decode())
            assert "not found" in payload["error"]
        finally:
            await h.stop()

    async def test_missing_session_key_returns_400(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            status, _, body = await _http_get(
                "127.0.0.1", h.port, "/debug/runs/recent"
            )
            assert status == 400
            payload = json.loads(body.decode())
            assert "session_key" in payload["error"]
        finally:
            await h.stop()

    async def test_404_for_unknown_path(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            status, _, _ = await _http_get("127.0.0.1", h.port, "/foo")
            assert status == 404
        finally:
            await h.stop()

    async def test_debug_path_includes_cors(self, tmp_path):
        h = _Harness(tmp_path)
        await h.start(tmp_path)
        try:
            _, headers, _ = await _http_get(
                "127.0.0.1", h.port, "/debug/runs/recent?session_key=default"
            )
            assert headers.get("access-control-allow-origin") == "*"
        finally:
            await h.stop()