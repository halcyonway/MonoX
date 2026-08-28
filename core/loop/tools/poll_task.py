"""poll_task tool：查询异步任务状态 / 摘要（不带 ws 也能看）。"""
from __future__ import annotations

import json

from core.async_task import EVENT_BUFFER_SIZE, AsyncTaskManager
from core.protocol import ToolResult

_POLL_EVENT_TAIL = 10  # 每个 task 附带最近 10 条事件摘要


class PollTaskTool:
    name = "poll_task"
    schema = {
        "type": "function",
        "function": {
            "name": "poll_task",
            "description": (
                "Poll async task(s) you forked: status, result text / error, and "
                "a short tail of recent events. Omit task_ids to list all tasks "
                "(optionally filtered by status)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids to poll. Omit to list all tasks.",
                    },
                    "status": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["pending", "running", "completed", "failed",
                                     "cancelled", "timed_out", "interrupted"],
                        },
                        "description": "Status filter (only when listing without task_ids).",
                    },
                },
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, async_task_manager: AsyncTaskManager) -> None:
        self._mgr = async_task_manager

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        args = arguments or {}
        task_ids = [t for t in (args.get("task_ids") or []) if isinstance(t, str)]
        if task_ids:
            tasks = [t for t in (self._mgr.get(tid) for tid in task_ids) if t is not None]
        else:
            # 不带 task_ids → 全量列表（与 MonoDesk Tasks 面板同口径；任务列表是
            # 全局 UI 状态，跨 session 可见。显式 scope 走 status 过滤即可）
            tasks = self._mgr.list(status=args.get("status") or None)

        out = []
        for t in tasks:
            item = t.summary()
            if t.status == "running":
                _, recent = self._mgr.snapshot(t.task_id) or ("", [])
                item["recent_events"] = recent[-_POLL_EVENT_TAIL:]
            out.append(item)

        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=json.dumps(
                {"tasks": out, "buffer_size": EVENT_BUFFER_SIZE},
                ensure_ascii=False,
                default=str,
            ),
            stderr="",
            exit_code=0,
        )
