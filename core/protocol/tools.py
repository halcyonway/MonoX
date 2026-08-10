"""Tool 接口。所有内置 tool 实现此协议。"""
from __future__ import annotations

from typing import Any, Protocol

from .events import ToolResult


class Tool(Protocol):
    """内置基础 tool 的接口契约。

    Loop 通过 name + schema 注入到 LLM tools 参数，
    通过 execute(call_id, arguments) 调度执行。
    """

    name: str
    schema: dict[str, Any]

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult: ...