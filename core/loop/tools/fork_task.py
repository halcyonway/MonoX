"""fork_task tool：fork 一个异步任务（subagent），立即返回 task_id。

parent_session_key 来自 engine 的 session contextvar（见 core/loop/engine.py 模块头
与 requirements/async-task.md「parent_session_key 从哪来」）——嵌套 fork 自动正确。
"""
from __future__ import annotations

import json

from core.async_task import (
    AsyncTaskManager,
    DEFAULT_TIMEOUT_SEC,
    MAX_TIMEOUT_SEC,
)
from core.loop.engine import current_session_key
from core.protocol import ToolResult


class ForkTaskTool:
    name = "fork_task"
    schema = {
        "type": "function",
        "function": {
            "name": "fork_task",
            "description": (
                "Fork an async task that runs independently in the background. "
                "Returns immediately with a task_id.\n\n"
                "- kind='subagent' (default): another agent instance with a fresh "
                "context and your full tool registry (including fork_task itself, "
                "supporting nesting). `description` becomes its first user turn.\n"
                "- kind='bash_long': run `command` in the background (long builds, "
                "test suites). Its output becomes the task result.\n\n"
                "When the task completes, you receive an <event kind='system' "
                "event_type='async-task-result'> in your input. You can also call "
                "poll_task() at any time, or cancel_task(task_id=...) to abort."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Task description (first user turn for subagent; UI label for bash_long).",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["subagent", "bash_long"],
                        "description": "Task kind. Default 'subagent'.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to run (required when kind=bash_long).",
                    },
                    "meta": {
                        "type": "object",
                        "description": "Arbitrary kv for tagging / UI display.",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": f"Timeout. Default {DEFAULT_TIMEOUT_SEC:.0f}, max {MAX_TIMEOUT_SEC:.0f}.",
                    },
                },
                "required": ["description"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, async_task_manager: AsyncTaskManager) -> None:
        self._mgr = async_task_manager

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        args = arguments or {}
        kind = args.get("kind") or "subagent"
        command = (args.get("command") or "").strip() or None
        description = (args.get("description") or "").strip()
        if kind == "bash_long":
            if not command:
                return ToolResult(
                    call_id=call_id, status="error", stdout="",
                    stderr="command is required when kind=bash_long", exit_code=1,
                )
            description = description or command.splitlines()[0][:120]
        if not description:
            return ToolResult(
                call_id=call_id, status="error", stdout="",
                stderr="description is required", exit_code=1,
            )
        parent_sk = current_session_key()
        if not parent_sk:
            return ToolResult(
                call_id=call_id, status="error", stdout="",
                stderr="fork_task called outside a session context", exit_code=1,
            )
        timeout_raw = args.get("timeout_sec")
        try:
            timeout = float(timeout_raw) if timeout_raw is not None else None
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call_id, status="error", stdout="",
                stderr=f"invalid timeout_sec: {timeout_raw!r}", exit_code=1,
            )
        try:
            task = await self._mgr.start(
                description=description,
                parent_session_key=parent_sk,
                meta=args.get("meta") or {},
                kind=kind,
                command=command,
                timeout_sec=timeout,
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call_id, status="error", stdout="", stderr=str(exc), exit_code=1,
            )
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=json.dumps({
                "task_id": task.task_id,
                "status": task.status,
                "timeout_sec": task.timeout_sec,
            }, ensure_ascii=False),
            stderr="",
            exit_code=0,
        )
