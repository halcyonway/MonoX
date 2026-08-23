"""SkillService 行为测试。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.skill_service import (
    SkillAbstract,
    SkillService,
    _parse_description,
    _parse_frontmatter,
    _parse_tier,
)


def _write(tmp: Path, name: str, body: str) -> Path:
    """辅助：写 `<tmp>/<name>/SKILL.md`。"""
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------- _parse_frontmatter ----------

def test_parse_frontmatter_basic() -> None:
    text = "---\nname: foo\ntier: 2\n---\n# Foo\nbody\n"
    assert _parse_frontmatter(text) == {"name": "foo", "tier": "2"}


def test_parse_frontmatter_no_frontmatter() -> None:
    text = "# Foo\nbody\n"
    assert _parse_frontmatter(text) == {}


def test_parse_frontmatter_quoted_value() -> None:
    text = '---\nname: "daily report"\n---\nbody\n'
    assert _parse_frontmatter(text) == {"name": "daily report"}


def test_parse_frontmatter_handles_inline_comments() -> None:
    text = "---\nname: foo  # this is a comment\ntier: 1\n---\n"
    # inline comments aren't stripped; verify it's tolerant
    fm = _parse_frontmatter(text)
    assert fm.get("tier") == "1"
    assert "foo" in fm.get("name", "")


def test_parse_frontmatter_skips_blank_and_comment_lines() -> None:
    text = "---\n# top comment\n\nname: x\n---\n"
    assert _parse_frontmatter(text) == {"name": "x"}


# ---------- _parse_tier ----------

def test_parse_tier_valid() -> None:
    assert _parse_tier("1") == 1
    assert _parse_tier("2") == 2


def test_parse_tier_invalid_defaults_to_1() -> None:
    assert _parse_tier("") == 1
    assert _parse_tier("99") == 1
    assert _parse_tier("abc") == 1


# ---------- _parse_description ----------

def test_parse_description_prefers_frontmatter() -> None:
    fm = {"description": "from frontmatter"}
    text = "# From H1\nbody\n"
    assert _parse_description(fm, text) == "from frontmatter"


def test_parse_description_falls_back_to_h1() -> None:
    fm: dict[str, str] = {}
    text = "# From H1\nbody\n"
    assert _parse_description(fm, text) == "From H1"


def test_parse_description_falls_back_to_h1_when_fm_empty() -> None:
    fm = {"description": ""}
    text = "# H1 title\nbody\n"
    assert _parse_description(fm, text) == "H1 title"


# ---------- SkillAbstract ----------

def test_skill_abstract_post_init_normalizes_invalid_tier() -> None:
    # tier=99 → 1; tier=0 → 1
    a = SkillAbstract(name="x", description="y", tier=99, path=Path("/tmp"))
    assert a.tier == 1
    b = SkillAbstract(name="x", description="y", tier=0, path=Path("/tmp"))
    assert b.tier == 1


# ---------- abstract ----------

def test_abstract_empty_dir(tmp_path: Path) -> None:
    svc = SkillService(tmp_path)
    assert svc.abstract() == []


def test_abstract_no_frontmatter(tmp_path: Path) -> None:
    _write(tmp_path, "coding", "# Coding helper\nbody\n")
    svc = SkillService(tmp_path)
    skills = svc.abstract()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "coding"
    assert s.description == "Coding helper"
    assert s.tier == 1  # default


def test_abstract_with_frontmatter(tmp_path: Path) -> None:
    body = (
        "---\n"
        "name: daily-report\n"
        "description: 生成每日工作日报\n"
        "tier: 1\n"
        "---\n"
        "# Daily Report\nbody\n"
    )
    _write(tmp_path, "daily", body)
    svc = SkillService(tmp_path)
    skills = svc.abstract()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "daily-report"  # frontmatter overrides dir name
    assert s.description == "生成每日工作日报"
    assert s.tier == 1


def test_abstract_tier_2(tmp_path: Path) -> None:
    _write(tmp_path, "rare", "---\ntier: 2\n---\n# Rare\n")
    svc = SkillService(tmp_path)
    skills = svc.abstract()
    assert skills[0].tier == 2


def test_abstract_skips_dirs_without_skill_md(tmp_path: Path) -> None:
    (tmp_path / "no_skill_md").mkdir()
    _write(tmp_path, "good", "# Good\n")
    svc = SkillService(tmp_path)
    assert [s.name for s in svc.abstract()] == ["good"]


def test_abstract_skips_files(tmp_path: Path) -> None:
    # Loose file at root level shouldn't be treated as skill
    (tmp_path / "loose.md").write_text("not a skill")
    _write(tmp_path, "real", "# Real\n")
    svc = SkillService(tmp_path)
    assert [s.name for s in svc.abstract()] == ["real"]


def test_abstract_swallows_read_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Point service at non-existent root → empty list, no raise
    svc = SkillService(tmp_path / "missing")
    assert svc.abstract() == []


def test_abstract_sorted_alphabetically(tmp_path: Path) -> None:
    for n in ("zebra", "alpha", "mango"):
        _write(tmp_path, n, f"# {n}\n")
    svc = SkillService(tmp_path)
    assert [s.name for s in svc.abstract()] == ["alpha", "mango", "zebra"]


def test_abstract_tier_normalization_invalid(tmp_path: Path) -> None:
    _write(tmp_path, "bad", "---\ntier: 99\n---\n# Bad\n")
    svc = SkillService(tmp_path)
    assert svc.abstract()[0].tier == 1


# ---------- abstract_l1 / max_l1 ----------

def test_abstract_l1_filters_out_tier_2(tmp_path: Path) -> None:
    _write(tmp_path, "l1a", "# l1a\n")
    _write(tmp_path, "l2a", "---\ntier: 2\n---\n# l2a\n")
    _write(tmp_path, "l1b", "# l1b\n")
    svc = SkillService(tmp_path)
    l1 = svc.abstract_l1()
    assert {s.name for s in l1} == {"l1a", "l1b"}


def test_abstract_l1_truncates_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # 3 skills, max_l1=2 → first 2 alphabetical, warn logged
    for n in ("a", "b", "c"):
        _write(tmp_path, n, f"# {n}\n")
    svc = SkillService(tmp_path, max_l1=2)
    with caplog.at_level(logging.WARNING, logger="monox.skill_service"):
        l1 = svc.abstract_l1()
    assert [s.name for s in l1] == ["a", "b"]
    assert any("exceeds max_l1=2" in rec.message for rec in caplog.records)


# ---------- load ----------

def test_load_full_content(tmp_path: Path) -> None:
    body = "---\nname: foo\n---\n# Foo\nLong body\n"
    _write(tmp_path, "foo", body)
    svc = SkillService(tmp_path)
    assert svc.load("foo") == body


def test_load_missing_raises(tmp_path: Path) -> None:
    svc = SkillService(tmp_path)
    with pytest.raises(FileNotFoundError):
        svc.load("does-not-exist")


# ---------- format_l1_prompt_section ----------

def test_format_l1_empty(tmp_path: Path) -> None:
    svc = SkillService(tmp_path)
    assert svc.format_l1_prompt_section() == ""


def test_format_l1_includes_l1_list(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", "# Alpha\n")
    _write(tmp_path, "beta", "---\ndescription: Beta desc\n---\n")
    svc = SkillService(tmp_path)
    section = svc.format_l1_prompt_section()
    assert "## Skills" in section
    assert "- `alpha`: Alpha" in section
    assert "- `beta`: Beta desc" in section
    assert "{MONOX_SKILLS_DIR}" in section  # caller still needs .format()


def test_format_l1_resolved_replaces_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", "# Alpha\n")
    svc = SkillService(tmp_path)
    section = svc.format_l1_prompt_section_resolved("/abs/.monox/skills")
    assert "/abs/.monox/skills" in section
    assert "{MONOX_SKILLS_DIR}" not in section


def test_format_l1_l2_section_present(tmp_path: Path) -> None:
    _write(tmp_path, "l1", "# L1\n")
    _write(tmp_path, "rare1", "---\ntier: 2\n---\n")
    _write(tmp_path, "rare2", "---\ntier: 2\n---\n")
    svc = SkillService(tmp_path)
    section = svc.format_l1_prompt_section()
    assert "Tier 2 (find via grep, not auto-injected)" in section
    assert "2 more skills available" in section


def test_format_l1_l2_section_omitted_when_zero(tmp_path: Path) -> None:
    _write(tmp_path, "l1", "# L1\n")
    svc = SkillService(tmp_path)
    section = svc.format_l1_prompt_section()
    assert "Tier 2" not in section