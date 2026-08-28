"""cancel_task tool：agent 主动取消异步任务。

不直接 cancel child loop——统一走 AsyncTaskManager.cancel（三种入口同一出口，
内部投 interrupt 复用 engine 协作中断）。见 requirements/async-task.md。
"""
from __future__ import annotations

import json

from core.async_task import AsyncTaskManager
from core.protocol import ToolResult


class CancelTaskTool:
    name = "cancel_task"
    schema = {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": (
                "Cancel a running async task you forked. The task is interrupted "
                "cooperatively (in-flight work is rolled back) and you receive an "
                "<event type='async-task-result'> announcing the cancellation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id to cancel."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, async_task_manager: AsyncTaskManager) -> None:
        self._mgr = async_task_manager

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        args = arguments or {}
        task_id = args.get("task_id") or ""
        if not task_id:
            return ToolResult(
                call_id=call_id, status="error", stdout="",
                stderr="task_id is required", exit_code=1,
            )
        ok = await self._mgr.cancel(task_id, reason="agent")
        return ToolResult(
            call_id=call_id,
            status="ok" if ok else "error",
            stdout=json.dumps(
                {"task_id": task_id, "cancelled": ok},
                ensure_ascii=False,
            ),
            stderr="" if ok else f"task not found or already finished: {task_id}",
            exit_code=0 if ok else 1,
        )
