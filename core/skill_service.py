"""SkillService — skill 系统的唯一入口。

设计：
- skill 是一段可复用知识，存在 `<skills_root>/<name>/SKILL.md`。
- 两 tier：**L1**（自动注入 system prompt）+ **L2**（agent grep 发现）。
- 默认 tier=1；通过 SKILL.md 顶部 YAML frontmatter `tier: 2` 降到 L2。
- **`abstract()` 每次调用都重扫文件系统**——agent 在 session 内创建 skill 后下一个 turn 立即生效。
- frontmatter 用手写 regex 解析（不引入 PyYAML）。

SKILL.md 格式：
    ---
    name: daily-report              # optional, fallback = 目录名
    description: 生成每日工作日报   # optional, fallback = H1 行
    tier: 1                         # optional, default 1
    ---

    # Daily Report

    正文...
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("monox.skill_service")


# 顶层 `---` 单独一行作为 frontmatter 分隔；body 内 `---` 不会匹配（必须从文件开头起）。
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class SkillAbstract:
    """单个 skill 的 metadata（注入 prompt 用 + agent 引用）。"""
    name: str             # 目录名 = skill_id；agent 用 skill_load(name=...) 引用
    description: str      # 单行，给 LLM 看
    tier: int             # 1 or 2
    path: Path            # <skills_root>/<name>/

    def __post_init__(self) -> None:
        # tier 兜底：任何非 1/2 的值都规范成 1，避免脏数据击穿 max_l1 逻辑
        if self.tier not in (1, 2):
            object.__setattr__(self, "tier", 1)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """从 SKILL.md 文本里抽 frontmatter，返回 {key: value} dict。

    容错：frontmatter 不存在或解析失败 → 返回 {}。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _parse_tier(raw: str) -> int:
    if raw == "2":
        return 2
    return 1


def _parse_description(frontmatter: dict[str, str], text: str) -> str:
    """description 优先级：frontmatter `description:` > H1 行 > 目录名。"""
    if "description" in frontmatter and frontmatter["description"]:
        return frontmatter["description"]
    # 取 H1（可能位于 frontmatter 之后，所以从整段文本找第一个 `# ...`）
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


class SkillService:
    """skill 的统一入口。设计参考 MemoryStore：每次 `abstract()` 重扫、无缓存。"""

    def __init__(self, root: Path, max_l1: int = 50) -> None:
        self._root = root
        self._max_l1 = max_l1

    # ---- 核心 API ----

    def abstract(self) -> list[SkillAbstract]:
        """扫 `<root>/<name>/SKILL.md`，返回所有 skill metadata（tier 1 + tier 2）。

        没有 SKILL.md 的子目录、frontmatter 解析失败的、目录不可读的都跳过。
        """
        if not self._root.exists():
            return []

        out: list[SkillAbstract] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError as exc:
                _log.warning("skill %r: cannot read SKILL.md: %s", entry.name, exc)
                continue
            frontmatter = _parse_frontmatter(text)
            desc = _parse_description(frontmatter, text) or entry.name
            tier = _parse_tier(frontmatter.get("tier", "1"))
            name = frontmatter.get("name", "").strip() or entry.name
            out.append(SkillAbstract(
                name=name,
                description=desc,
                tier=tier,
                path=entry,
            ))
        return out

    def abstract_l1(self) -> list[SkillAbstract]:
        """仅返回 L1，受 `max_l1` 限制。超限时 warn 并截断。"""
        all_l1 = [s for s in self.abstract() if s.tier == 1]
        if len(all_l1) > self._max_l1:
            _log.warning(
                "L1 skills=%d exceeds max_l1=%d; truncating to first %d (alphabetical)",
                len(all_l1), self._max_l1, self._max_l1,
            )
        return all_l1[: self._max_l1]

    def load(self, name: str) -> str:
        """读 `<root>/<name>/SKILL.md` 全文。

        Raises:
            FileNotFoundError: skill 不存在。
        """
        return (self._root / name / "SKILL.md").read_text(encoding="utf-8")

    # ---- Prompt 渲染 ----

    def format_l1_prompt_section(self) -> str:
        """返回 system prompt 的 `## Skills` section（已替换 `{MONOX_SKILLS_DIR}` 为绝对路径）。

        包含：目录说明 + find/load/create 机制 + L1 列表 + L2 计数。
        空时返回 ""，由 caller 决定是否注入。
        """
        all_skills = self.abstract()
        l1 = [s for s in all_skills if s.tier == 1][: self._max_l1]
        l2_count = sum(1 for s in all_skills if s.tier == 2)
        if not all_skills:
            return ""
        return self._render_template(l1, l2_count)

    @staticmethod
    def _render_template(l1: list[SkillAbstract], l2_count: int) -> str:
        """渲染 prompt section。

        `{MONOX_SKILLS_DIR}` 是占位符——调用方负责 `.format(**path_vars)` 替换为绝对路径。
        """
        l1_lines = "\n".join(f"- `{s.name}`: {s.description}" for s in l1)
        if not l1_lines:
            l1_lines = "(none yet)"
        l2_block = ""
        if l2_count:
            l2_block = (
                f"\n### Tier 2 (find via grep, not auto-injected)\n\n"
                f"{l2_count} more skills available — `ls {{MONOX_SKILLS_DIR}}/` to list, "
                f"`grep -l '<keyword>' {{MONOX_SKILLS_DIR}}/*/SKILL.md` to narrow.\n"
            )
        return (
            "\n## Skills\n"
            "\n"
            "Skills are reusable knowledge packages. Each skill is a directory under\n"
            "`{MONOX_SKILLS_DIR}/<name>/` containing a `SKILL.md`.\n"
            "\n"
            "- **Find** a skill: `ls {MONOX_SKILLS_DIR}/` lists all; to narrow by topic\n"
            "  run `grep -l '<keyword>' {MONOX_SKILLS_DIR}/*/SKILL.md`.\n"
            "- **Load** a skill's full body: call `skill_load(name='<name>')` — returns\n"
            "  the complete SKILL.md.\n"
            "- **Create** a skill: write a new directory\n"
            "  `{MONOX_SKILLS_DIR}/<new_name>/SKILL.md` with optional YAML frontmatter\n"
            "  (`name:`, `description:`, `tier: 1|2`). Default tier is 1. Pick tier=2\n"
            "  for rarely-used skills you don't want auto-injected every turn.\n"
            "\n"
            "### Available (L1, auto-injected each turn)\n"
            "\n"
            f"{l1_lines}\n"
            f"{l2_block}"
        )

    def format_l1_prompt_section_resolved(self, skills_dir: str) -> str:
        """`format_l1_prompt_section` 的便捷版：直接用绝对路径替换 `{MONOX_SKILLS_DIR}`。

        推荐用这个，调用方无需自己 `.format()`。
        """
        return self.format_l1_prompt_section().replace("{MONOX_SKILLS_DIR}", skills_dir)