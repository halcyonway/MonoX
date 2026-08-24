"""multimodalunderstand tool — MiniMax vision API (text-openai-api compatible).

Input: a local file path or HTTP(S) URL pointing to an image.
Output: textual description of the image content.

Uses `requests` directly (no SDK), following the MiniMax text-openai-api spec:
https://platform.minimaxi.com/docs/api-reference/text-openai-api

Image is sent as:
- URL: `image_url` with `url` field → for HTTP URLs
- base64: `image_url` with `url: f"data:{mime};base64,{b64}"` → for local files
"""
from __future__ import annotations

import base64
import os
import re
from typing import Any

import requests

from core.protocol import ToolResult


_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
_API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")
# 视觉模型：在 MiniMax 的 text-openai-api 上，仅 MiniMax-M3（Opus-tier）接受 image_url。
# 其它档位（M2.x 系列）即使收到合法 PNG data URI，也会在 <think> 里直接断言
# "no image attached" 并拒绝描述。默认走 M3；测试时可被 MULTIMODAL_MODEL 覆盖。
_DEFAULT_VISION_MODEL = "MiniMax-M3"
_MODEL = os.environ.get("MULTIMODAL_MODEL", _DEFAULT_VISION_MODEL)


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


# URL 路径后缀 → MIME。所有 URL 模式（本地 / HTTP）都共用这一份。
_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _mime_from_url(url: str) -> str:
    """从 URL 路径后缀推 MIME。fallback 到 image/png（MiniMax 默认接受）。"""
    # 去掉 query string 和 fragment，只看 path
    path = url.split("?")[0].split("#")[0]
    # 用 str.rsplit 找最后一个 '.'；跳过 query 中的 '.'
    for ext in _EXT_TO_MIME:
        if path.lower().endswith(ext):
            return _EXT_TO_MIME[ext]
    return "image/png"


def _image_b64(path: str, mime: str = "image/png") -> str:
    with open(path, "rb") as f:
        raw = f.read()
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _fetch_b64(url: str, mime: str, timeout: int = 30) -> str:
    """HTTP URL → 下载 → base64 data URI。

    为什么要这一步：MiniMax vision API 是云服务，没法直接 fetch 我们 localhost 的文件。
    哪怕 URL 是 internet 端的公开图片，统一走 base64 也省一次 MiniMax 服务端的 egress。
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return f"data:{mime};base64,{base64.b64encode(resp.content).decode('ascii')}"


def _call_vision(image_url: str, prompt: str = "Describe this image in detail.") -> str:
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }

    # Build content blocks
    if _is_http_url(image_url):
        # HTTP URL：fetch → base64（MiniMax 拿不到 localhost，不能直接喂 URL）
        mime = _mime_from_url(image_url)
        data_uri = _fetch_b64(image_url, mime)
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": "default"},
            },
        ]
    else:
        # Local file: encode as data-URI
        content_blocks = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": _image_b64(image_url), "detail": "default"},
            },
        ]

    payload: dict[str, Any] = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": content_blocks}],
        "stream": False,
    }

    resp = requests.post(
        f"{_API_BASE}/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    choices = data.get("choices") or []
    if not choices:
        return "(no response from model)"

    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    # content may be a string or a list of content blocks
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        )
    return str(content)


class MultimodalUnderstandTool:
    name = "multimodalunderstand"
    schema = {
        "type": "function",
        "function": {
            "name": "multimodalunderstand",
            "description": (
                "Analyze an image and return a detailed textual description of its content. "
                "Accepts a local file path (e.g. /path/to/image.png) or an HTTP(S) URL. "
                "Use this when you need to understand what's shown in an image — "
                "screenshots, photos, diagrams, charts, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_url": {
                        "type": "string",
                        "description": (
                            "URL or local file path of the image to analyze. "
                            "Common case: an HTTP URL returned by the upload endpoint "
                            "(e.g. http://127.0.0.1:8768/debug/attachments/abc123.png). "
                            "Local file paths are also accepted for backwards compatibility."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Optional question or instruction about the image. "
                            "Defaults to 'Describe this image in detail.'."
                        ),
                    },
                },
                "required": ["attachment_url"],
                "additionalProperties": False,
            },
        },
    }

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        url = arguments.get("attachment_url", "")
        if not url:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr="attachment_url is required",
                exit_code=1,
            )

        prompt = arguments.get("prompt") or "Describe this image in detail."

        try:
            description = _call_vision(url, prompt)
            return ToolResult(
                call_id=call_id,
                status="ok",
                stdout=description,
                stderr="",
                exit_code=0,
            )
        except FileNotFoundError:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"file not found: {url}",
                exit_code=1,
            )
        except requests.HTTPError as exc:
            body = exc.response.text[:500] if exc.response else ""
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"HTTP {exc.response.status_code if exc.response else '?'}: {body}",
                exit_code=1,
            )
        except Exception as exc:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_code=1,
            )
