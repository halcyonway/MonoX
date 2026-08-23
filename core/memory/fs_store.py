"""MemoryStore 文件系统实现。

跨会话全局布局：

    <root>/
        Memory.md        # 索引（短小，注入 system prompt）
        notes/           # 实际记忆文件
            <name>.md

session_key 入参为兼容壳，不再作为路径分量。
"""
from __future__ import annotations

from pathlib import Path

from core.protocol import MemoryStore


class FsMemoryStore(MemoryStore):
    def __init__(self, root: Path) -> None:
        self._root = root

    # 路径层
    def _index_path(self) -> Path:
        return self._root / "Memory.md"

    def _notes_dir(self) -> Path:
        return self._root / "notes"

    async def read_index(self, session_key: str) -> str:
        p = self._index_path()
        return p.read_text() if p.exists() else ""

    async def write_note(self, session_key: str, name: str, content: str) -> None:
        notes = self._notes_dir()
        notes.mkdir(parents=True, exist_ok=True)
        (notes / name).write_text(content)

    async def update_index(self, session_key: str, content: str) -> None:
        p = self._index_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
