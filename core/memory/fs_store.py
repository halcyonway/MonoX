"""MemoryStore 文件系统实现。

目录结构：
    <root>/<session_key>/
        Memory.md        # 索引（短小、基本不变）
        notes/           # 实际记忆文件
            <name>.md
"""
from __future__ import annotations

from pathlib import Path

from core.protocol import MemoryStore


class FsMemoryStore(MemoryStore):
    def __init__(self, root: Path) -> None:
        self._root = root

    def _index_path(self, session_key: str) -> Path:
        return self._root / session_key / "Memory.md"

    def _notes_dir(self, session_key: str) -> Path:
        return self._root / session_key / "notes"

    async def read_index(self, session_key: str) -> str:
        p = self._index_path(session_key)
        return p.read_text() if p.exists() else ""

    async def write_note(self, session_key: str, name: str, content: str) -> None:
        notes = self._notes_dir(session_key)
        notes.mkdir(parents=True, exist_ok=True)
        (notes / name).write_text(content)

    async def update_index(self, session_key: str, content: str) -> None:
        p = self._index_path(session_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    async def append_fact(self, session_key: str, fact: str) -> None:
        p = self._index_path(session_key)
        p.parent.mkdir(parents=True, exist_ok=True)

        text = p.read_text() if p.exists() else ""

        if "## Conversation Summaries" not in text:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n## Conversation Summaries\n"

        bullet = "- " + fact.replace("\n", "\n  ")
        text += bullet + "\n"
        p.write_text(text)