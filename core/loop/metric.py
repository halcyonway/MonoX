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

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_idx": self.step_idx,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "tool_calls_count": self.tool_calls_count,
        }


@dataclass
class SessionMetric:
    steps: list[StepMetric] = field(default_factory=list)

    def add(self, metric: StepMetric) -> None:
        self.steps.append(metric)

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": len(self.steps),
            "total_latency_ms": sum(s.latency_ms for s in self.steps),
            "total_tool_calls": sum(s.tool_calls_count for s in self.steps),
        }