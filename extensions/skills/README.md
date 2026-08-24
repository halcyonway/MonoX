# extensions/skills/

git-tracked 的「公共 skill 库」。这里是 MonoX 开箱即用的 skill 模板集合，
**不需要手动安装** —— `run.py` 启动时会自动 sync 到 `.monox/skills/`（runtime
副本目录，`SkillService` 实际读的就是这里）。

## 工作流

`extensions/skills/<name>/` 是 **source of truth**（git-tracked）。要改任何
skill（写 prompt、加 helper script、调 frontmatter）都改这里。`run.py` 启动
时通过 `core.skill_sync.sync_extension_skills()` 自动把这里缺失的 skill 拷到
`.monox/skills/`，LLM 下个 turn 就能用。

**不要直接改 `.monox/skills/`** —— 下次启动 sync 会发现 extensions 没有但
runtime 没有（或者反之），按「补缺失不覆盖」策略不动；但语义上 runtime 是
副本，权威版本在 extensions。

完整 sync 语义 + 边界 case 见 [`spec/ARCHITECTURE.md` 第 9.1 节](../../spec/ARCHITECTURE.md#91-skill-source-of-truth-与-sync-语义)。

## 目录约定

每个 skill 一个子目录：

```
extensions/skills/<skill-name>/
├── SKILL.md                # LLM 读的入口（必需）
├── *.py / *.sh             # skill 配套脚本（可选）
└── templates/              # skill 私有资源（可选，如 prompt 模板）
    └── *.md
```

`SKILL.md` 顶部 YAML frontmatter：

```markdown
---
description: <一段话描述 skill 干啥 + 怎么用>
tier: 1                       # 1 = L1 自动注入 prompt；2 = L2 关键词触发
---

<body 详细 workflow>
```

完整 schema 见 `core/skill_service.py` 的 `_parse_frontmatter()`。

## sync 行为速查

| 状态 | sync 行为 |
|---|---|
| runtime 没有 extensions 里有 | 整目录拷过去（**auto-resurrect** on delete）|
| runtime 已有 | **跳过**，runtime 那份优先（用户可能改过）|
| runtime 有 extensions 没有 | 不动（用户的私人 skill）|
| extensions 目录本身不存在 | 静默 noop |
| extensions/<name>/ 没有 SKILL.md | 跳过（非法 entry，不算 skill）|
| sync 结果 | 写到 `.monox/state/skill-sync.json` |

**强制刷回 extensions 版本**：删 runtime 那份后重启即可：
```sh
rm -rf .monox/skills/i2i   # 删本地副本
python run.py config.toml  # 启动 → sync 把 extensions/i2i 重新拷过来
```

## 当前内置 skill

| skill | 说明 | tier |
|---|---|---|
| `i2i/` | 阿里云百炼 qwen-image-3.0 I2I（图生图）。模板 CRUD + 应用模板 + 实时 prompt。需要 `DASHSCOPE_API_KEY` env var。 | 1 |

## 配置项

`config.toml` 的 `[sandbox]` 段：

```toml
[sandbox]
skills_root = "./.monox/skills"        # runtime 副本（gitignored）
extensions_skills_dir = "./extensions/skills"   # 公共 skill 库（git-tracked）
```

两者都是相对路径（相对 cwd）。`run.py` 启动时自动调 sync，**一般情况下不需要手动调**。