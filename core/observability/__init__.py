"""可观测性核心：trace 数据模型 + 存储 + 收集器。

一个 Run = 一次 agent 回复（event 进来到 wait_io）。
Run 内是 span tree：Turn (LLM call) → (reasoning / act / compress) 子节点。

存储是 Protocol；文件 (JsonlTraceStore) 是默认实现，调用方不感知。
"""
from core.observability.collector import TraceCollector
from core.observability.jsonl_store import JsonlTraceStore, RunSummary
from core.observability.store import TraceStore
from core.observability.types import Run, Span, SpanKind, Turn

__all__ = [
    "Run",
    "RunSummary",
    "Span",
    "SpanKind",
    "TraceCollector",
    "TraceStore",
    "Turn",
    "JsonlTraceStore",
]