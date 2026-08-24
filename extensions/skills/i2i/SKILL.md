---
description: 阿里云百炼 I2I（图生图）skill。基于 qwen-image-3.0-pro 模型，提供模板 CRUD、应用模板生成、临时 prompt 生成 + 可选保存三种模式。
tier: 1
---

# I2I skill — 阿里云百炼 图生图

调用 `dashscope.aliyuncs.com` 的 multimodal-generation endpoint，输入本地图片
（base64 data URL），返回生成图 URL（24h 有效）。模板存在本 skill 的 `templates/`
子目录，输出图存到 `<workspace>/i2i/`（默认 `.monox/workspace/i2i/`，可用
`I2I_WORKSPACE_DIR` 环境变量覆盖）。

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

## 三种使用模式

### 模式 1：模板 CRUD

模板就是一段 prompt，存在 `<skill_dir>/templates/<name>.md`，frontmatter 是
YAML metadata，body 是 prompt 文本。

```sh
python <skill_dir>/i2i.py list                          # 列所有模板（name + description）
python <skill_dir>/i2i.py show <name>                   # 打印模板全文
python <skill_dir>/i2i.py add <name> --description "..." < prompt.md
                                                       # 从 stdin 读 prompt 创建
python <skill_dir>/i2i.py edit <name>                   # 用 $EDITOR 编辑
python <skill_dir>/i2i.py rm <name>                     # 删（-y 跳过确认）
```

模板示例（`templates/photo-journal.md`）：

```markdown
---
description: 竖向照片日记卡片（上半实景照 + 下半手绘色块）
tags: [card, vintage, pastel]
---

整体竖向构图，画面垂直对半分割，上下区域严格各占 50% 画幅。
画面上半部分：载入参考图实景照片的核心画面并智能裁切...
```

### 模式 2：应用模板 → 生成图

用户提供图片 + 选模板 + 选模型 → 调 API → 输出图存到 workspace。

```sh
python <skill_dir>/i2i.py apply \
    --image /path/to/input.jpg \
    --template photo-journal \
    --model qwen-image-3.0-pro
# → 打印 saved <workspace>/i2i/20260824_223015_qwen-image-3.0-pro_photo-journal.png
# → 打印 url: https://dashscope-.../xxx.png（24h 有效）
```

可用模型（任选）：
- `qwen-image-3.0-pro`（推荐：I2I 1-3 张参考图，质量高）
- `qwen-image-3.0`（更便宜）

### 模式 3：实时 prompt → 生成 → 可选保存

用户提供图片 + 描述意图 → 你按意图自由发挥写 prompt → 调 API → 询问是否
保存成模板。

```sh
python <skill_dir>/i2i.py raw \
    --image /path/to/input.jpg \
    --prompt "<你按用户意图写的 prompt>" \
    --model qwen-image-3.0-pro
# 输出图 + url

# 如果用户说"把这个 prompt 保存成模板"，加 --save-as：
python <skill_dir>/i2i.py raw \
    --image /path/to/input.jpg \
    --prompt "..." \
    --save-as my-template \
    --save-description "用户起的中文描述"
```

## 输出图路径

默认：`<MONOX_WORKSPACE 或 .monox/workspace>/i2i/<timestamp>_<model>_<tag>.png`

- tag：apply 是模板名，raw 是 "raw" 或 `--save-as` 名
- 可用 `I2I_WORKSPACE_DIR` 环境变量整体覆盖

## 错误处理

- `DASHSCOPE_API_KEY` / `ALI_YUN_API_KEY` 都没设 → `ERROR: ... not set`
- API 返回 HTTP 401 (InvalidApiKey) → 99% 是 `ALI_YUN_API_KEY` 在用，
  改用 `DASHSCOPE_API_KEY`
- API 返回 HTTP 400 (InvalidParameter image) → 检查输入图路径 / base64 编码
- 超时 → 默认 180s，qwen-image-3.0-pro 一般 30-60s 出图