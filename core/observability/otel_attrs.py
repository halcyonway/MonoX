"""OpenTelemetry Semantic Convention 属性名常量 + 旧 key → OTel key 映射。

OTel 没对应的 agent 特有字段（loop.compress.* / loop.cancelled 等）按
"前缀.子名" 风格扩展。整个项目所有 record_* helper、engine 落点都从这里
import 常量，不允许散落字符串字面量。

完整契约见 spec/requirements/observability-otel.md §2。

参考：
- OpenTelemetry GenAI Semantic Conventions
  https://opentelemetry.io/docs/specs/semconv/gen-ai/
- OpenTelemetry Span Kind
  https://opentelemetry.io/docs/concepts/signals/traces/#span-kind
"""
from __future__ import annotations

# ──────────── Service-level（任何 span 都可带）────────────
ATTR_SERVICE_NAME = "service.name"
ATTR_SESSION_ID = "session.id"

# ──────────── Agent / loop 自定义扩展（OTel 无对应）────────────
# 沿用 OTel 点分隔命名风格；语义上属于"agent run 的内部阶段"
ATTR_LOOP_TURN_IDX = "loop.turn.idx"
ATTR_LOOP_COMPRESS_LEVEL = "loop.compress.level"
ATTR_LOOP_COMPRESS_SUMMARY = "loop.compress.summary"
ATTR_LOOP_COMPRESS_FOLDED = "loop.compress.folded_count"
ATTR_LOOP_COMPRESS_BUDGETS = "loop.compress.budget_ids"
ATTR_LOOP_CANCELLED = "loop.cancelled"  # bool，扩展 span status 3 值用

# ──────────── OTel GenAI 语义约定（gen_ai.*）────────────
ATTR_GENAI_REQUEST_MODEL = "gen_ai.request.model"
ATTR_GENAI_REQUEST_MESSAGES = "gen_ai.request.messages"  # list[dict]
ATTR_GENAI_REQUEST_TOOL_SPECS = "gen_ai.request.tool_specs"  # list[dict]（OTel 无；扩展字段）

ATTR_GENAI_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_GENAI_RESPONSE_TEXT = "gen_ai.response.text"  # str
ATTR_GENAI_RESPONSE_REASONING = "gen_ai.response.reasoning"  # str | None
ATTR_GENAI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"  # str

ATTR_GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"  # int
ATTR_GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"  # int
ATTR_GENAI_USAGE_CACHED_TOKENS = "gen_ai.usage.cached_tokens"  # int

ATTR_GENAI_CLIENT_OPERATION_DURATION = "gen_ai.client.operation.duration"  # int (ms)
ATTR_GENAI_CLIENT_TIME_TO_FIRST_TOKEN = "gen_ai.client.time_to_first_token"  # int (ms)

# ──────────── OTel Tool 语义约定（tool.*）────────────
ATTR_TOOL_NAME = "tool.name"
ATTR_TOOL_CALL_ID = "tool.call.id"
ATTR_TOOL_CALL_ARGUMENTS = "tool.call.arguments"  # OTel 规定 string；MonoX 存 JSON string
ATTR_TOOL_RESULT = "tool.result"  # OTel 规定 string；MonoX 存 JSON string
ATTR_TOOL_RESULT_STATUS = "tool.result.status"  # 扩展（OTel 无独立字段）
ATTR_TOOL_RESULT_TRUNCATED = "tool.result.truncated"  # 扩展（OTel 无独立字段）

# ──────────── OTel Error 语义约定（error.*）────────────
ATTR_ERROR_TYPE = "error.type"
ATTR_ERROR_MESSAGE = "error.message"


# ──────────── 旧 key → OTel key 映射（迁移期参考）────────────
# 不在代码运行时使用，仅供阅读 / 文档 / 一次性迁移脚本参考。
# 完整映射见 spec/requirements/observability-otel.md §2.2。
LEGACY_TO_OTEL = {
    # reasoning span 旧 attrs
    "model": ATTR_GENAI_REQUEST_MODEL,
    "messages": ATTR_GENAI_REQUEST_MESSAGES,
    "response_text": ATTR_GENAI_RESPONSE_TEXT,
    "reasoning_content": ATTR_GENAI_RESPONSE_REASONING,
    "finish_reason": ATTR_GENAI_RESPONSE_FINISH_REASONS,
    "latency_ms": ATTR_GENAI_CLIENT_OPERATION_DURATION,
    # usage 嵌套 dict 拆 3 个顶层 attr
    "usage.prompt_tokens": ATTR_GENAI_USAGE_INPUT_TOKENS,
    "usage.completion_tokens": ATTR_GENAI_USAGE_OUTPUT_TOKENS,
    "usage.cached_tokens": ATTR_GENAI_USAGE_CACHED_TOKENS,
    # tool span 旧 attrs
    "tool_name": ATTR_TOOL_NAME,
    "args": ATTR_TOOL_CALL_ARGUMENTS,
    "result": ATTR_TOOL_RESULT,
    # compress span 旧 attrs
    "level": ATTR_LOOP_COMPRESS_LEVEL,
    "summary": ATTR_LOOP_COMPRESS_SUMMARY,
    "folded_count": ATTR_LOOP_COMPRESS_FOLDED,
    "budget_ids": ATTR_LOOP_COMPRESS_BUDGETS,
}