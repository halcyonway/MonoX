"""Context 组装 + L1 工具结果压缩。

注意：tool 消息的 content 现在用 XML event 格式（见 core.loop.event_format），
不是 JSON 字符串。XML schema 文档会注入 system prompt，让 LLM 知道怎么读。
"""
from __future__ import annotations

import uuid
from typing import Any

from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.protocol import ToolResult


L1_TRUNCATE_LEN = 4000


def assemble_messages(
    system: str,
    memory_index: str,
    skill_summary: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    parts: list[str] = [system]
    if memory_index:
        parts.append(f"\n\n## Memory\n{memory_index}")
    if skill_summary:
        parts.append(f"\n\n## Available Skills\n{skill_summary}")
    return [{"role": "system", "content": "".join(parts)}] + list(messages)


def compress_tool_result(
    result: ToolResult,
    budget: ReadToolResultBudgetTool,
    limit: int = L1_TRUNCATE_LEN,
) -> ToolResult:
    if len(result.stdout) <= limit and len(result.stderr) <= limit:
        return result

    budget_id = uuid.uuid4().hex[:12]
    budget.put(budget_id, result)
    half = limit // 2

    def _truncate(text: str) -> str:
        return (
            f"[L1 compressed, full version requires read_tool_result_budget(budget_id='{budget_id}')]\n"
            f"{text[:half]}\n...\n{text[-half:]}"
        )

    return ToolResult(
        call_id=result.call_id,
        status=result.status,
        stdout=_truncate(result.stdout) if len(result.stdout) > limit else result.stdout,
        stderr=_truncate(result.stderr) if len(result.stderr) > limit else result.stderr,
        exit_code=result.exit_code,
        artifacts=result.artifacts,
        truncated=True,
        budget_id=budget_id,
    )


def format_tool_message(result: ToolResult, *, tool: str | None = None) -> str:
    """DEPRECATED：tool 消息 content 现在由 core.loop.event_format.tool_result_event_xml 生成。

    保留仅为向后兼容测试。新代码请用 tool_result_event_xml(call_id, result, tool=tool_name)。
    """
    from core.loop.event_format import tool_result_event_xml
    return tool_result_event_xml("", result, tool=tool)