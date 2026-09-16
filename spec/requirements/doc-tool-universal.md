# doc-tool-universal: 万能文档读取工具 + 前端格式白名单

> **状态**: 2026-09 起草。
> **作者意图**: 用户在 chat 里甩一个 PDF / Word / Excel 路径，agent 应该能直接读；
> 不是合法格式就明说，让 agent 自己决定是 `bash` 调别的工具还是告诉用户「我读不了」。
> 同步约束 MonoDesk 上传，只允许跟 tool 支持的格式一致——避免上传了 tool 读不了的东西。

## 1. Context

当前 agent 想读一个本地文件只能 `bash cat file.pdf` —— 拿到的是字节流，模型无意义；
正确路径是调 `multimodalunderstand`（仅 image/audio）或自己 `pip install pypdf` 后 `python -c "..."`，
每次都要绕一圈。

用户的诉求：
1. **一个 tool，多种格式**：传 `path`，tool 自己按后缀识别；支持的格式直接返回 markdown 文本。
2. **不支持的格式就明说**：不静默失败，不给空字符串；让 agent 决定怎么办（`bash` 调别的、坦白说读不了）。
3. **tool spec 明列支持范围**：避免 LLM 试错；schema 描述里把"我现在支持什么"写死。
4. **MonoDesk 上传要卡住格式**：用户拖一个 .rar 进来不收，跟 tool 支持的范围对齐。

## 2. 设计

### 2.1 整体拓扑

```
                 ┌─ MonoX side ─────────────────────────────┐
   LLM agent ──►│ bash / multimodalunderstand / read_doc ... │
                 │     │                                    │
                 │     ▼                                    │
                 │  read_doc (NEW)                          │
                 │   args: { path: str }                    │
                 │   内部: suffix → handler                 │
                 │     .pdf  → pypdf extract_text           │
                 │     .txt  → utf-8 read                   │
                 │     .md   → utf-8 read                   │
                 │     .csv  → csv → md table               │
                 │     .json → json.dumps(indent=2)         │
                 │     其他   → ToolResult(status=error,    │
                 │               "unsupported format: .x")  │
                 └──────────────────────────────────────────┘

                 ┌─ MonoDesk side ──────────────────────────┐
   user drag ──►│ Composer: accept="application/pdf,         │
                │  text/plain, text/markdown, text/csv,     │
                │  application/json"                        │
                │  不在白名单 → silently drop + console.warn │
                └────────────────────────────────────────────┘
```

### 2.2 v1 支持的格式白名单

| 后缀 | MIME | v1 处理 | 来源 |
|---|---|---|---|
| `.pdf` | `application/pdf` | `pypdf` 抽 text → 拼回 markdown 段落 | 新增 `pypdf` 依赖 |
| `.txt` | `text/plain` | `Path.read_text(encoding="utf-8")` | stdlib |
| `.md` / `.markdown` | `text/markdown` | 同上，保留原文 | stdlib |
| `.csv` | `text/csv` | `csv.reader` → markdown 表格 | stdlib |
| `.json` | `application/json` | `json.dumps(indent=2, ensure_ascii=False)` | stdlib |

> **v1 不支持**（tool spec 写明，agent 看到立刻知道）：
> `.docx` / `.pptx` / `.xlsx` / `.epub` / `.html` / 图片 / 音频
>
> 未来 v2+ 再扩。调研过 `marker-pdf`（datalab-to/marker）质量最高但依赖重（PyTorch ≥ 2GB），
> v1 不上；v2 看 LLM 调 marker 走 `bash` 自行启子进程是否更划算。

### 2.3 Tool Schema

```python
# core/loop/tools/read_doc.py
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
                ".txt / .md (read as utf-8), "
                ".csv (parsed into a markdown table), "
                ".json (re-serialized with indent=2). "
                "If the file has an unsupported suffix, returns an error — "
                "do not retry; instead use `bash` with another tool (e.g. `libreoffice --headless --convert-to pdf`, "
                "`python -c ...` with a dedicated library) or tell the user the format is not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or sandbox-relative path to the document. "
                            "Relative paths resolve against the session workspace. "
                            "Examples: '/path/to/report.pdf', './data/q3.csv', 'notes.md'."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
```

### 2.4 Suffix → Handler 映射

```python
_SUFFIX_HANDLERS: dict[str, Callable[[Path], str]] = {
    ".pdf":  _read_pdf,   # pypdf.PdfReader
    ".txt":  _read_text,
    ".md":   _read_text,
    ".markdown": _read_text,
    ".csv":  _read_csv,   # csv.reader → md table
    ".json": _read_json,  # json.dumps(indent=2, ensure_ascii=False)
}
```

大小写不敏感：先 `path.suffix.lower()` 再查表。

### 2.5 错误语义

| 情况 | 返回 |
|---|---|
| `path` 为空 / 不存在 | `status=error`, `stderr="file not found: <path>"` |
| 文件存在但后缀不支持 | `status=error`, `stderr=f"unsupported format: {suffix}. Supported: .pdf .txt .md .csv .json. Use `bash` to convert or another tool."` |
| PDF 加密 / 损坏 | `status=error`, `stderr=f"pypdf error: {type(e).__name__}: {e}"` |
| CSV 解析失败 | `status=error`, `stderr=f"csv error: {e}"` |
| JSON 解析失败 | `status=error`, `stderr=f"json error: {e}"` |
| PDF 抽出来是空（纯图片 PDF） | `status=ok`, `stdout="(PDF contains no extractable text — likely a scanned image. Use `multimodalunderstand` on individual pages or run OCR via `bash`.)"` |
| 文件 > 5MB | `status=error`, `stderr=f"file too large: {size_mb:.1f}MB (limit 5MB). Use `bash` to chunk or use a streaming reader."` |

**关键**：「不支持格式」和「PDF 无文本」都明确告诉 agent **下一步该干嘛**（用 `bash` / `multimodalunderstand`），
不是空 stdout 让人猜。

### 2.6 安全 / 沙箱

- 路径解析：相对路径 `self._workspace / path`（同 `bash` tool 的 cwd 解析逻辑）
- 绝对路径直接用，但要 `resolve()` 后检查仍然存在（防 symlink 攻击不在 v1 范围；记 spec §6 不做）
- 不引入新沙箱边界；read-only 操作，跟 `multimodalunderstand` 一样信任 agent

### 2.7 安装与依赖

`pypdf` 是新依赖。两条路：

1. **`pyproject.toml` 加 `dependencies = [..., "pypdf>=4.0"]`**，改一份。
2. **可选依赖**：`[project.optional-dependencies] doc = ["pypdf>=4.0"]`，`uv sync --extra doc` 才装。

v1 选 ①：pypdf 纯 Python、无原生编译、wheel 普遍，~5MB 安装体积，比起把 PDF 支持留作 opt-in
更有价值。记入 `scripts/install.sh` 的依赖提示。

### 2.8 MonoDesk 侧：上传格式白名单

`Composer.tsx` 当前 `addFiles` 只过 `image/*`，`accept="image/*"`。改造点：

```tsx
// 新增常量
const ALLOWED_UPLOAD_MIME = [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/json",
  // 保留原 image
  "image/png", "image/jpeg", "image/gif", "image/webp",
];

// addFiles 过滤逻辑：
const allowed = Array.from(files).filter((f) => ALLOWED_UPLOAD_MIME.includes(f.type));

// accept 改成：
accept="application/pdf,text/plain,text/markdown,text/csv,application/json,image/*"
```

预览组件 (`<img>` thumbnail) 当前只服务 image；PDF/CSV 走通用 doc thumb（图标 + 文件名），
不在 v1 范围——v1 目标只是"不让不支持的格式上传"，预览 fallback 是 v2 美化。

> **设计取舍**：不在 MonoDesk 侧硬限制「只 PDF」——白名单跟 tool 侧 v1 列表 1:1 同步。
> v2 扩 docx 等格式时只改一个常量 + tool spec 一行。

### 2.9 Debug server 侧：upload 端点

`core/debug_server.py:_handle_attachment_upload` 当前对 mime 无限制（只防 path traversal）。
要不要在 server 端也卡白名单？

**v1 不动 server 端**。理由：
- server 端无 schema 概念，不知道 tool 名单
- agent 自己 `bash curl` 上传时不会经过 MonoDesk，不该被这条规则挡
- 白名单是 UI 体验问题，不是安全边界

记入 §6 不做。

---

## 3. 文件清单

### 新增（3 个文件）

| 文件 | 内容 |
|---|---|
| `core/loop/tools/read_doc.py` | `ReadDocTool` (Tool Protocol) + suffix handlers + pypdf/pdf/csv/json 实现 |
| `tests/test_read_doc.py` | 单元测试：5 个格式 happy path、unsupported suffix、missing file、big file、scanned PDF |
| `spec/requirements/doc-tool-universal.md` | 本文件 |

### 修改（4 个文件）

| 文件 | 修改 |
|---|---|
| `core/loop/tools/__init__.py` | `+ from .read_doc import ReadDocTool`，加入 `__all__` |
| `core/loop/__init__.py` | `+ from .tools import ReadDocTool` + `__all__` |
| `run.py` | import + 装配到 `ToolRegistry([...])` |
| `pyproject.toml` | `dependencies = [..., "pypdf>=4.0"]` |

### MonoDesk 侧修改（2 个文件）

| 文件 | 修改 |
|---|---|
| `src/components/Composer.tsx` | 加 `ALLOWED_UPLOAD_MIME` 常量；`addFiles` 按白名单过滤；`accept` 同步；非白名单 silent drop + console.warn |
| `src/components/Composer.test.tsx` | 1 个新测试：上传 .rar / .docx → 不出现在 previews 列表 |

---

## 4. 验证

### 4.1 MonoX 侧

```bash
cd MonoX
uv run pytest tests/test_read_doc.py -q          # 新 tool 单测
uv run pytest tests/ -q                          # 全跑，确认不破其他
uv run python run.py --stop && uv run python run.py  # 启停正常
```

测试用例：

1. **PDF happy path**: `pypdf` 抽 sample.pdf（多页）→ 拼接段落返回
2. **TXT**: `tmp.txt = "hello\n世界"` → stdout 完整保留
3. **MD**: 保留原文（不二次渲染）
4. **CSV**: 三行三列 → markdown table 格式
5. **JSON**: dict / list → `indent=2` 序列化
6. **Unsupported suffix**: `.docx` → `status=error`, `stderr` 含 "unsupported format" + 提示用 bash
7. **Missing file**: `/nonexistent` → `status=error`, `stderr="file not found"`
8. **Big file**: 构造 6MB .txt → `status=error`, `stderr` 含 "file too large"
9. **Scanned PDF**: 空 pypdf 抽 → `stdout` 含 "PDF contains no extractable text" + 提示 multimodalunderstand
10. **Suffix case-insensitive**: `.PDF` 跟 `.pdf` 走同一路径

### 4.2 MonoDesk 侧

```bash
cd MonoDesk
npx vitest run                  # 全跑，确认不破现有 + 新 1 个 test
npm run typecheck
npm run dev                     # 手工 e2e:
  # - 拖一个 .pdf → previews 出现 thumb
  # - 拖一个 .docx → silently drop, console.warn 出现
  # - 拖一个 .png → 仍正常（image 兼容）
```

### 4.3 手工 e2e

```bash
# 1) 准备一份 PDF（任意一份现成 PDF）
cp ~/Downloads/some.pdf .monox/workspace/default/test.pdf

# 2) 启 runtime
cd MonoX && uv run python run.py

# 3) MonoDesk 端发问
"帮我读一下 workspace/default/test.pdf 的内容"
# 期望：agent 调 read_doc(path="test.pdf")，看到 markdown 文本，回答内容
```

---

## 5. 关键不变量

- `core/protocol/ToolResult` 不变 schema（status / stdout / stderr / exit_code）
- `bash` / `multimodalunderstand` / `skill_load` / `wait_io` 行为不变
- 工具不写文件，只读
- 工具 spec 描述里**显式列支持格式**（避免 LLM 试错）
- 「不支持格式」错误信息包含**下一步建议**（用 `bash` 调其它工具）
- MonoDesk 端白名单 = tool 端白名单 1:1（v1 都是 image + pdf + txt + md + csv + json）

---

## 6. 不做（明确范围）

- **不做** `.docx` / `.pptx` / `.xlsx` 支持（v2 再说；marker-pdf 或 python-docx 等）
- **不做** 图片 / 音频走 `read_doc`（`multimodalunderstand` 已覆盖）
- **不做** OCR（扫描 PDF 提示 agent 调 `multimodalunderstand` 或 `bash` 跑 tesseract）
- **不做** 服务端 `debug_server.py` upload mime 白名单（agent 走 bash 上传不应被挡）
- **不做** symlink / path traversal 防护（v1 信任 agent；同 `bash` 行为）
- **不做** chunked / streaming PDF reader（5MB 上限卡死，大文档提示用 `bash`）
- **不做** MonoDesk 端的 PDF / doc 预览 UI（v1 只卡格式，缩略图是 v2）
- **不做** 配置化白名单（hard-code 列表；扩展 = 改两个常量）
- **不做** `marker-pdf` / `pymupdf` 切换（v1 pypdf 足够；v2 看 LLM 调度的 marker ROI 再评估）
