# system-prompt-ref-emit：system prompt 教 LLM emit [[ref ...]] token

> **Feature**。配合 MonoDesk `evidence-chain.md` 渲染层，本 spec 改
> MonoX `run.py:DEFAULT_SYSTEM_TEMPLATE`，新增「## Evidence Chain（ref）」
> 段，教 LLM 在 final answer 文本里给关键观点打 ref token，让用户能验证来源。
>
> 协议层 + 渲染层全部在 MonoDesk 端定义（`spec/requirements/evidence-chain.md`），
> 本 spec 只动 prompt 文案。LLM 一旦按本段规则 emit，MonoDesk 端无需任何改动即可渲染。

## 1. 目标

1. **降低幻觉**：调研 / 总结 / 对比场景里，关键结论必须有 ref 标记
2. **不打断阅读**：ref 是行内 token（不是脚注、不是末尾 references 区块）
3. **prompt 自完备**：LLM 看完 prompt 就能 emit，无需额外 system reminder
4. **type 开放**：常见 type（`link` / `memory` / `snippet` / `tool`）给了示例，
   其它 type 由 LLM 自定（前端会降级渲染成 JSON dump）

## 2. 改动点

`run.py:DEFAULT_SYSTEM_TEMPLATE`（lines 63-181）在 `## Image preview` 段之后
（line 139 之后、`Tool results may be L1-compressed` 之前）插入新段
`## Evidence Chain（ref）`。

**为什么不放更靠后？** `## Image preview`（line 95-139）和 `## Evidence
Chain` 都是「输出格式约束」（不是工具用法、不是任务规则），放一起便于 LLM
聚类阅读。位置不破现有段顺序。

## 3. prompt 段文案

```text
## Evidence Chain（ref）

调研 / 总结 / 多源对比场景下，**关键结论必须给出 ref**，让用户能验证来源。

### 语法

```
[[ref id=N type=TYPE key=value ...]]
```

- 紧跟被标注的观点之后（行内）
- `id` 从 1 开始递增（同一 final answer 内唯一）
- `type` 与 key 见下方

### 什么时候 emit

- **关键结论**（不是显而易见的陈述）：✓ emit
- **常识 / 简单事实**（如「Python 是动态类型语言」）：✗ 不 emit
- **数据 / 引用 / 数字**（如「2024 年全球 AI 市场规模 X 亿」）：✓ emit
- **用户原文 / 之前对话片段**（如「你之前提到…」）：✓ emit snippet
- **闲聊 / 单步工具调用结果汇报**：✗ 不 emit（除非结果是关键决策依据）

### 常用 type

| type | 场景 | 必填字段 |
|---|---|---|
| `link` | 外部文章 / 文档 / GitHub URL | url, title |
| `memory` | 你从 memory 里读到的关键事实 | key（memory 索引）, snippet（≤ 200 字符） |
| `snippet` | 用户之前对话 / 某段上下文 | from（来源描述）, content |
| `tool` | 之前某次 tool 调用的关键返回 | tool_name, call_id, result_summary |

未识别的 type 也允许 —— 前端会降级显示所有 key=value。

### 正确示例

```
MonoX 是 2022 年成立的 AI agent runtime [1]，核心定位是自托管 ReAct 循环 [2]。
[[ref id=1 type=link url="https://monox.dev/about" title="MonoX 官网 About"]]
[[ref id=2 type=memory key="identity/monox" snippet="MonoX 2022 年成立，定位 self-hosted agent runtime"]]
```

### 错误示例

- `MonoX 是 2022 年成立的 [[ref id=1 type=link url=...]]` —— ref 应该放在观点**之后**，不是插入观点中间
- 整段文字一个 ref 也没有，但里面包含「2022 年成立」「AI agent runtime」等关键事实 —— 关键结论必须 ref
- `[[ref id=1 type=link url="..."]]` 不带 title —— 前端只显示 URL，不直观
- ref 出现在 reasoning 或 tool_call 里 —— **ref 只能出现在 final answer 的文本流**（reasoning / tool_call 里的 ref 不会被前端解析）

### 适用边界

- final answer 是 **文本流**（renderMarkdown 会扫到）；reasoning / tool_call args / system note 里出现的 ref token **不会被前端解析**（这些 channel 不走 markdown pipeline）
- 如果 final answer 里**完全没有任何可标注的来源**（如纯闲聊 / 单句回复 / 你自己推理得出的结论），整段可以零 ref—— 不要为了凑数硬塞
- 推断 / 推测（inference）标注 ref 时用 `snippet` + `from="模型推断"` 让用户知道这是模型自己的推测，不是外部来源
```

## 4. 设计决策

### 4.1 为什么放在「Image preview」段之后

- 两个段都是「输出格式约束」（不是工具用法、不是会话规则）
- LLM 在输出最终 final answer 前会扫 prompt 末段的格式说明 —— 放在靠后位置
  但仍在 `Tool results may be L1-compressed` 这种通用工具提示之前，能确保
  LLM 看完 image + ref 两套格式约束后再看上下文
- 不放最末尾的原因：最末尾是 `{MONOX_*}` 路径占位符说明，属于环境信息，
  格式约束跟它性质不同

### 4.2 为什么用「观点之后」而不是「观点之前」或脚注

- **观点之前**：`[[ref ...]] MonoX 是 2022 年...` —— 阅读时 ref 抢在结论前面，视觉顺序错乱
- **观点之后**（采用）：`MonoX 是 2022 年成立的 [1]` —— chip 紧跟被标注的关键词，符合学术 footnote 习惯
- **末尾 references 区块**：列表式总结所有 ref —— 多了一步跳转、阅读上下文被撕开
- 跟现有「`![alt](path)` 紧跟被引用的内容」原则一致 —— 都是行内随用

### 4.3 为什么 id 让 LLM 自增，不在 server 端补

- LLM 已经在 final answer 文本流里嵌入 token，server 端补需要扫描 + 替换整段文本
  —— 风险高于让 LLM 自增（漏改 / 多改都会让 chip 错位）
- LLM 自增的错误模式（id 重复 / id 跳跃）对前端是**降级渲染**（popover 内容
  仍按各自的 key=value 显示，id 重复不会崩）—— 不是阻塞错误
- 跟现有 prompt 风格一致：不依赖隐式 server 行为，LLM 自己写自洽的 token

### 4.4 为什么给「错误示例」段

LLM 在「不确定」时倾向「什么都不做」（零 ref），这是幻觉风险最大的形态。
prompt 显式列 bad case + 反例，能：
- 提醒 LLM 「关键结论必须 ref」是硬规则，不是建议
- 阻止 LLM 把 ref 写到 reasoning / tool_call 里（明示 ref 的合法 channel）

### 4.5 为什么不强制 N 个 ref / turn

- 纯闲聊 turn 不该有 ref（强制会让 LLM 编造 ref，更糟）
- 短回复（一句事实陈述）也不该有 ref（强制同上）
- 「**关键结论才 emit**」是软规则，但比「每 turn 至少 N 个 ref」更安全

## 5. 测试

### `tests/test_system_prompt.py`（新文件）

| Case | 期望 |
|---|---|
| 1. DEFAULT_SYSTEM_TEMPLATE 包含「## Evidence Chain（ref）」段 | grep 命中 |
| 2. 段里包含 `[[ref id=N type=TYPE key=value ...]]` 语法说明 | grep 命中 |
| 3. 段里包含四个 type（link / memory / snippet / tool） | grep 命中 |
| 4. 段里包含「正确示例」+「错误示例」 | grep 命中 |
| 5. 段里包含「关键结论必须 ref」硬规则 | grep 命中 |
| 6. `render_default_system(cfg)` 输出含 evidence chain 段 | 集成测试 |
| 7. 段位置在 `## Image preview` 之后、`Tool results may be L1-compressed` 之前 | 顺序断言 |
| 8. 段不破坏现有段（image preview / bash target / reading documents 等） | 现有 prompt 内容仍命中 |

### 不变量测试

- 现有所有 `tests/test_*` 全过（prompt 是输入文本，加段不会破现有 engine /
  proxy / tool 测试）
- 现有 e2e `tests/test_e2e.py` 全过

## 6. 验证

```bash
cd MonoX && uv run pytest tests/ -q
# 期望：新增 test_system_prompt.py 全过
# 现有 ~150 测试全过
```

### 手工 e2e（等用户配合）

1. 起 MonoX run.py
2. 问一个调研类问题：「MonoX 是哪年成立的？核心定位是什么？」
3. 观察 final answer：应包含 1~2 个 `[[ref id=N type=link ...]]` token
4. MonoDesk 端 freeze 后：token 渲染成 chip + popover，hover 显示 title + url

## 7. 不动

- wire 协议（ref 是文本流的一部分，不新增 wire frame）
- core/ 任何代码
- 任何 channel 实现
- 任何 tool 实现
- prompt 现有段（image preview / bash target / reading documents / path vars / async tasks）
