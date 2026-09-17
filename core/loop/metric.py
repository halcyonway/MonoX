"""Step / Session metric 数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepMetric:
    step_idx: int
    latency_ms: int = 0
    tokens: dict[str, Any] | None = None
    tool_calls_count: int = 0
    # 首个有效 token 落地的服务端耗时（ms，从 LLMProxy.stream() 内 POST 发出
    # 到第一个非空 delta_text / delta_reasoning 收到）。Otel:
    # gen_ai.client.time_to_first_token。None 表示未采集到（首 chunk 里没
    # 内容、或 stream 0 字节异常退出）。
    ttft_ms: int | None = None
    # 本次 LLM 调用实际下发的 OpenAI function schema 列表——塞进 reasoning
    # span 的 gen_ai.request.tool_specs 让 trace 能看到「这次 LLM 允许用
    # 哪些 tool」。可能为空（无 tool 能力的 LLM 调用）。
    tool_schemas: list[dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_idx": self.step_idx,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "tool_calls_count": self.tool_calls_count,
            "ttft_ms": self.ttft_ms,
            "tool_schemas": self.tool_schemas,
        }


@dataclass
class SessionMetric:
    steps: list[StepMetric] = field(default_factory=list)

    def add(self, metric: StepMetric) -> None:
        self.steps.append(metric)

    def drop_last(self) -> None:
        if self.steps:
            self.steps.pop()

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": len(self.steps),
            "total_latency_ms": sum(s.latency_ms for s in self.steps),
            "total_tool_calls": sum(s.tool_calls_count for s in self.steps),
        }