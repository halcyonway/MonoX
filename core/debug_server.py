"""DebugServer — MonoX 可观测性 + skill 管理的 HTTP 入口（:8768，独立端口）。

跟 HealthServer 一样用 stdlib asyncio.start_server，避免 aiohttp 依赖。

路由：
- GET  /health
    → {"sessions": [...]}            （同 :8767/health，方便 monoDesk 复用）
- GET  /debug/runs/recent?session_key=X&limit=20
    → {"runs": [RunSummary, ...]}    （按 start_ts 倒序）
- GET  /debug/runs/<run_id>?session_key=X
    → Run JSON（完整 turns + spans）
- GET  /debug/skills/list
    → {"skills": [SkillAbstract, ...]}  （name / description / tier / path）
- GET  /debug/skills/<name>
    → {"name", "description", "tier", "path", "body"}   （完整 SKILL.md）
- PUT  /debug/skills/<name>          （body = 完整 markdown 文本）
    → {"ok": true, "name": "..."}
- DELETE /debug/skills/<name>
    → {"ok": true, "name": "..."}
- POST /debug/skills/upload          （body = zip 字节）
    → {"added": ["foo", "bar"]}
- POST /debug/attachments/upload     （body = 原始文件字节，Content-Type 决定 mime）
    → {"url": "http://127.0.0.1:8768/debug/attachments/<uuid>.png", ...}
- GET  /debug/attachments/<file>    返回上传的文件字节（绝对路径用 HTTP 喂回前端，
                                     multimodalunderstand tool 也能直接走这条 URL）
- OPTIONS /debug/*                  CORS preflight：浏览器 POST 带非 simple Content-Type
                                     (e.g. image/png) 会先 OPTIONS，必须 204 + 头
                                     否则浏览器拦掉真正的 POST → 上传静默失败。
- 其他 → 404

URL 设计：upload 返回的 `url` 是个**绝对 HTTP URL**，不是本地文件路径。
原因：(1) 前端 `<img src=...>` 在浏览器里跨 origin 加载 file:// 直接被 CORS 拦；
       (2) MiniMax vision API 是云服务，拿不到 localhost 本地文件。
       HTTP URL 三方都能用同一份（前端渲染 / multimodalunderstand 走 fetch → base64）。

MonoDesk dev 走 vite proxy 转 `/debug/*` → `http://127.0.0.1:8768`；
prod Tauri 模式下由于是 desktop app 直接 fetch localhost，没跨域问题。
简单 `Access-Control-Allow-Origin: *` 兜底（仅 /debug/*）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.observability import JsonlTraceStore, TraceStore
from core.skill_service import SkillService
from core.skill_upload import SkillUploadError, extract_skill_zip

_log = logging.getLogger("monox.debug_server")

# 单 process 内一组 per-session JsonlTraceStore（按 session_key 懒加载并缓存）。
TraceProvider = Callable[[str], Awaitable[TraceStore]]


@dataclass(frozen=True)
class DebugServerConfig:
    host: str = "127.0.0.1"
    port: int = 8768


def _sanitize_filename(raw: str) -> str:
    """清洗 user-supplied filename：剥路径分隔符 + 不可打印字符，保留原始语义（中文/emoji/空格）。

    disk 文件名直接用 sanitize 后的原名（+ ext）—— 不加 uuid、不重命名，path 就是文件本身。
    """
    if not raw:
        return ""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if not base or base in (".", ".."):
        return ""
    safe = base.replace("/", "_").replace("\\", "_")
    safe = "".join(c for c in safe if c.isprintable() and c != "\x00")
    if not safe or safe in (".", ".."):
        return ""
    if len(safe) > 200:
        safe = safe[:200]
    return safe


class DebugServer:
    def __init__(
        self,
        cfg: DebugServerConfig,
        *,
        trace_provider: TraceProvider,
        skill_service: SkillService | None = None,
        attachments_root: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._trace_provider = trace_provider
        self._skill_service = skill_service
        self._attachments_root = attachments_root
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

        # 解析 header 块（拆 \r\n），拿 request line + Content-Length + Content-Type
        try:
            header_block = raw.split(b"\r\n\r\n", 1)[0].decode("ascii", errors="replace")
        except Exception:
            await _write_404(writer)
            return
        header_lines = header_block.split("\r\n")
        if not header_lines:
            await _write_404(writer)
            return
        request_line = header_lines[0]
        parts = request_line.split()
        if len(parts) < 2:
            await _write_404(writer)
            return
        method, path_q = parts[0], parts[1]

        # CORS preflight：浏览器 POST 带非 simple Content-Type（e.g. image/png）会先
        # 发 OPTIONS 探一下；不返回 204 + CORS 头浏览器就拦掉真正的 POST，
        # 上传请求就静默 404。必须放在 method 白名单检查之前。
        if method == "OPTIONS":
            path_pre = urlsplit(path_q).path
            if path_pre.startswith("/debug/"):
                await _write_cors_preflight(writer)
            else:
                await _write_404(writer)
            return

        if method not in ("GET", "PUT", "POST", "DELETE"):
            await _write_404(writer)
            return

        # parse headers
        content_length = 0
        content_type = ""
        x_filename = ""
        for line in header_lines[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            kl = k.strip().lower()
            vl = v.strip()
            if kl == "content-length":
                try:
                    content_length = int(vl)
                except ValueError:
                    content_length = 0
            elif kl == "content-type":
                content_type = vl
            elif kl == "x-filename":
                # 浏览器 raw body 上传没 multipart boundary，server 解不了
                # Content-Disposition，所以走自定义 header 带原名。
                x_filename = vl

        # 读 body（如果声明了长度）
        body_bytes = b""
        if content_length > 0:
            try:
                body_bytes = await reader.readexactly(content_length)
            except (asyncio.IncompleteReadError, Exception):
                cors_pre = path_q.startswith("/debug/")
                await _send_json(
                    writer, 400, {"error": "incomplete body"}, extra_cors=cors_pre,
                )
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
        # ---- skill 管理路由（需要注入 SkillService）----
        if self._skill_service is not None and path.startswith("/debug/skills"):
            await self._handle_skills(method, path, body_bytes, content_type, writer, cors)
            return
        # ---- attachment 上传路由（需要注入 attachments_root）----
        if path == "/debug/attachments/upload" and method == "POST":
            await self._handle_attachment_upload(body_bytes, content_type, writer, cors, x_filename)
            return
        # ---- attachment serve 路由：把上传过的文件字节喂回前端 / multimodal tool ----
        if path.startswith("/debug/attachments/") and method == "GET":
            await self._handle_attachment_serve(path, writer, cors)
            return
        await _write_404(writer, extra_cors=cors)

    async def _handle_attachment_upload(
        self,
        body_bytes: bytes,
        content_type: str,
        writer: asyncio.StreamWriter,
        cors: bool,
        x_filename: str = "",
    ) -> None:
        """POST /debug/attachments/upload — 保存文件到 attachments_root。

        Content-Type 决定 MIME type（默认 image/png）。
        返回 saved file 的 HTTP URL（`http://host:port/debug/attachments/<uuid>.png`），
        前端 <img src> 直接用；multimodalunderstand tool 也用这个 URL（fetch + base64）。

        音频走另一种语义：除了 HTTP url，还返回本地绝对 `path` 字段，方便
        MonoDesk 把它直接塞进 ws frame 的 attachment.path，server 端 skill
        （如 asr）能直接读本地文件处理，避免 HTTP 回环→重传字节的浪费。
        """
        root = self._attachments_root
        if root is None:
            await _send_json(writer, 500, {"error": "attachments not configured"}, extra_cors=cors)
            return

        import uuid
        import os
        from urllib.parse import unquote

        # 从 Content-Type 提取 mime，e.g. "image/png" or "image/png; charset=..."
        mime = content_type.split(";")[0].strip() or "image/png"
        # 常见 mime → 扩展名（image + audio 都要支持）
        ext_map = {
            # image
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            # audio（ASR 落地用）
            "audio/mp4": ".m4a",       # iOS 录音、微信音频常见
            "audio/x-m4a": ".m4a",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/wave": ".wav",
            "audio/ogg": ".ogg",
            "audio/aac": ".aac",
            "audio/flac": ".flac",
            "audio/x-flac": ".flac",
            "audio/opus": ".opus",
        }
        ext = ext_map.get(mime, "")
        original_name = _sanitize_filename(unquote(x_filename)) if x_filename else ""
        # disk 文件名 = 原名 + ext（没原名时退回 `<uuid><ext>`，避免同名覆盖）。
        filename = f"{original_name}{ext}" if original_name else f"{uuid.uuid4().hex}{ext}"

        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "other"

        attachments_dir = root / "attachments"
        try:
            attachments_dir.mkdir(parents=True, exist_ok=True)
            saved_path = attachments_dir / filename
            saved_path.write_bytes(body_bytes)
        except OSError as exc:
            await _send_json(writer, 500, {"error": f"write failed: {exc}"}, extra_cors=cors)
            return

        _log.info("attachment saved: %s (%d bytes, mime=%s, kind=%s)", saved_path, len(body_bytes), mime, kind)
        # attachment 是本地文件 —— wire 上只传本地 path，LLM / 前端都拿 path 直接用。
        display_name = original_name if original_name else filename
        payload: dict[str, Any] = {
            "path": str(saved_path),
            "name": display_name,
            "mime": mime,
            "kind": kind,
        }
        await _send_json(writer, 200, payload, extra_cors=cors)

    async def _handle_attachment_serve(
        self, path: str, writer: asyncio.StreamWriter, cors: bool,
    ) -> None:
        """GET /debug/attachments/<filename> — 把上传过的图片字节喂回调用方。

    路径合法性：
- 必须以 attachments/ 子目录为根（防 path traversal：`../` 直接拒绝）。
- 文件名不含 `/`。
    """
        root = self._attachments_root
        if root is None:
            await _send_json(writer, 500, {"error": "attachments not configured"}, extra_cors=cors)
            return
        filename = path[len("/debug/attachments/"):]
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            await _send_json(writer, 400, {"error": "invalid filename"}, extra_cors=cors)
            return
        attachments_dir = (root / "attachments").resolve()
        file_path = (attachments_dir / filename).resolve()
        # 二次校验：resolve 后仍必须在 attachments_dir 下
        if attachments_dir != file_path and attachments_dir not in file_path.parents:
            await _send_json(writer, 400, {"error": "invalid filename"}, extra_cors=cors)
            return
        if not file_path.is_file():
            await _send_json(writer, 404, {"error": "file not found"}, extra_cors=cors)
            return
        try:
            body = file_path.read_bytes()
        except OSError as exc:
            await _send_json(writer, 500, {"error": f"read failed: {exc}"}, extra_cors=cors)
            return
        # 从扩展名推 mime（写文件时也用同样映射，对称）
        ext_map = {
            # image
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            # audio
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
            ".opus": "audio/opus",
            # doc（新增，跟 ALLOWED_UPLOAD_MIME 白名单 1:1 对齐 —— 之前漏 doc
            # 导致 PDF 等发出去后 server 返回 application/octet-stream，浏览器
            # <img src=...pdf> 拿不到正确 mime 渲染第一页）
            ".pdf": "application/pdf",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".csv": "text/csv",
            ".json": "application/json",
            ".txt": "text/plain",
        }
        mime = ext_map.get(file_path.suffix.lower(), "application/octet-stream")
        await _send_bytes(writer, 200, mime, body, extra_cors=cors)

    async def _handle_skills(
        self,
        method: str,
        path: str,
        body_bytes: bytes,
        content_type: str,
        writer: asyncio.StreamWriter,
        cors: bool,
    ) -> None:
        """处理 /debug/skills/* 系列路由。

        - GET  /debug/skills/list
        - GET  /debug/skills/<name>
        - PUT  /debug/skills/<name>           body = markdown 文本
        - DELETE /debug/skills/<name>
        - POST /debug/skills/upload           body = zip 字节
        """
        svc = self._skill_service
        assert svc is not None  # caller checks

        # /debug/skills/list
        if path == "/debug/skills/list":
            if method != "GET":
                await _send_json(
                    writer, 405, {"error": "method not allowed"}, extra_cors=cors,
                )
                return
            payload = {
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "tier": s.tier,
                        "path": str(s.path),
                    }
                    for s in svc.abstract()
                ]
            }
            await _send_json(writer, 200, payload, extra_cors=cors)
            return

        # /debug/skills/upload
        if path == "/debug/skills/upload":
            if method != "POST":
                await _send_json(
                    writer, 405, {"error": "method not allowed"}, extra_cors=cors,
                )
                return
            try:
                added = extract_skill_zip(body_bytes, svc._root)
            except SkillUploadError as exc:
                await _send_json(
                    writer, 400, {"error": str(exc)}, extra_cors=cors,
                )
                return
            except zipfile.BadZipFile as exc:
                await _send_json(
                    writer, 400, {"error": f"invalid zip: {exc}"}, extra_cors=cors,
                )
                return
            except OSError as exc:
                await _send_json(
                    writer, 500, {"error": f"write failed: {exc}"}, extra_cors=cors,
                )
                return
            _log.info("uploaded skill pack: added=%s", added)
            await _send_json(writer, 200, {"added": added}, extra_cors=cors)
            return

        # /debug/skills/<name>
        name = path[len("/debug/skills/"):]
        if not name or "/" in name:
            await _send_json(
                writer, 400, {"error": "invalid skill name"}, extra_cors=cors,
            )
            return

        if method == "GET":
            try:
                body = svc.load(name)
                meta = next((s for s in svc.abstract() if s.name == name), None)
            except FileNotFoundError:
                await _send_json(
                    writer, 404, {"error": "skill not found", "name": name},
                    extra_cors=cors,
                )
                return
            payload = {
                "name": name,
                "description": meta.description if meta else "",
                "tier": meta.tier if meta else 1,
                "path": str(svc._root / name),
                "body": body,
            }
            await _send_json(writer, 200, payload, extra_cors=cors)
            return
        if method == "PUT":
            try:
                text = body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                await _send_json(
                    writer, 400, {"error": "body must be utf-8 text"}, extra_cors=cors,
                )
                return
            try:
                svc.write(name, text)
            except ValueError as exc:
                await _send_json(
                    writer, 400, {"error": str(exc)}, extra_cors=cors,
                )
                return
            except OSError as exc:
                await _send_json(
                    writer, 500, {"error": f"write failed: {exc}"}, extra_cors=cors,
                )
                return
            await _send_json(writer, 200, {"ok": True, "name": name}, extra_cors=cors)
            return
        if method == "DELETE":
            try:
                svc.delete(name)
            except FileNotFoundError:
                await _send_json(
                    writer, 404, {"error": "skill not found", "name": name},
                    extra_cors=cors,
                )
                return
            except ValueError as exc:
                await _send_json(
                    writer, 400, {"error": str(exc)}, extra_cors=cors,
                )
                return
            except OSError as exc:
                await _send_json(
                    writer, 500, {"error": f"delete failed: {exc}"}, extra_cors=cors,
                )
                return
            await _send_json(writer, 200, {"ok": True, "name": name}, extra_cors=cors)
            return
        await _send_json(
            writer, 405, {"error": "method not allowed"}, extra_cors=cors,
        )


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


async def _write_cors_preflight(writer: asyncio.StreamWriter) -> None:
    """OPTIONS 探一下：浏览器 POST 带非 simple Content-Type (e.g. image/png) 之前
    会先发 OPTIONS；不返回 204 + 完整 CORS 头浏览器就拦掉真正的 POST。

    Max-Age 24h：避免每次请求都 preflight。
    """
    headers = [
        "HTTP/1.1 204 No Content",
        "Access-Control-Allow-Origin: *",
        "Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers: Content-Type, X-Filename",
        "Access-Control-Max-Age: 86400",
        "Content-Length: 0",
        "Connection: close",
    ]
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
    writer.write(raw)
    try:
        await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


async def _send_bytes(
    writer: asyncio.StreamWriter,
    status: int,
    mime: str,
    body: bytes,
    *,
    extra_cors: bool,
) -> None:
    """Serve 二进制文件：状态行 + Content-Type + Content-Length + CORS（可选）+ body。

    _send_json 只吃 dict；二进制文件流必须用这个。
    """
    status_text = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {status_text}",
        f"Content-Type: {mime}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if extra_cors:
        headers.insert(2, "Access-Control-Allow-Origin: *")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
    writer.write(raw)
    try:
        await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


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