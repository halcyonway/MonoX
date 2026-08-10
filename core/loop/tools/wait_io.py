"""wait_io tool: agent 主动结束当前 turn，等待外部输入。

调一次 → loop 进入 wait_io 状态 → engine 跳出 react，
等新 InboundEvent 到达后继续。
"""
from __future__ import annotations

from core.protocol import ToolResult


class WaitIoTool:
    name = "wait_io"
    schema = {
        "type": "function",
        "function": {
            "name": "wait_io",
            "description": (
                "Pause the loop and wait for external input. "
                "Call this when you need the user to reply / confirm, "
                "or when you are done and ready to receive the next message. "
                "The loop will resume when a new event arrives in the input queue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why you are pausing (shown to user).",
                    },
                },
                "additionalProperties": False,
            },
        },
    }

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout="[wait_io] loop paused, waiting for external input",
            stderr="",
            exit_code=0,
        )