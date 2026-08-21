"""Checkpoint / Memory 存储接口。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .events import CheckpointRecord


@runtime_checkable
class CheckpointStore(Protocol):
    """Checkpoint 持久化。v0: JsonlCheckpointStore; 未来可切 sqlite。"""

    async def save(self, record: CheckpointRecord) -> None: ...

    async def load_latest(self, session_key: str) -> CheckpointRecord | None: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Memory 读写。v0: FsMemoryStore; 未来可切向量库。

    设计原则：Memory.md 短小 + 索引；notes/ 子目录存实际记忆。
    """

    async def read_index(self, session_key: str) -> str:
        """返回 Memory.md 全文（含索引区）。"""
        ...

    async def write_note(self, session_key: str, name: str, content: str) -> None:
        """写入 memory/<session_key>/notes/<name>。"""
        ...

    async def update_index(self, session_key: str, content: str) -> None:
        """整体替换 Memory.md。"""
        ...

    async def append_fact(self, session_key: str, fact: str) -> None:
        """向 Memory.md 追加一条事实/摘要条目（L3 自动维护）。"""
        ...