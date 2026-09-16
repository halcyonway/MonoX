"""read_doc tool 单测。

PDF 用例需要一份带可抽文字的 PDF。pypdf 写不进去文字（只能加空页），
所以手动构造一份最小可抽取的 PDF 字节流塞到 tmp_path，
覆盖 PDF happy path + 扫描件（空白页 → 空 stdout）两条路径。

依赖：pypdf（项目新增 dep）。其它 handler 都是 stdlib。
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from core.loop.tools.read_doc import MAX_FILE_BYTES, ReadDocTool


# 最小可抽取文字的 PDF 字节流。
# 1 页，72x72，文字 stream: "(hello) Tj"
# 由 PyMuPDF 之外最直接的「最少行 PDF 模板」拼出来。
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 33>>stream\n"
    b"BT /F1 12 Tf 10 10 Td (Hello PDF) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000056 00000 n \n"
    b"0000000103 00000 n \n"
    b"0000000207 00000 n \n"
    b"0000000290 00000 n \n"
    b"trailer<</Size 6/Root 1 0 R>>\n"
    b"startxref\n360\n%%EOF\n"
)


def _build_tool(tmp_path: Path) -> ReadDocTool:
    # 拿 tmp_path 当 workspace 根，相对路径解析用
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return ReadDocTool(workspace)


async def _run(tool: ReadDocTool, path: str):
    return await tool.execute("c1", {"path": path})


# ---- happy paths ---------------------------------------------------------

@pytest.mark.asyncio
async def test_txt_returns_content(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "note.txt").write_text("hello\n世界", encoding="utf-8")
    r = await _run(tool, "note.txt")
    assert r.status == "ok"
    assert r.stdout == "hello\n世界"


@pytest.mark.asyncio
async def test_md_returns_content_verbatim(tmp_path):
    tool = _build_tool(tmp_path)
    md = "# Title\n\n- a\n- b\n"
    (tmp_path / "ws" / "r.md").write_text(md, encoding="utf-8")
    r = await _run(tool, "r.md")
    assert r.status == "ok"
    assert r.stdout == md
    # .markdown 长后缀同样工作
    (tmp_path / "ws" / "r2.markdown").write_text(md, encoding="utf-8")
    r2 = await _run(tool, "r2.markdown")
    assert r2.status == "ok"
    assert r2.stdout == md


@pytest.mark.asyncio
async def test_csv_renders_markdown_table(tmp_path):
    tool = _build_tool(tmp_path)
    csv_path = tmp_path / "ws" / "data.csv"
    # utf-8-sig BOM 验证：保证 Excel 导出可读
    csv_path.write_bytes(b"\xef\xbb\xbf" + "name,age\nAlice,30\nBob,25\n".encode("utf-8"))
    r = await _run(tool, "data.csv")
    assert r.status == "ok"
    # 实际输出（用 repr 确认空格数）
    expected = "| name | age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 25 |"
    assert r.stdout == expected, f"got: {r.stdout!r}"


@pytest.mark.asyncio
async def test_json_pretty_prints_with_unicode(tmp_path):
    tool = _build_tool(tmp_path)
    data = {"你好": "world", "n": [1, 2, 3]}
    (tmp_path / "ws" / "x.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    r = await _run(tool, "x.json")
    assert r.status == "ok"
    assert r.stdout == json.dumps(data, indent=2, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pdf_happy_path_extracts_text(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "doc.pdf").write_bytes(_MINIMAL_PDF)
    r = await _run(tool, "doc.pdf")
    assert r.status == "ok"
    # pypdf 抽出来包含 "Hello"（具体大小写 / 空白随版本略变）
    assert "Hello" in r.stdout
    assert "PDF" in r.stdout


@pytest.mark.asyncio
async def test_pdf_scanned_returns_hint(tmp_path):
    """纯图片 PDF（pypdf 抽出来是空）→ 返回 ok + 提示走 multimodalunderstand。"""
    tool = _build_tool(tmp_path)
    # 用 pypdf 写一张空白页 — 没有文字 stream
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf_path = tmp_path / "ws" / "scanned.pdf"
    with open(buf_path, "wb") as f:
        w.write(f)
    r = await _run(tool, "scanned.pdf")
    assert r.status == "ok"
    assert "no extractable text" in r.stdout
    assert "multimodalunderstand" in r.stdout


# ---- error paths ---------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_path_argument(tmp_path):
    tool = _build_tool(tmp_path)
    r = await _run(tool, "")
    assert r.status == "error"
    assert "path is required" in r.stderr


@pytest.mark.asyncio
async def test_missing_file(tmp_path):
    tool = _build_tool(tmp_path)
    r = await _run(tool, "ghost.pdf")
    assert r.status == "error"
    assert "file not found" in r.stderr


@pytest.mark.asyncio
async def test_path_is_directory(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "subdir").mkdir()
    r = await _run(tool, "subdir")
    assert r.status == "error"
    assert "not a regular file" in r.stderr


@pytest.mark.asyncio
async def test_unsupported_suffix_returns_error_with_suggestion(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "book.docx").write_bytes(b"PK\x03\x04 fake docx")
    r = await _run(tool, "book.docx")
    assert r.status == "error"
    assert "unsupported format" in r.stderr
    assert ".docx" in r.stderr
    # 错误信息必须告诉 agent 下一步怎么办
    assert "bash" in r.stderr
    # 列出所有支持的后缀
    for suf in (".pdf", ".txt", ".md", ".csv", ".json"):
        assert suf in r.stderr


@pytest.mark.asyncio
async def test_no_suffix_is_unsupported(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "LICENSE").write_text("MIT", encoding="utf-8")
    r = await _run(tool, "LICENSE")
    assert r.status == "error"
    assert "unsupported format" in r.stderr


@pytest.mark.asyncio
async def test_file_too_large(tmp_path):
    tool = _build_tool(tmp_path)
    big = tmp_path / "ws" / "big.txt"
    # 不真的写 5MB，只用 stat_size 蒙 tool —— 但 tool 走 stat()，得真造
    # 折中：写 ~5.5MB 的稀疏文件 (sparse on Linux OK; on macOS 也 OK)
    with open(big, "wb") as f:
        f.write(b"x" * (MAX_FILE_BYTES + 1024 * 1024))
    r = await _run(tool, "big.txt")
    assert r.status == "error"
    assert "too large" in r.stderr
    # 提示走 bash chunk
    assert "bash" in r.stderr


# ---- 路径解析 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_absolute_path_works(tmp_path):
    tool = _build_tool(tmp_path)
    other = tmp_path / "external.txt"
    other.write_text("absolute-path content", encoding="utf-8")
    r = await _run(tool, str(other))
    assert r.status == "ok"
    assert "absolute-path content" in r.stdout


@pytest.mark.asyncio
async def test_suffix_case_insensitive(tmp_path):
    tool = _build_tool(tmp_path)
    (tmp_path / "ws" / "UPPER.TXT").write_text("hi", encoding="utf-8")
    r = await _run(tool, "UPPER.TXT")
    assert r.status == "ok"
    assert r.stdout == "hi"


# ---- schema contract ----------------------------------------------------

def test_schema_lists_supported_formats_in_description(tmp_path):
    tool = _build_tool(tmp_path)
    desc = tool.schema["function"]["description"]
    for suf in (".pdf", ".txt", ".md", ".csv", ".json"):
        assert suf in desc, f"tool description must mention supported format {suf}"
    # 必须告诉 agent 失败时怎么办
    assert "bash" in desc
    assert "unsupported" in desc.lower() or "not supported" in desc.lower()
