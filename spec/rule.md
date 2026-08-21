# 项目规则

## 架构分层

```
core/           稳定内核。零 UI / 零 IM / 零 LLM SDK 依赖。
extensions/     可重写的适配层。channel adapter、skill、tool 注册入口。
run.py          装配脚本（顶层，不属于 core 也不属于 extension）。
```

**系统 = 模块 + 协议。**

- 模块有边界、可替换；协议是模块间稳定契约（`Protocol` + frozen dataclass）。
- 模块只通过协议交互，不直接依赖具体实现；替换实现不改协议，就不动其他模块。

**core 是 stable kernel，不为单个 channel / skill / LLM 妥协。**

- core 只依赖：`httpx`（LLMProxy stream）、`tomli`（py<3.11 兼容）
- core 不知道 rich / prompt_toolkit / textual / feishu / openai SDK 存在
- core 接口用 `Protocol` + 抽象 `dataclass(frozen=True)` 事件 / 数据结构
- core 改动必须先看 `requirements/`，确认是稳定 API 改动而非 adapter 该做的事

**extensions/ 是 adapter 集合。**

- 新 channel：实现 `Channel` Protocol，挂在 `extensions/channels/`，`run.py` 加一个 `kind`
- 新 tool：`core.loop.tool_registry` 注册或 `extensions/skills/<name>/SKILL.md` skill 描述
- 新 LLM API：改 `config.toml` 三个字段 + 必要时 `[llm.extra_params]` 透传；不改 core

**装配在 `run.py`，不属于 core。**

- core / extensions 都不应该自己组装完整 runtime
- run.py 是用户实际入口；切 channel / session_key 都通过这里

## 注释与文档原则

**少写注释。代码即注释。**

- 不为「显而易见」的代码写注释（变量名 / 类型已经清楚时）
- 注释只在三处用：模块顶 docstring（做什么 + 为什么）、关键决策的 why（非细节（what）、公开 API 的 contract
- 中文注释 OK（项目是中文语境），但英文 / 中文混排时统一术语（如 `core` 不译、`channel` 不译、`adapter` 不译）
- README / spec 写中文；代码标识符英文

## 接口约定

- 事件 / 数据结构用 `@dataclass(frozen=True)`，不可变
- 异步优先（`async def`），IO 路径不阻塞 event loop
- `Protocol` + runtime_checkable 走鸭子类型；不强制继承
- 错误用返回 status 字段，不用异常控制流（tool_result / inbound_event）

## 验证

- 任何改动后跑 `uv run python tests/test_e2e.py`，4/4 必须过
- 新增 channel / skill 自带 `tests/test_*.py` smoke test
- 不在 commit 时跑长任务；CI 跑 e2e 即可

## 暂未立的规则（等有需求再加）

- commit message 风格（目前是 Conventional Commits 风格，但不强制）
- 版本号策略（目前按 v0.x 迭代命名 commit，tag 还没用）
- 文档站点（当前 spec/ 是 md，不打算建独立站点）