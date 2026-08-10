"""Context 组装 + L1 工具结果压缩。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.protocol import ToolResult


L1_TRUNCATE_LEN = 4000
L1_HALF = L1_TRUNCATE_LEN // 2


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


def compress_tool_result(result: ToolResult, budget: ReadToolResultBudgetTool) -> ToolResult:
    if len(result.stdout) <= L1_TRUNCATE_LEN and len(result.stderr) <= L1_TRUNCATE_LEN:
        return result

    budget_id = uuid.uuid4().hex[:12]
    budget.put(budget_id, result)

    truncated_stdout = (
        f"[L1 compressed, full version requires read_tool_result_budget(budget_id='{budget_id}')]\n"
        f"{result.stdout[:L1_HALF]}\n...\n{result.stdout[-L1_HALF:]}"
    )
    return ToolResult(
        call_id=result.call_id,
        status=result.status,
        stdout=truncated_stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        truncated=True,
        budget_id=budget_id,
    )


def format_tool_message(result: ToolResult) -> str:
    return json.dumps(
        {
            "status": result.status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "truncated": result.truncated,
            "budget_id": result.budget_id,
        },
        ensure_ascii=False,
    )