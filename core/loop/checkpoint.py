"""JsonlCheckpointStore: append-only log 实现。

哲学和 LLM 自己的对话流一样 append：
- `{"kind":"msg", "role": "user"|"assistant"|"tool", ...}`  —— 单条消息
- `{"kind":"compact", "step":K, "summary":str, "folded_count":int,
    "compressed_messages":[...]}`  —— L2 折叠节点

每条事件独立成行；restore 时从最近的 compact 节点开始 forward-replay msg 事件。
**不允许** update / delete。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from core.protocol import CheckpointStore


class JsonlCheckpointStore(CheckpointStore):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, session_key: str, event: dict[str, Any]) -> None:
        # session_key 写在每行 event 里——多 session 共享一份 jsonl 也能区分
        # （目前设计上每个 session 自己一份 jsonl，所以其实冗余；但留着方便合并 / 调试）
        rec = {"_session_key": session_key, **event}
        line = json.dumps(rec, default=str, ensure_ascii=False)
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        with self._path.open("a") as f:
            f.write(line + "\n")

    async def load_messages(self, session_key: str) -> list[dict[str, Any]]:
        events = await asyncio.to_thread(self._read_events, session_key)
        # 从最近的 compact 节点开始 replay；如无 compact 则从开头
        messages: list[dict[str, Any]] = []
        for ev in events:
            kind = ev.get("kind")
            if kind == "compact":
                messages = list(ev.get("compressed_messages") or [])
            elif kind == "msg":
                role = ev.get("role")
                if role not in ("user", "assistant", "tool"):
                    continue
                # 把 storage-only 字段剥掉，还原原始 message dict
                msg = {k: v for k, v in ev.items() if k not in ("_session_key", "kind")}
                messages.append(msg)
        return messages

    def _read_events(self, session_key: str) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_session_key") == session_key:
                    out.append(data)
        return out
