"""扫描 skills 目录，生成 system prompt 用的 skill 摘要列表。"""
from __future__ import annotations

from pathlib import Path


class SkillSummaryLoader:
    def __init__(self, skills_root: Path) -> None:
        self._root = skills_root

    def summary(self) -> str:
        if not self._root.exists():
            return ""
        lines: list[str] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            md = entry / "SKILL.md"
            if not md.exists():
                continue
            first_line = md.read_text().strip().split("\n", 1)[0].lstrip("# ").strip()
            lines.append(f"- `{entry.name}`: {first_line}")
        return "\n".join(lines)