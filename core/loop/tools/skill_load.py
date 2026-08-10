"""skill_load tool: 按需加载 skill 完整 SKILL.md。"""
from __future__ import annotations

from pathlib import Path

from core.protocol import ToolResult


class SkillLoadTool:
    name = "skill_load"
    schema = {
        "type": "function",
        "function": {
            "name": "skill_load",
            "description": "Load the full SKILL.md of a skill by name. Use when you need details of a specific skill before invoking it via bash.",
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

    def __init__(self, skills_root: Path) -> None:
        self._skills_root = skills_root

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        name = arguments["name"]
        skill_md = self._skills_root / name / "SKILL.md"
        if not skill_md.exists():
            return ToolResult(
                call_id=call_id,
                status="error",
                stdout="",
                stderr=f"skill not found: {name}",
                exit_code=1,
            )
        return ToolResult(
            call_id=call_id,
            status="ok",
            stdout=skill_md.read_text(),
            stderr="",
            exit_code=0,
        )