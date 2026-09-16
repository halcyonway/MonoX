"""read_doc tool: 万能本地文档读取，按后缀识别格式。

设计要点（详见 spec/requirements/doc-tool-universal.md）：

- args = ``{path}``，suffix 检测在 tool 内部完成
- v1 支持：.pdf / .txt / .md / .csv / .json
- 不支持的格式：返回 ``status=error`` + stderr 明确告诉 agent 下一步怎么办
  （不要 retry；用 ``bash`` 调其它工具或告诉用户格式不支持）
- 只读操作；路径解析跟 ``bash`` tool 一致：相对路径 = session workspace
- PDF 抽不出来文字（纯扫描件）时返回空 stdout + 提示走 ``multimodalunderstand``
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Callable

from core.protocol import ToolResult

# 5MB 上限：超过则提示用 bash chunk / streaming reader。pypdf 抽 5MB PDF 内存
# 峰值约 50-100MB（解压后），再大就 4GB 内存里容易 OOM。
MAX_FILE_BYTES = 5 * 1024 * 1024


# ---- handler 们 ----------------------------------------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_pdf(path: Path) -> str:
    """pypdf 抽文字 → 段落拼接。

    为什么用 pypdf：纯 Python、无原生编译、wheel 普遍，~5MB 安装体积；
    缺点是对复杂排版（多栏 / 表格）抽取质量一般，但对「普通 PDF 文字版」够用。
    v2 看 marker-pdf ROI 再升级。
    """
    # Lazy import：pypdf 是新增的 hard dep，但 tool 装载不 import 它；
    # 单元测试 / 不调 read_doc 的 agent 不会为它付钱。
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            # 单页失败不打断整文；在返回里标出
            parts.append(f"\n[page {i+1} extract error: {type(exc).__name__}: {exc}]\n")
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _read_csv(path: Path) -> str:
    """CSV → markdown table。

    用 utf-8-sig 自动剥 BOM（Excel 导出的 CSV 经常带 BOM）。
    `newline=""` 是 csv 模块要求：避免 \r\n 在 Python 层被预先转换。
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return "(empty csv)"
    header = rows[0]
    body = rows[1:]

    def _cell(v: str) -> str:
        # markdown table 单元格不能含未转义的 | ；同时把换行折成 <br>
        return v.replace("|", "\\|").replace("\n", " ").strip()

    out = ["| " + " | ".join(_cell(c) for c in header) + " |"]
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in body:
        # 行长度不齐时补空字符串
        cells = list(row) + [""] * (len(header) - len(row))
        out.append("| " + " | ".join(_cell(c) for c in cells[: len(header)]) + " |")
    return "\n".join(out)


def _read_json(path: Path) -> str:
    """JSON → 缩进序列化。

    ensure_ascii=False 保留中文等非 ASCII 字符（人类可读）；agent 拿到后能直接
    引用字段名 / 值。如果用户要 ASCII 形式可自己再过一遍。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---- suffix 表 -----------------------------------------------------------

# 顺序：先匹配 .markdown（长后缀在前）再 .md；用 startswith 判断。
_HANDLERS: list[tuple[tuple[str, ...], Callable[[Path], str]]] = [
    ((".pdf",), _read_pdf),
    ((".txt",), _read_text),
    ((".md", ".markdown"), _read_text),
    ((".csv",), _read_csv),
    ((".json",), _read_json),
]

# 调试用：把整张表拍平成 dict 给 error message 用
_FLAT_SUFFIXES: dict[str, str] = {}
for suffixes, _ in _HANDLERS:
    for s in suffixes:
        _FLAT_SUFFIXES[s] = s


def _resolve_handler(path: Path) -> tuple[Callable[[Path], str], str] | tuple[None, str]:
    """按 suffix 找 handler；返回 (handler, suffix_lower) 或 (None, suffix_lower)。"""
    suf = path.suffix.lower()
    for suffixes, handler in _HANDLERS:
        if suf in suffixes:
            return handler, suf
    return None, suf


# ---- tool 类 -------------------------------------------------------------

class ReadDocTool:
    name = "read_doc"
    schema = {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": (
                "Read a local document file and return its text content as markdown. "
                "Format is auto-detected from the file suffix. "
                "Supported formats: "
                ".pdf (text extraction via pypdf), "
                ".txt / .md / .markdown (read as utf-8), "
                ".csv (parsed into a markdown table), "
                ".json (re-serialized with indent=2). "
                "If the file has an unsupported suffix, returns an error — "
                "do not retry; instead use `bash` with another tool "
                "(e.g. `libreoffice --headless --convert-to pdf`, `pandoc`, "
                "`python -c ...` with a dedicated library) "
                "or tell the user the format is not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or sandbox-relative path to the document. "
                            "Relative paths resolve against the session workspace "
                            "(same as `bash` cwd). "
                            "Examples: '/path/to/report.pdf', './data/q3.csv', 'notes.md'."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        path_arg = arguments.get("path", "").strip()
        if not path_arg:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr="path is required",
                exit_code=1,
            )

        # 跟 bash tool 一致：相对路径 = session workspace 解析
        p = Path(path_arg)
        if not p.is_absolute():
            p = (self._workspace / path_arg).resolve()
        else:
            p = p.resolve()

        if not p.exists():
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"file not found: {p}",
                exit_code=1,
            )

        if not p.is_file():
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"not a regular file: {p}",
                exit_code=1,
            )

        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            size_mb = size / (1024 * 1024)
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=(
                    f"file too large: {size_mb:.1f}MB (limit {MAX_FILE_BYTES // (1024*1024)}MB). "
                    f"Use `bash` to chunk or use a streaming reader."
                ),
                exit_code=1,
            )

        handler, suffix = _resolve_handler(p)
        if handler is None:
            supported = " ".join(sorted(_FLAT_SUFFIXES.keys()))
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=(
                    f"unsupported format: {suffix or '(no suffix)'}. "
                    f"Supported: {supported}. "
                    f"Use `bash` to convert (e.g. `libreoffice --headless --convert-to pdf`, "
                    f"`pandoc -o out.md in.docx`) or tell the user the format is not supported."
                ),
                exit_code=1,
            )

        try:
            text = handler(p)
        except FileNotFoundError:
            # 存在性 stat 之后文件被删了 — 罕见但要 cover
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"file disappeared during read: {p}",
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

        # 特殊：PDF 完全抽不出文字（扫描件）
        if suffix == ".pdf" and not text.strip():
            return ToolResult(
                call_id=call_id,
                status="ok",
                stdout=(
                    "(PDF contains no extractable text — likely a scanned image. "
                    "Use `multimodalunderstand` on individual pages or run OCR via `bash` "
                    "(e.g. `ocrmypdf`, `tesseract`).)"
                ),
                stderr="",
                exit_code=0,
            )

        # 大小提示：抽出来超过 100KB 时给 agent 标一句（避免它回灌整个 LLM context）
        header = ""
        if len(text) > 100 * 1024:
            header = f"(extracted {len(text):,} chars; truncated summary recommended before quoting)\n\n"

        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=header + text,
            stderr="",
            exit_code=0,
        )
