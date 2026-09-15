---
description: 阿里云百炼 I2I（图生图）skill。基于 qwen-image-3.0-pro 模型，提供模板 CRUD、应用模板生成、临时 prompt 生成 + 可选保存三种模式。通过 exec_cli mono_i2i 调用。
tier: 1
---

# I2I skill — 阿里云百炼 图生图

调用 `dashscope.aliyuncs.com` 的 multimodal-generation endpoint，输入本地图片
（base64 data URL），返回生成图 URL（24h 有效）。模板存在本 skill 的 `templates/`
子目录，输出图存到 `<workspace>/i2i/`（默认 `.monox/workspace/i2i/`，可用
`I2I_WORKSPACE_DIR` 环境变量覆盖）。

## 调用方式

通过 `exec_cli` 调 CLI server（端口 8769，本机常驻）：

```sh
exec_cli mono_i2i list                                         # 列所有模板
exec_cli mono_i2i show <name>                                  # 打印模板全文
exec_cli mono_i2i add <name> --description "..." --prompt "<prompt 正文>" [--tags t1 t2] [--force]
                                                                # 创建模板
exec_cli mono_i2i rm <name> [-y]                               # 删（-y 跳过确认）
exec_cli mono_i2i apply --image <path> --template <name> [--model ...]
                                                                # 应用模板 → 生成图
exec_cli mono_i2i raw --image <path> --prompt "..." [--model ...] [--save-as <name>] [--save-description "..."]
                                                                # 实时 prompt → 生成图（可选保存成模板）
```

`edit` 子命令在 CLI server 模式下不支持（server 没有 TTY / `$EDITOR`），需要编辑模板时直接改文件：
`<repo>/extensions/cli/i2i/templates/<name>.md` 或 `.monox/skills/i2i/templates/<name>.md`（运行时副本）。

## API key

需要在 zshrc 里设：

```sh
export DASHSCOPE_API_KEY=sk-...   # 主用（model API 通用 key）
export ALI_YUN_API_KEY=sk-sp-...   # fallback（百炼 app key，不能调 model API）
```

`i2i.py` 优先用 `DASHSCOPE_API_KEY`，没有再 fallback 到 `ALI_YUN_API_KEY`。

**重要**：`ALI_YUN_API_KEY` 虽然你设了，但它是 Bailian app key（`sk-sp-` 前缀），
**不能**直接调 model API，会返回 `InvalidApiKey`。脚本里 fallback 是为了让你
看到清晰错误信息而不是崩溃；如果你看到 fallback 路径失败，记得改用
`DASHSCOPE_API_KEY`。

## 耗时与并发

**单次 30-60s 出图。** `apply` / `raw` 走 `qwen-image-3.0-pro` 模型，实测 30-60s 出图。
**用 `fork_task`，不要用 `bash`。** system prompt 的 `## Async tasks` 段讲了 fork /
poll / cancel 完整生命周期——fork 一个 subagent 调 `exec_cli mono_i2i apply ...`，
父 turn 不阻塞，子任务完成时通过 `<event kind='system' event_type='async-task-result'>`
自动唤醒。

**多模板并行生成：** 用户给 N 张图 / N 个模板要同时出 N 张图时，**一次 assistant
turn fork N 个 subagent**（N 个并行 tool call），不要串行 fork。完成后等 N 个
`async-task-result` 事件一起回来，统一汇报给用户。

**例：并行生成 3 张图：**

```python
# 同一个 assistant turn 里 3 个并行 fork_task 调用：
fork_task(description="用 photo-journal 模板处理 input.jpg",
          kind="subagent",
          meta={"kind": "i2i_apply", "template": "photo-journal"})
# → {"task_id": "t1", ...}

fork_task(description="用 watercolor-soft 模板处理 input.jpg",
          kind="subagent",
          meta={"kind": "i2i_apply", "template": "watercolor-soft"})
# → {"task_id": "t2", ...}

fork_task(description="用 polaroid-vintage 模板处理 input.jpg",
          kind="subagent",
          meta={"kind": "i2i_apply", "template": "polaroid-vintage"})
# → {"task_id": "t3", ...}

# 父 turn 立即返回；3 个 subagent 并行跑（各自 30-60s）；
# 等 3 个 async-task-result 事件逐个到达，汇总 3 个 saved_path 告诉用户。
```

`meta={"kind": "i2i_apply", "template": ...}` 是给 MonoDesk 看板用的——用户能直观
看到"正在并行处理 3 张图"。

## 输出与预览

`mono_i2i apply` / `mono_i2i raw` 返回两个图引用：

| 字段 | 含义 | 能直接预览？ |
|---|---|---|
| `saved_path` | 本地绝对路径（`workspace/i2i/<ts>_<model>_<tag>.png`） | ❌ `file://` 在 MonoDesk 被 CORS 拦，绝对路径浏览器 fetch 不到 |
| `image_url` | 阿里云 OSS 公网 URL，24h 有效 | ✅ 直接 `![alt](image_url)` 即可 |

**默认嵌 OSS URL（24h 内）：**

```markdown
![风格化结果](https://dashscope-...xxx.png)
```

**如果用户要长期保留（>24h）：** OSS 链接会过期，需要把 `saved_path` 上传到 debug
server 拿永久 URL：

```sh
curl -s -X POST --data-binary @"<saved_path>" \
     -H "Content-Type: image/png" \
     http://127.0.0.1:8768/debug/attachments/upload
# → {"url": "http://127.0.0.1:8768/debug/attachments/<uuid>.png", "kind": "image"}
```

然后：

```markdown
![风格化结果（永久）](http://127.0.0.1:8768/debug/attachments/<uuid>.png)
```

**绝对不要** 只输出"输出路径：/Users/.../xxx.png"——MonoDesk 不会渲染，要 markdown
语法 `![alt](url)` 才会。详见 system prompt 的 `## Image preview` 段。

## 三种使用模式

### 模式 1：模板 CRUD

模板就是一段 prompt，存在 `<repo>/extensions/cli/i2i/templates/<name>.md`，
frontmatter 是 YAML metadata，body 是 prompt 文本。

CLI 输出格式（`list`）：

```json
{"ok": true, "data": {"templates": [{"name": "...", "description": "..."}, ...], "count": N}}
```

### 模式 2：应用模板 → 生成图

用户提供图片 + 选模板 + 选模型 → 调 API → 输出图存到 workspace。

CLI 输出格式（`apply`）：

```json
{
  "ok": true,
  "data": {
    "saved_path": "/path/to/output.png",
    "saved_bytes": 1234567,
    "image_url": "https://dashscope-.../xxx.png",
    "model": "qwen-image-3.0-pro",
    "template": "photo-journal",
    "template_description": "...",
    "key_used": "DASHSCOPE_API_KEY",
    "usage": {...}
  }
}
```

可用模型：
- `qwen-image-3.0-pro`（推荐：I2I 1-3 张参考图，质量高）
- `qwen-image-3.0`（更便宜）

### 模式 3：实时 prompt → 生成 → 可选保存

用户提供图片 + 描述意图 → 你按意图自由发挥写 prompt → 调 API → 可选 `--save-as` 保存成模板。

## 输出图路径

默认：`<MONOX_WORKSPACE 或 .monox/workspace>/i2i/<timestamp>_<model>_<tag>.png`

- tag：apply 是模板名，raw 是 "raw" 或 `--save-as` 名
- 可用 `I2I_WORKSPACE_DIR` 环境变量整体覆盖

## 错误处理

| 现象 | 处理 |
|---|---|
| `DASHSCOPE_API_KEY` / `ALI_YUN_API_KEY` 都没设 | export 后重试 |
| `dashscope HTTP 401` | 99% 是 `ALI_YUN_API_KEY` 在用，改用 `DASHSCOPE_API_KEY` |
| `dashscope HTTP 400` | 检查输入图路径 / base64 编码 |
| 超时 | 默认 180s，qwen-image-3.0-pro 一般 30-60s 出图 |
| `image not found` | 路径不对，先 `ls` 确认 |
| `edit not supported in CLI server mode` | 直接编辑 `templates/<name>.md` 文件 |
| `exec_cli: cannot reach CLI server at ...` | 先 `uv run python -m extensions.cli.inner.server &` 起 server |
