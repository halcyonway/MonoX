"""内置 tool 注册表。"""
from __future__ import annotations

from core.protocol import Tool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def add(self, tool: Tool) -> None:
        """运行期追加（装配后注册 fork_task / poll_task / cancel_task 用）。同名覆盖。"""
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        return [t.schema for t in self._tools.values()]