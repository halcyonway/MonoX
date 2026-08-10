"""read_tool_result_budget tool: 按需读取被 L1 压缩的工具结果完整版。

配合 Loop 的 L1 压缩：
- 压缩时把原始 ToolResult 存入 _budgets，附 budget_id
- LLM 看到 ToolResult.truncated=True + budget_id 时可调本 tool 取完整版
"""
from __future__ import annotations

from core.protocol import ToolResult


class ReadToolResultBudgetTool:
    name = "read_tool_result_budget"
    schema = {
        "type": "function",
        "function": {
            "name": "read_tool_result_budget",
            "description": "Read the full content of a tool result that was compressed by L1 compression. Use when you need details beyond the truncated summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget_id": {"type": "string", "description": "The budget_id from a truncated tool result."},
                },
                "required": ["budget_id"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self) -> None:
        self._budgets: dict[str, ToolResult] = {}

    def put(self, budget_id: str, result: ToolResult) -> None:
        self._budgets[budget_id] = result

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        budget_id = arguments["budget_id"]
        original = self._budgets.get(budget_id)
        if original is None:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"budget not found: {budget_id}",
                exit_code=1,
            )
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=original.stdout,
            stderr=original.stderr,
            exit_code=original.exit_code,
        )