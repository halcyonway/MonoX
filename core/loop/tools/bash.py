"""bash tool: 沙箱唯一执行入口。

`target` 参数（spec/requirements/bash-target.md）：

- optional，LLM 自控长度
- BashTool.execute **完全不读** —— 纯前端展示用，进 args dict 自然 wire 过去
- 不进 bash 真执行的 command 字符串，LLM context 里它跟 cmd 平级
"""
from __future__ import annotations

from pathlib import Path

from core.protocol import SandboxRunner, ToolResult


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
                    "target": {
                        "type": "string",
                        "description": (
                            "Optional one-line human-readable summary of what this command does, "
                            "displayed in MonoDesk next to the BASH label so the user can scan "
                            "a long tool sequence at a glance. Keep it under ~10 Chinese characters "
                            "(or ~30 ASCII). Example: '列出 workspace 内容' / 'run pytest' / "
                            "'install pypdf'. NOT executed; ignored by the tool itself."
                        ),
                    },
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
                    "cwd": {"type": "string", "description": "Working directory, absolute or relative to session workspace."},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, runner: SandboxRunner, workspace: Path) -> None:
        self._runner = runner
        self._workspace = workspace

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        # arguments["target"] 故意不读：target 是 display-only，
        # 跟 cmd 一起从 LLM 端过来，但 bash 真执行只用 cmd。
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