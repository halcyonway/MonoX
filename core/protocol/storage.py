"""Checkpoint / Memory 存储接口。"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CheckpointStore(Protocol):
    """Checkpoint 持久化——append-only log 形式。

    哲学：和 LLM 自己的对话流一样 append。每行 JSON 是一个事件：
      - `{"kind":"msg","role":"user"|"assistant"|"tool",...}`  —— 单条消息
      - `{"kind":"compact","step":K,"summary":str,"folded_count":int,"compressed_messages":[...]}`  —— L2 折叠节点

    重启 = 读 events；从最近的 compact 节点取 compressed_messages 作基底；之后按顺序
    replay msg 事件构造最终 messages 列表。**不允许 update / delete**——如果某 step 要回滚
    （interrupt），in-memory 回滚即可，checkpoint 已经写过的内容保留，但 in-memory state
    不再 reference 它们。这是有意为之：checkpoint 是历史记录，不是 state mirror。
    """

    async def append(self, session_key: str, event: dict[str, Any]) -> None:
        """追加一条事件。"""
        ...

    async def load_messages(self, session_key: str) -> list[dict[str, Any]]:
        """重建最终 messages 列表。从最近的 compact 节点开始 replay。"""
        ...


@runtime_checkable
class MemoryStore(Protocol):
    """Memory 读写——跨会话的全局长期记忆。

    设计原则：
        - `Memory.md`：放在 `<root>/Memory.md`，**跨会话**，每次调用 read_index
          就拿到最新内容，注入 system prompt。内容是稀疏索引（每行 `- topic: notes/x.md`），
          真正的细节在 `notes/<topic>.md`。
        - `notes/`：放在 `<root>/notes/`，**跨会话**，按主题一个文件。
        - 写由 LLM 通过 Bash 完成（"记住xxx" / 重要事实），属于低频、显式行为。
          本接口只暴露给运行时代码用，LLM 自身不走这些 API。
        - session_key 入参保留作兼容壳（不再用于路径分段）；传 "default" 即可。
    """

    async def read_index(self, session_key: str) -> str:
        """读取 Memory.md 全文。若文件不存在返回空串。"""
        ...

    async def write_note(self, session_key: str, name: str, content: str) -> None:
        """写入 memory/notes/<name>。name 不带后缀，由调用方负责。"""
        ...

    async def update_index(self, session_key: str, content: str) -> None:
        """整体替换 Memory.md。"""
        ...
