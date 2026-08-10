"""JsonlCheckpointStore: v0 实现，未来可切 sqlite。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from core.protocol import CheckpointRecord, CheckpointStore, ToolResult


class JsonlCheckpointStore(CheckpointStore):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def save(self, record: CheckpointRecord) -> None:
        line = json.dumps(asdict(record), default=str, ensure_ascii=False)
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self._path.open("a") as f:
            f.write(line + "\n")

    async def load_latest(self, session_key: str) -> CheckpointRecord | None:
        if not self._path.exists():
            return None
        last = await asyncio.to_thread(self._read_latest, session_key)
        if last is None:
            return None
        return CheckpointRecord(
            session_key=last["session_key"],
            step_idx=last["step_idx"],
            messages=tuple(last.get("messages") or ()),
            tool_results=tuple(ToolResult(**t) for t in (last.get("tool_results") or [])),
            compressed_snapshot=last.get("compressed_snapshot"),
        )

    def _read_latest(self, session_key: str) -> dict | None:
        last: dict | None = None
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("session_key") == session_key:
                    last = data
        return last