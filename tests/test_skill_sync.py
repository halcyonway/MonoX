"""core/skill_sync.py 单测：sync 行为 + auto-resurrect + 整目录拷贝。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.skill_sync import sync_extension_skills


def _make_skill(parent: Path, name: str, body: str = "hello", *, with_helper: bool = False) -> Path:
    """在 parent/<name>/SKILL.md 建一个合法 skill（可选 helper script + templates）。"""
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\ndescription: {name}\n---\n\n{body}\n", encoding="utf-8"
    )
    if with_helper:
        # 模拟 i2i skill：除了 SKILL.md 还有 helper script 和 templates/
        (d / "helper.py").write_text("# helper\n", encoding="utf-8")
        (d / "templates").mkdir(exist_ok=True)
        (d / "templates" / "a.md").write_text("prompt A\n", encoding="utf-8")
        (d / "templates" / "b.md").write_text("prompt B\n", encoding="utf-8")
    return d


# ---- Tests ----


def test_copies_missing_skill(tmp_path: Path):
    """extensions 有 X，runtime 没有 → 整目录拷过去。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    _make_skill(ext, "alpha")

    sync_extension_skills(ext, rt)

    assert (rt / "alpha" / "SKILL.md").exists()
    assert (rt / "alpha" / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_skips_existing_skill(tmp_path: Path):
    """runtime 已有 X → 不动；runtime 内容保留（包括用户改过的）。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    _make_skill(ext, "alpha", body="from extensions")
    rt.mkdir()
    rt_existing = rt / "alpha"
    rt_existing.mkdir()
    (rt_existing / "SKILL.md").write_text("USER EDIT\n", encoding="utf-8")

    sync_extension_skills(ext, rt)

    # runtime 那份应该原封不动
    assert (rt / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "USER EDIT\n"


def test_runtime_only_skill_untouched(tmp_path: Path):
    """runtime 有 X，extensions 没有 → 不动（用户的决定）。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    ext.mkdir()
    rt.mkdir()
    (rt / "personal").mkdir()
    (rt / "personal" / "SKILL.md").write_text("personal only\n", encoding="utf-8")

    sync_extension_skills(ext, rt)

    assert (rt / "personal" / "SKILL.md").read_text(encoding="utf-8") == "personal only\n"


def test_extensions_missing_is_noop(tmp_path: Path):
    """extensions 目录不存在 → 静默 noop，不报错。"""
    ext = tmp_path / "ext-does-not-exist"
    rt = tmp_path / "runtime"

    result = sync_extension_skills(ext, rt)

    assert result.copied == []
    assert result.skipped_existing == []
    assert result.skipped_invalid == []
    assert result.errors == []
    assert not rt.exists()


def test_entry_without_skill_md_is_skipped(tmp_path: Path):
    """extensions/<name>/ 没有 SKILL.md → 算非法 entry，跳过。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    ext.mkdir()
    (ext / "broken").mkdir()
    (ext / "broken" / "README.md").write_text("not a skill\n", encoding="utf-8")
    _make_skill(ext, "valid")

    result = sync_extension_skills(ext, rt)

    assert "broken" in result.skipped_invalid
    assert "valid" in result.copied
    assert not (rt / "broken").exists()


def test_auto_resurrect_after_delete(tmp_path: Path):
    """先 sync → 用户删 runtime → 再 sync → 又被补上（auto-resurrect）。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    _make_skill(ext, "alpha")

    sync_extension_skills(ext, rt)
    assert (rt / "alpha").exists()

    # 用户删了
    import shutil
    shutil.rmtree(rt / "alpha")
    assert not (rt / "alpha").exists()

    # 再 sync → auto-resurrect
    sync_extension_skills(ext, rt)
    assert (rt / "alpha" / "SKILL.md").exists()


def test_full_skill_dir_copied_intact(tmp_path: Path):
    """i2i 这种有 helper script + templates/ 的 skill 必须整目录拷过去。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    _make_skill(ext, "i2i", body="image skill", with_helper=True)

    sync_extension_skills(ext, rt)

    # SKILL.md + helper.py + templates/a.md + templates/b.md 全部到位
    assert (rt / "i2i" / "SKILL.md").exists()
    assert (rt / "i2i" / "helper.py").exists()
    assert (rt / "i2i" / "templates" / "a.md").exists()
    assert (rt / "i2i" / "templates" / "b.md").exists()


def test_state_json_written(tmp_path: Path):
    """传 state_path 时写 .monox/state/skill-sync.json，含 timestamp + 列表。"""
    ext = tmp_path / "ext"
    rt = tmp_path / "runtime"
    state = tmp_path / "state" / "skill-sync.json"
    _make_skill(ext, "alpha")
    _make_skill(ext, "beta")

    sync_extension_skills(ext, rt, state_path=state)

    assert state.exists()
    data = json.loads(state.read_text(encoding="utf-8"))
    assert set(data["copied"]) == {"alpha", "beta"}
    assert data["skipped_existing"] == []
    assert "timestamp" in data