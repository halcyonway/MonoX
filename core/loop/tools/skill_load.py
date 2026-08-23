"""skill_load tool: 按需加载 skill 完整 SKILL.md。

薄壳——实际 IO 走 SkillService.load()。保持 tool schema 不变，LLM 视角零负担。
"""
from __future__ import annotations

from core.protocol import ToolResult
from core.skill_service import SkillService


class SkillLoadTool:
    name = "skill_load"
    schema = {
        "type": "function",
        "function": {
            "name": "skill_load",
            "description": "Load the full SKILL.md of a skill by name. Use when you need details of a specific skill before invoking it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (directory name under skills/)."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, service: SkillService) -> None:
        self._service = service

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        name = arguments["name"]
        try:
            content = self._service.load(name)
        except FileNotFoundError:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"skill not found: {name}",
                exit_code=1,
            )
        except OSError as exc:
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"failed to read skill {name!r}: {exc}",
                exit_code=1,
            )
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=content,
            stderr="",
            exit_code=0,
        )