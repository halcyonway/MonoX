# attachment-local-path: multimodalunderstand 跳过 debug server fetch

> multimodalunderstand tool 在收到 debug server attachment URL 时反推本地 path，
> 直接 open() 读 bytes，不再走 requests.get。解决同进程回环 debug server 的
> `ReadTimeout` 问题（`#53`）。
>
> 实现：
> - `core/loop/tools/multimodal_understand.py`（`MultimodalUnderstandTool.__init__(attachments_root)` +
>   `_debug_server_path()` 反推）
> - `run.py`（`run()` 注入 `attachments_root=Path(cfg.sandbox.tmp_root)`）
> - `tests/test_multimodal_understand_path.py`（8 个 unit case 覆盖 happy / edge / security）

---

## 1. Context

MonoX 接收 MonoDesk 上传的图片时，debug server 把 bytes 写到
`{tmp_root}/attachments/<uuid>.<ext>` 然后返回 HTTP URL
`http://<host>:<port>/debug/attachments/<uuid>.<ext>`。

LLM 看到 `url` 字段后调 `multimodalunderstand(attachment_url="http://127.0.0.1:8768/debug/attachments/abc.png")`。
tool 内部用 `requests.get(url, timeout=30)` 抓 bytes。

**问题**：Tauri / 浏览器场景下，从 MonoX process 内 `requests.get` 自己起的
`127.0.0.1:8768` server 经常 `ReadTimeout`（30s 超时）。现象：上传图片 → 调
multimodal_understand → 等 30s → 报错。`#53`。

**根因**：Tauri WebView / 某些 proxy / 端口转发环境下，process 内的 HTTP 客户端
对 localhost 自指请求不可靠。audio 走另一条路（debug server 直接返 `path` 字段，
skill 用 `open(path)`），从来没事 —— 因为 **本地文件 IO 永远可达**。

**修法**：把 `attachments_root` 注入 multimodal_understand，URL 形如
`/debug/attachments/<fname>` 时反推 `{attachments_root}/attachments/<fname>` 走
local 分支（`_image_b64` 走 `open().read()`），不调 `requests.get`。

## 2. 设计

### 2.1 MultimodalUnderstandTool 注入 attachments_root

`run.py` 的 `tools = ToolRegistry([...])` 改为：

```python
MultimodalUnderstandTool(attachments_root=Path(cfg.sandbox.tmp_root)),
```

`attachments_root` 必须跟 debug server 的 `attachments_root` **同源**（都是
`cfg.sandbox.tmp_root`，否则反推路径会 404）。spec/rule.md 里 `sandbox.tmp_root`
是 single source of truth。

### 2.2 `_debug_server_path(url)` 反推规则

```python
def _debug_server_path(self, url: str) -> Path | None:
    if self._attachments_root is None:
        return None
    path_part = url.split("?", 1)[0].split("#", 1)[0]
    marker = "/debug/attachments/"
    idx = path_part.find(marker)
    if idx < 0:
        return None
    fname = path_part[idx + len(marker):]
    if not fname or "/" in fname or "\\" in fname or fname.startswith("."):
        return None
    candidate = self._attachments_root / "attachments" / fname
    return candidate if candidate.is_file() else None
```

**8 条规则**（unit test 覆盖）：

| 场景 | 返回 |
|---|---|
| URL 是 debug server 形式 + 本地文件存在 | `Path`（走 local） |
| URL 是 debug server 形式 + 本地文件不存在 | `None`（fallback fetch） |
| URL 不是 debug server 形式（internet URL） | `None`（原 fetch） |
| filename 含 `/` 或 `\`（path traversal） | `None` |
| filename 为空 | `None` |
| filename 以 `.` 开头（隐藏文件） | `None` |
| `attachments_root=None`（旧代码 back-compat） | 全部 `None` |
| URL 带 query string / fragment | 仍能匹配（先 strip） |

**安全考量**：

- `fname` 不含 `/` `\` `.` 开头 —— 阻止 `../../etc/passwd` 这类 path traversal escape `attachments_root`
- `attachments_root=None` 时工具完全跳过反推 —— 旧测试 / 旧调用方零改动
- 只在 file 存在时返回 —— 进程重启 / 文件被 GC 兜底走 fetch，行为不破

### 2.3 execute() 路由

```python
local_path = self._debug_server_path(url)
effective = str(local_path) if local_path else url
description = _call_vision(effective, prompt)
```

`_call_vision` 已有 `image_url` 是 local path 时走 `_image_b64(path)` 的分支
（line 95-102），不动。

### 2.4 不变（明确范围）

- `_call_vision` / `_fetch_b64` / `_image_b64` 不动
- tool schema 不变（`attachment_url` 字段名 + 描述不变）
- audio 处理路径不动（debug server 已经对 audio 返回 `path` 字段，
  server-side skill 直接 open()）
- Wire protocol（`Attachment` type）不动 —— 这是 server-side tool 的反推，
  前端不需要新字段
- read_doc 工具（已删除）不动

## 3. 验证

### 3.1 单元测试

`uv run pytest tests/test_multimodal_understand_path.py -v` —— 期望 8/8 pass。

### 3.2 全量测试

`uv run pytest tests/ -q` —— 期望 347+ passed（8 个新 case 加进去；其它不破）。

### 3.3 手工 e2e

1. 启动 MonoX runtime + MonoDesk。
2. 上传一张 PNG（drag & drop 或 file picker）→ user message bubble 出现缩略图
   （由 MonoDesk 那边 `attachment-routing.md` 走 `<img>`）。
3. 让 agent 调 multimodalunderstand → 不再 ReadTimeout；2-3s 内返回图像描述。

## 4. 文件清单

### 修改

- `core/loop/tools/multimodal_understand.py` — `__init__(attachments_root)` 注入；
  `_debug_server_path()` 反推；`execute()` 用 `effective` 路由；docstring 加 #53 引用
- `run.py` — `MultimodalUnderstandTool(attachments_root=Path(cfg.sandbox.tmp_root))`

### 新增

- `tests/test_multimodal_understand_path.py` — 8 case（happy / edge / security）
- 本 spec 文档

### 不动

- `core/debug_server.py`（audio 已返 path；image/pdf/doc 仍只返 url 暂时 OK，
  后续如果要节省前端 fetch 也可加 path 字段，但跟本 spec 解耦）
- Wire protocol（`Attachment` type）
- 前端任何代码
