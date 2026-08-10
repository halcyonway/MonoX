"""bash tool: 沙箱唯一执行入口。"""
from __future__ import annotations

from pathlib import Path

from core.protocol import ToolResult
from core.sandbox import BashRunner


class BashTool:
    name = "bash"
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the sandbox. The working directory defaults to the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The bash command to execute."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
                    "cwd": {"type": "string", "description": "Working directory, absolute or relative to session workspace."},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, runner: BashRunner, workspace: Path) -> None:
        self._runner = runner
        self._workspace = workspace

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        cmd = arguments["cmd"]
        timeout = int(arguments.get("timeout", 30))
        cwd_arg = arguments.get("cwd")

        if cwd_arg:
            cwd_path = Path(cwd_arg)
            cwd = cwd_path if cwd_path.is_absolute() else (self._workspace / cwd_arg).resolve()
        else:
            cwd = self._workspace

        try:
            r = await self._runner.run(cmd, cwd=cwd, timeout=timeout)
            status = "ok" if r.exit_code == 0 else "error"
            if r.exit_code == 124:
                status = "timeout"
            return ToolResult(
                call_id=call_id,
                status=status,
                stdout=r.stdout,
                stderr=r.stderr,
                exit_code=r.exit_code,
            )
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"bash tool error: {e}",
                exit_code=-1,
            )