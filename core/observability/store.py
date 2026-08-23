"""Trace 存储接口（Protocol）。

调用方（TraceCollector / DebugServer）只依赖本协议，不知道也不在乎底层是
文件 / DB / 内存 / 远端。JsonlTraceStore 是默认实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.observability.types import Run


@dataclass(frozen=True)
class RunSummary:
    """轻量摘要（不含 turns / spans）。用于列表 endpoint。"""
    run_id: str
    session_key: str
    user_text: str
    start_ts: float
    end_ts: float | None
    status: str
    turn_count: int


class TraceStore(Protocol):
    """Trace 持久化接口。

    实现必须是线程 / 协程安全的；调用方可能在多个 session_key 上并发写。
    """

    async def save_run(self, run: Run) -> None:
        """增量 upsert 一个 Run（run_id 已存在则覆盖完整对象）。"""

    async def get_run(self, session_key: str, run_id: str) -> Run | None:
        """取完整 Run（含 turns/spans）。None 表示不存在。"""

    async def list_runs(self, session_key: str, limit: int = 20) -> list[RunSummary]:
        """按 start_ts 倒序拉该 session 的最近 N 条摘要。"""

    async def restore(self, session_key: str, limit: int = 20) -> list[Run]:
        """启动时拉最近 N 条完整 Run（用于启动预热 / 调试视图）。"""