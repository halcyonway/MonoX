# attachment-local-path: wire frame 只传本地 path，不暴露 debug server URL

> Attachment 在 wire frame 上只携带本地绝对 path（`path` 字段），不暴露
> `http://127.0.0.1:<port>/debug/attachments/...` URL。LLM 拿到 attachment 后
> 直接拿 `path` 调 read_doc / multimodalunderstand，工具走本地 IO；前端拿 `path`
> 走 Tauri asset protocol 渲染。三方各取所需。

## 1. Context

之前 attachment 在 wire frame 上同时带 `url` 和 `path`：

- `url` 是 debug server `http://127.0.0.1:8768/debug/attachments/<disk-file>`。
  LLM 拿到后要么用 url 反推本地 path（_debug_server_path，复杂且易错），
  要么直接传给工具（read_doc 报「path looks like a URL」）。
- `path` 是音频 attachment 专属字段，image / pdf / doc 不填。

**两个问题**：

1. **跨工具的 path 解析各做各的**：multimodal_understand / read_doc / asr skill
   各自判断 url 是不是 debug server 的、各自反推 path、各自处理 path traversal。
   同一份 url 三处解析逻辑。
2. **wire frame 上暴露 `127.0.0.1:8768`**：系统是 desktop local 的，wire 上不该
   出现 loopback URL。LLM prompt / attachment XML / tool error 信息里写
   `http://127.0.0.1:...` 是泄漏内部实现。

## 2. 设计

### 2.1 upload → 返回 `{path, name, mime, kind}`（无 url）

`core/debug_server.py::_handle_attachment_upload` 的 payload 只含：

```python
{
    "path": str(saved_path),         # 本地绝对路径
    "name": display_name,            # 原名（中文 / 空格保留）
    "mime": mime,
    "kind": kind,                    # image / audio / other
}
```

**disk 文件名** = sanitize 后的原名 + ext（不拼 uuid，不重命名）。

```python
filename = f"{original_name}{ext}" if original_name else f"{uuid.uuid4().hex}{ext}"
```

`original_name` 来自 `X-Filename` header（浏览器 raw body 上传没法用
Content-Disposition）。`_sanitize_filename` 剥路径分隔符 + 不可打印字符，保留
中文 / 空格 / emoji。

无原名时（drag without filename 边界 case）退回到 `<uuid><ext>`，避免重名覆盖。

### 2.2 wire frame 只透传 path

`core/protocol/wire_frames.py`：

- **frame_to_inbound**（client → runtime）：attachment 必备 `path` 字段。无
  `path` 直接跳过（不是有效 attachment）。`File.content` 不再写 url 字节。
- **inbound_to_frame**（runtime → channel）：attachment 输出 `{path, name,
  mime, kind}`，不写 `url`。

`core/loop/event_format.py::_attachment_xml` 渲染 `<attachment path="..."/>`
（不是 `url="..."`），LLM context 里只看到本地 path。

### 2.3 工具不再做 URL 反推

- **multimodalunderstand**：撤回 `_debug_server_path()`。`execute()` 直接传
  path 给 `_call_vision`，_call_vision 内部走 `_image_b64(open(path))`。不再
  注入 `attachments_root`，不再有反推逻辑。
- **read_doc**：维持 path / url 拦截原状，错误信息改为「use the `path` field
  from the attachment element」。

### 2.4 前端渲染

`MonoDesk` Conversation 收到 attachment（`{path, name, mime}`）后用
`convertFileSrc(path)`（Tauri asset protocol）转 `asset://...`，`<img>` 直接渲染。
Dev browser fallback `file://${path}`。

## 3. 不变量

- LLM 视角的 attachment 永远是 `<attachment path="..." />`，不再含 `http://`
- 磁盘文件名 = 原名（中文 / 空格保留），LLM 直接 `<attachment path="..." />`
  读到的就是用户上传的文件本身
- 上传通道保留在 debug server（独立进程的 IPC 通道，bytes 必须经过 HTTP
  POST），但响应 payload 不再含 wire-visible URL
- `read_doc` URL 拦截逻辑保留（防御性，挡 internet URL）

## 4. 文件清单

### 修改

- `core/debug_server.py` — `disk_name` 改原名 + ext；payload 不含 url；`X-Filename`
  header 解析
- `core/protocol/wire_frames.py` — `frame_to_inbound` 要求 path；`inbound_to_frame`
  输出不含 url；`_file_to_dict` path 独立字段
- `core/loop/event_format.py` — `_attachment_xml` 渲染 `path` 属性
- `core/loop/tools/multimodal_understand.py` — 撤回 `_debug_server_path` 反推
- `core/loop/tools/read_doc.py` — URL 拦截错误信息更新
- `core/loop/event_format.py` EVENT_SCHEMA_DOC — attachment 说明用 path
- `run.py` — system prompt 改用 path；`MultimodalUnderstandTool()` 不再传
  `attachments_root`
- `MonoDesk/src/ws/protocol.ts` — `Attachment.url` → `Attachment.path`
- `MonoDesk/src/components/Composer.tsx` — 上传读 `json.path`
- `MonoDesk/src/components/Conversation.tsx` — `<img src={convertFileSrc(path)}>`
- `MonoDesk/src/components/Composer.tsx` — `VITE_DEBUG_URL` → `VITE_MONOX_UPLOAD_URL`

## 5. 验证

### 5.1 e2e 手工

1. 启动 MonoX runtime + MonoDesk。
2. 上传 `心洲科技公司介绍 .pdf` → bubble 显示文件名 + pdf icon（不是 broken image）
3. 让 agent 读 PDF → LLM 调 `read_doc(path="<attachment path>")` → 读出 PDF 文字
4. wire frame 日志（debug server 开启）确认 attachment 是 `<attachment path="..." />`

### 5.2 不再 127

```bash
grep -rn "127.0.0.1" core/loop/event_format.py run.py
```

LLM 视角可见的文件里无 127 字样。