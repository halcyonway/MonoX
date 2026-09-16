# bash-target: bash tool 加 target 字段（前端展示用）

> 2026-09 起草。
> **用户原话**：「bash 工具加一个参数，target，10 字以内，只用于在 monodesk
> 这里展示。不然一眼看过去全是 bash，看不出在干啥，agent 自己说 target 谁狠么。」
>
> 闭环两仓库：MonoX 改 tool schema + system prompt；MonoDesk 改 ToolBlock 渲染。
> 协议 / event 层 **不动**（`ToolPending.args` 本来就全量 wire 过去，前端一直能读）。

## 1. Context

现在一个长 turn 里 agent 调 N 次 bash 工具，前端渲染就是「● BASH」一长串，
必须点开每一条才知道在执行什么命令——可读性差。

参考 git 提交 `target` / 标题的思路：让 LLM 调 bash 时**主动**写一句**人类能扫读的概括**，
前端放在 BASH label 旁边作为副标题。纯展示用：

```
● BASH  列出 workspace 内容              ok  343ms
● BASH  安装 pypdf 依赖                  ok  387ms
● BASH  跑单测                            error  23ms
```

不替代 `cmd`（cmd 仍然可点开看完整命令），只是在折叠状态下给一个 glance summary。

## 2. 设计

### 2.1 Tool schema 改动（`core/loop/tools/bash.py`）

```python
class BashTool:
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the sandbox. ...",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "..."},
                    "target": {
                        "type": "string",
                        "description": (
                            "Optional one-line human-readable summary of what this "
                            "command does, displayed in MonoDesk next to the BASH "
                            "label. Keep it under ~10 Chinese characters (or ~30 ASCII). "
                            "Example: '列出 workspace 内容' / 'run pytest' / 'install pypdf'. "
                            "NOT executed; ignored by the tool itself."
                        ),
                    },
                    "timeout": {...},
                    "cwd": {...},
                },
                "required": ["cmd"],   # ← target 不 required
                "additionalProperties": False,
            },
        },
    }
```

**关键点**：
- `target` 是 **optional**，agent 不写也不报错
- `BashTool.execute` **完全不读** `target`——bash 真执行的还是 `cmd`
- 长度**不写死**（不强制 maxLength）：用户原话「LLM 自己控制，如果真的超出了，我们 monodesk 层面做截断」
- wire 协议不动：`ToolPending.args` 本来就是 dict，target 跟着其他 args 一起进前端
- LLM context 影响：target 进 tool_calls.arguments → 进下一轮 LLM context。~10 中文字 = ~30 token，可控

### 2.2 System prompt 提示（`run.py` DEFAULT_SYSTEM_TEMPLATE）

加一段简短提示：

```text
## bash tool `target` field (MonoDesk display)

When you call `bash`, fill the optional `target` parameter with a one-line
human-readable Chinese summary of what the command does — MonoDesk shows it
next to the BASH label so the user can scan a long tool sequence at a glance.

Rules:
- Keep it under ~10 Chinese characters (or ~30 ASCII). MonoDesk truncates
  beyond that, but writing long wastes tokens.
- Describe the *intent* (what / why), not the command itself.
  ✓ "列出 workspace 内容" / "run unit tests" / "install pypdf"
  ✗ "ls -la workspace" (echoes the cmd) / "ls" (too vague)
- If unsure, skip it — `target` is optional. MonoDesk falls back to first
  ~30 chars of `cmd` when `target` is missing.
```

### 2.3 MonoDesk 渲染改动（`Conversation.tsx:ToolBlock`）

`child.args` 字段在 data model 里一直保留（之前讨论「展示不全还不如不展示」时
砍掉了 `<span className="t-args">` UI 元素，**但 data 字段没动**）。

新逻辑：

```tsx
// 1) 优先取 args.target
const target = child.args?.target as string | undefined;
// 2) 长度截断（防御：LLM 写超了不破坏布局）
const MAX_TARGET = 30;  // 留点 ASCII 余量
const targetShown = target
  ? target.length > MAX_TARGET ? target.slice(0, MAX_TARGET) + "…" : target
  : null;
// 3) fallback: 没有 target → 截 cmd 前 30 字
const cmdPreview = !targetShown && child.args?.cmd
  ? (child.args.cmd as string).replace(/\s+/g, " ").slice(0, 30)
  : null;
const summary = targetShown ?? cmdPreview;
```

UI 位置：

```
┌──────────────────────────────────────────────────┐
│ ● BASH  列出 workspace 内容         ok   343ms  │  ← 折叠态
│         ▾                                         │
└──────────────────────────────────────────────────┘
```

折叠态：label 后插入 `<span className="t-summary">{summary}</span>`（dim 灰色）。
展开态：完整 cmd + target 都显示在 body 里（target 作为 dim 头部行）。

**测 1 个 test**：target 字段被正确渲染；超长 target 被截断；fallback 到 cmd 前 30 字。

## 3. 文件清单

### MonoX 侧（2 个文件）

| 文件 | 修改 |
|---|---|
| `core/loop/tools/bash.py` | schema 加 `target` optional 字段；execute 不动 |
| `run.py` | system prompt 加「bash target field」段 |

### MonoDesk 侧（2 个文件）

| 文件 | 修改 |
|---|---|
| `src/components/Conversation.tsx` | `ToolBlock` 渲染 target 优先 / fallback cmd 截前 30 字；超长截断 |
| `src/components/Conversation.test.tsx` | 1-2 个 test：target 渲染 / 超长截断 / fallback |

### 不动

- `core/protocol/events.py`（`ToolPending.args: dict` 早就是 dict 字段，target 自然进）
- wire_frames / llm_proxy（target 走 OpenAI 标准 tool_calls 通道，无须特殊处理）
- engine.ts（child.args 字段保留，只改 UI 渲染）
- 其它 tool（fork_task / multimodalunderstand 等）这次**不**加 target——bash 是一等公民先做，
  后续 v2 看 LLM 用得多再加

## 4. 验证

### 4.1 MonoX 侧

```bash
cd MonoX
uv run pytest tests/ -q
# 期望：357 passed（不破现有 + bash target schema 不影响 tool execute 逻辑）
```

手工 e2e：
```bash
uv run python run.py
# 在 MonoDesk 发一条任务，观察 agent 调 bash 时：
# 1) LLM 填了 target → MonoDesk BASH bar 出现中文 summary
# 2) LLM 没填 target → fallback 到 cmd 前 30 字
# 3) LLM 写超长 target → 前端截断 + …
```

### 4.2 MonoDesk 侧

```bash
cd MonoDesk
npm test   # 期望：137+ passed（133 + 4 conversation）
npm run typecheck  # 不破（type 不变）
```

新 test：
- `args.target` 存在 → 渲染 `<span class="t-summary">列出 workspace 内容</span>`
- `args.target` 缺 + `args.cmd` 存在 → fallback 到 `cmd.slice(0, 30)`
- `args.target` 超 30 字 → 截断 + `…`
- `args.target` 与 `args.cmd` 都缺 → summary 段不渲染

### 4.3 端到端对比

截图前后：
- 之前：6 个 ● BASH 一字排开，全靠展开看
- 之后：6 个 ● BASH 各自带一行中文 summary，折叠态即可扫读

## 5. 关键不变量

- bash tool 的 `cmd` 行为完全不变（target 不污染执行）
- wire 协议 / event schema 不变（dict 已经够灵活）
- `target` 是 **optional**，不写不报错
- 长度 soft limit：30 字符 / MonoDesk 截断；LLM 自己写短些
- 其它 tool 不受影响

## 6. 不做（明确范围）

- **不**给其它 tool（fork_task / read_doc / multimodalunderstand / skill_load）加 target——
  v1 只 bash，v2 看 LLM 用得多再加
- **不**改 bash tool 任何运行时逻辑（cwd / timeout / 沙箱行为）
- **不**让 target 出现在 LLM context 的「压缩后 ToolResult」里——L1 压缩时 target
  作为 args 一部分会被一起压进 budget_id，agent 调 read_tool_result_budget 也只是
  拿完整 args，跟现在一样
- **不**在 wire 层加独立字段（target 走 OpenAI 标准 tool_calls，自然在 args 里）
- **不**做 i18n 字符串 / 主题（target 就是 LLM 写的原文，UTF-8 透传）
- **不**给 target 加 maxLength schema constraint（用户原话明确「LLM 自己控制」）
