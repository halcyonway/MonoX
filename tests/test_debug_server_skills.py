"""core/debug_server.py 的 skill 路由单测。

通过直接调 _handle_skills() + fake writer 测试路由逻辑（不拉真 server）。
端到端跑通靠 run.py 集成；这里只验证 method/path/参数 → response 映射。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.debug_server import DebugServer, DebugServerConfig
from core.skill_service import SkillService


@dataclass
class _FakeWriter:
    """捕获 HTTP response（status + body）。"""
    status: int = 0
    status_text: str = ""
    body: bytes = b""
    headers: dict[str, str] | None = None

    def write(self, data: bytes) -> None:
        # data 是 "HTTP/1.1 200 OK\r\n...\r\n\r\n<body>"
        text = data.decode("ascii", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        if lines:
            m = re.match(r"HTTP/1\.[01] (\d+)(?: (.*))?$", lines[0])
            if m:
                self.status = int(m.group(1))
                self.status_text = m.group(2) or ""
        self.body = body.encode("utf-8")

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


def _build(skills_root: Path) -> tuple[DebugServer, SkillService]:
    """构造带 SkillService 的 DebugServer（不依赖 TraceProvider）。"""
    svc = SkillService(skills_root)
    srv = DebugServer(
        DebugServerConfig(host="127.0.0.1", port=0),
        trace_provider=_noop_trace_provider,
        skill_service=svc,
    )
    return srv, svc


async def _noop_trace_provider(_sk: str):  # pragma: no cover - unused in skill tests
    raise NotImplementedError


async def _get(srv: DebugServer, path: str) -> _FakeWriter:
    w = _FakeWriter()
    await srv._handle_skills("GET", path, b"", "", w, cors=path.startswith("/debug/"))
    return w


async def _put(srv: DebugServer, path: str, body: bytes, ct: str = "text/markdown") -> _FakeWriter:
    w = _FakeWriter()
    await srv._handle_skills("PUT", path, body, ct, w, cors=path.startswith("/debug/"))
    return w


async def _delete(srv: DebugServer, path: str) -> _FakeWriter:
    w = _FakeWriter()
    await srv._handle_skills("DELETE", path, b"", "", w, cors=path.startswith("/debug/"))
    return w


async def _post_upload(srv: DebugServer, body: bytes, ct: str = "application/zip") -> _FakeWriter:
    w = _FakeWriter()
    await srv._handle_skills("POST", "/debug/skills/upload", body, ct, w, cors=True)
    return w


def _json(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


# ---------- list ----------

class TestList:
    async def test_empty(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _get(srv, "/debug/skills/list")
        assert w.status == 200
        assert _json(w.body) == {"skills": []}

    async def test_returns_metadata(self, tmp_path: Path) -> None:
        srv, svc = _build(tmp_path)
        svc.write("foo", "# Foo\n\nbody\n")
        svc.write("rare", "---\ntier: 2\n---\n# Rare\n")
        w = await _get(srv, "/debug/skills/list")
        assert w.status == 200
        data = _json(w.body)
        assert {s["name"] for s in data["skills"]} == {"foo", "rare"}
        foo = next(s for s in data["skills"] if s["name"] == "foo")
        assert foo["tier"] == 1
        assert foo["description"] == "Foo"
        assert foo["path"].endswith("/foo")

    async def test_rejects_post(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = _FakeWriter()
        await srv._handle_skills("POST", "/debug/skills/list", b"", "", w, cors=True)
        assert w.status == 405


# ---------- get one ----------

class TestGetOne:
    async def test_returns_body(self, tmp_path: Path) -> None:
        srv, svc = _build(tmp_path)
        svc.write("foo", "# Foo\n\nbody\n")
        w = await _get(srv, "/debug/skills/foo")
        assert w.status == 200
        data = _json(w.body)
        assert data["name"] == "foo"
        assert data["body"] == "# Foo\n\nbody\n"
        assert data["tier"] == 1
        assert data["description"] == "Foo"

    async def test_missing_returns_404(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _get(srv, "/debug/skills/missing")
        assert w.status == 404
        assert "not found" in _json(w.body)["error"]


# ---------- put (write) ----------

class TestPut:
    async def test_creates_new_skill(self, tmp_path: Path) -> None:
        srv, svc = _build(tmp_path)
        w = await _put(srv, "/debug/skills/new", b"# New\n")
        assert w.status == 200
        assert _json(w.body) == {"ok": True, "name": "new"}
        assert (tmp_path / "new" / "SKILL.md").read_text() == "# New\n"

    async def test_overwrites_existing(self, tmp_path: Path) -> None:
        srv, svc = _build(tmp_path)
        svc.write("a", "v1")
        w = await _put(srv, "/debug/skills/a", b"v2")
        assert w.status == 200
        assert (tmp_path / "a" / "SKILL.md").read_text() == "v2"

    async def test_rejects_invalid_name(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _put(srv, "/debug/skills/../escape", b"x")
        assert w.status == 400

    async def test_rejects_non_utf8(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _put(srv, "/debug/skills/foo", b"\xff\xfe\x00bad")
        assert w.status == 400
        assert "utf-8" in _json(w.body)["error"]

    async def test_rejects_name_with_slash(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = _FakeWriter()
        await srv._handle_skills(
            "PUT", "/debug/skills/foo/bar", b"x", "", w, cors=True,
        )
        assert w.status == 400


# ---------- delete ----------

class TestDelete:
    async def test_removes_skill(self, tmp_path: Path) -> None:
        srv, svc = _build(tmp_path)
        svc.write("a", "x")
        w = await _delete(srv, "/debug/skills/a")
        assert w.status == 200
        assert _json(w.body) == {"ok": True, "name": "a"}
        assert not (tmp_path / "a").exists()

    async def test_missing_returns_404(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _delete(srv, "/debug/skills/nope")
        assert w.status == 404

    async def test_invalid_name_returns_400(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _delete(srv, "/debug/skills/../escape")
        assert w.status == 400


# ---------- upload ----------

def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


class TestUpload:
    async def test_single_skill(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        body = _zip({"foo/SKILL.md": "# Foo\n"})
        w = await _post_upload(srv, body)
        assert w.status == 200
        assert _json(w.body) == {"added": ["foo"]}
        assert (tmp_path / "foo" / "SKILL.md").read_text() == "# Foo\n"

    async def test_multi_skill(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        body = _zip({
            "a/SKILL.md": "# A",
            "b/SKILL.md": "# B",
        })
        w = await _post_upload(srv, body)
        assert w.status == 200
        assert _json(w.body) == {"added": ["a", "b"]}

    async def test_not_a_zip_returns_400(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _post_upload(srv, b"definitely not a zip")
        assert w.status == 400
        assert "invalid zip" in _json(w.body)["error"]

    async def test_empty_zip_returns_400(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = await _post_upload(srv, b"")
        assert w.status == 400

    async def test_bad_structure_returns_400(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        # 顶层直接放 SKILL.md（不允许：必须在子目录里）
        body = _zip({"SKILL.md": "# root"})
        w = await _post_upload(srv, body)
        assert w.status == 400

    async def test_rejects_get(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = _FakeWriter()
        await srv._handle_skills(
            "GET", "/debug/skills/upload", b"", "", w, cors=True,
        )
        assert w.status == 405


# ---------- no skill_service configured ----------

class TestNoSkillServiceConfigured:
    async def test_skill_path_returns_404(self, tmp_path: Path) -> None:
        """DebugServer 构造时不传 skill_service → /debug/skills/* 都走 404。"""
        srv = DebugServer(
            DebugServerConfig(host="127.0.0.1", port=0),
            trace_provider=_noop_trace_provider,
            # 没有 skill_service
        )
        # 直接用 _handle 会 fallback 到 404（_handle_skills 不会被调）
        # 这里简单验证构造 OK；端到端由 _handle 测试覆盖
        assert srv._skill_service is None


# ---------- invalid paths ----------

class TestPathValidation:
    async def test_empty_name(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = _FakeWriter()
        await srv._handle_skills(
            "GET", "/debug/skills/", b"", "", w, cors=True,
        )
        assert w.status == 400

    async def test_nested_path(self, tmp_path: Path) -> None:
        srv, _ = _build(tmp_path)
        w = _FakeWriter()
        await srv._handle_skills(
            "GET", "/debug/skills/foo/bar", b"", "", w, cors=True,
        )
        assert w.status == 400
