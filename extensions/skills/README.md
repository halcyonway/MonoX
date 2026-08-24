# extensions/skills/

git-tracked 的「公共 skill 库」。这里放的是 MonoX 开箱即用的 skill 模板，
用户可以按下面任意一种方式装到本地运行时：

## 安装方式

### 方式 A：拷贝（推荐，最简单）

```sh
# 单 skill
cp -r extensions/skills/i2i .monox/skills/

# 全部
cp -r extensions/skills/* .monox/skills/
```

拷贝后这些 skill 立刻被 `SkillService` 发现（`<skills_root>/<name>/SKILL.md`
是 MonoX 的约定），重启 MonoX 后 LLM 就能用。

### 方式 B：软链（开发时常用，源文件改了立即生效）

```sh
ln -s "$(pwd)/extensions/skills/i2i" .monox/skills/i2i
```

### 方式 C：直接改 `skills_root`

在 `config.toml` 里把 `skills_root` 指到 extensions/skills/：

```toml
[sandbox]
skills_root = "./extensions/skills"
```

适合想完全用 git-tracked 版本的场景（个人 skill 仍可放在 `.monox/skills/`，
MonoX 会同时扫两个 root —— 但当前实现是单一 root，需要扩展才能两个都扫，
见 [MonoX PR #2 局限](#monox-pr-2-局限)）。

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

## 当前内置 skill

| skill | 说明 | tier |
|---|---|---|
| `i2i/` | 阿里云百炼 qwen-image-3.0 I2I（图生图）。模板 CRUD + 应用模板 + 实时 prompt。需要 `DASHSCOPE_API_KEY` env var。 | 1 |

## MonoX PR #2 局限

`SkillService` 当前只读 `config.toml` 里 `sandbox.skills_root` 单一 root。
如果用户想同时用 git-tracked 的公共 skill + 自己的私人 skill，需要：

1. 把 `skills_root` 改成 list（schema 升级）
2. `SkillService` 启动时按顺序扫多个 root，名字冲突时优先前面的

短期 workaround：把 `extensions/skills/*` 拷到 `.monox/skills/`，私人 skill
也放在 `.monox/skills/`，两者平铺混在一起（skill name 不能冲突）。