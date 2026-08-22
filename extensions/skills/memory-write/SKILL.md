---
name: memory-write
description: Write to long-term memory (notes/ + Memory.md index)
metadata:
  type: skill
---

# Memory Write

写入长期记忆。

## 何时使用
- 用户明确表达要记住某事（"记住 / remember / save this"）
- 跨 session 需要保留的信息（用户偏好、项目约定、踩过的坑）
- 任务完成后的关键决策摘要

## 不要使用
- 临时性的任务上下文 → 直接用对话流
- 大段原始数据 → 写到 workspace 而不是 memory

## 写入方式

两步：
1. 把内容写入 `notes/<name>.md`
2. 更新 `Memory.md` 索引（如新增条目）

示例（LLM 通过 bash 调用）：

```bash
mkdir -p $MONOX_MEMORY_ROOT/$MONOX_SESSION_KEY/notes
cat > $MONOX_MEMORY_ROOT/$MONOX_SESSION_KEY/notes/python-version.md << 'EOF'
# Python 版本

项目用 Python 3.11，依赖管理用 uv。
EOF
```

然后更新 `Memory.md` 索引：

```bash
cat >> $MONOX_MEMORY_ROOT/$MONOX_SESSION_KEY/Memory.md << 'EOF'
- Python 版本：见 notes/python-version.md
EOF
```

## 索引原则
- Memory.md 保持短小（< 1 屏）
- 一行一条索引：`- <主题>：见 notes/<name>.md`
- 已存在的索引不要重复添加