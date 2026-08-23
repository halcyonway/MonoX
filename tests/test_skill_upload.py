"""core/skill_upload.py 单测：zip 解压 + 结构校验。"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from core.skill_upload import SkillUploadError, extract_skill_zip


def _make_zip(files: dict[str, str]) -> bytes:
    """files: {zip_path: content}。空字符串 → 当目录（entry 以 '/' 结尾）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if content == "":
                # directory entry
                zf.writestr(name if name.endswith("/") else name + "/", b"")
            else:
                zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


class TestHappyPath:
    def test_single_skill(self, tmp_path: Path) -> None:
        z = _make_zip({"foo/SKILL.md": "# Foo\n\nbody\n"})
        added = extract_skill_zip(z, tmp_path)
        assert added == ["foo"]
        assert (tmp_path / "foo" / "SKILL.md").read_text() == "# Foo\n\nbody\n"

    def test_multi_skill(self, tmp_path: Path) -> None:
        z = _make_zip({
            "alpha/SKILL.md": "# A\n",
            "beta/SKILL.md": "---\ntier: 2\n---\n# B\n",
            "gamma/SKILL.md": "# G\n",
        })
        added = extract_skill_zip(z, tmp_path)
        assert added == ["alpha", "beta", "gamma"]
        assert (tmp_path / "alpha" / "SKILL.md").read_text() == "# A\n"
        assert (tmp_path / "beta" / "SKILL.md").read_text() == "---\ntier: 2\n---\n# B\n"
        assert (tmp_path / "gamma" / "SKILL.md").read_text() == "# G\n"

    def test_with_extra_files(self, tmp_path: Path) -> None:
        z = _make_zip({
            "foo/SKILL.md": "# F\n",
            "foo/scripts/run.sh": "#!/bin/sh\necho hi\n",
            "foo/data/note.txt": "note",
        })
        extract_skill_zip(z, tmp_path)
        assert (tmp_path / "foo" / "scripts" / "run.sh").exists()
        assert (tmp_path / "foo" / "data" / "note.txt").read_text() == "note"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        extract_skill_zip(_make_zip({"foo/SKILL.md": "v1"}), tmp_path)
        extract_skill_zip(_make_zip({"foo/SKILL.md": "v2"}), tmp_path)
        assert (tmp_path / "foo" / "SKILL.md").read_text() == "v2"

    def test_returns_alphabetical_order(self, tmp_path: Path) -> None:
        z = _make_zip({
            "zebra/SKILL.md": "z",
            "alpha/SKILL.md": "a",
            "middle/SKILL.md": "m",
        })
        assert extract_skill_zip(z, tmp_path) == ["alpha", "middle", "zebra"]


class TestStructureErrors:
    def test_empty_body(self, tmp_path: Path) -> None:
        with pytest.raises(SkillUploadError, match="empty body"):
            extract_skill_zip(b"", tmp_path)

    def test_not_a_zip(self, tmp_path: Path) -> None:
        with pytest.raises(zipfile.BadZipFile):
            extract_skill_zip(b"this is not a zip", tmp_path)

    def test_skill_md_at_root_rejected(self, tmp_path: Path) -> None:
        z = _make_zip({"SKILL.md": "# Root\n"})
        with pytest.raises(SkillUploadError, match="stray entry"):
            extract_skill_zip(z, tmp_path)

    def test_mixed_root_and_subdir_rejected(self, tmp_path: Path) -> None:
        z = _make_zip({
            "loose.txt": "stray",
            "foo/SKILL.md": "# F\n",
        })
        with pytest.raises(SkillUploadError, match="stray entry"):
            extract_skill_zip(z, tmp_path)

    def test_missing_skill_md(self, tmp_path: Path) -> None:
        z = _make_zip({"foo/other.md": "no skill md here"})
        with pytest.raises(SkillUploadError, match="does not contain SKILL.md"):
            extract_skill_zip(z, tmp_path)


class TestNameValidation:
    def test_invalid_name_with_space(self, tmp_path: Path) -> None:
        # 空格不在 SkillService 合法名集合里（_is_valid_skill_name 没禁空格，
        # 但我们要求 'no path separators'，而 zip 里空格 = 实际目录名含空格，
        # 与 SkillService 对合法名认知不一致 — 但 SkillService 实际允许空格，
        # 所以这里只测试明确非法的 '.' 前缀）
        z = _make_zip({".hidden/SKILL.md": "x"})  # . 前缀被拒
        with pytest.raises(SkillUploadError, match="invalid skill name"):
            extract_skill_zip(z, tmp_path)

    def test_invalid_name_dotdot(self, tmp_path: Path) -> None:
        # zip slip 攻击：试图写到父目录
        z = _make_zip({"../escape/SKILL.md": "x"})
        # 顶层是 ".."，以 "." 开头 → _is_valid_skill_name 拒绝
        with pytest.raises(SkillUploadError, match="invalid skill name"):
            extract_skill_zip(z, tmp_path)

    def test_valid_names(self, tmp_path: Path) -> None:
        # 与 SkillService._is_valid_skill_name 一致：允许 unicode、空格、不允许 '.' 前缀
        for name in ("simple", "with-dash", "with_underscore", "Mixed.Case", "数字123", "with space"):
            z = _make_zip({f"{name}/SKILL.md": "x"})
            added = extract_skill_zip(z, tmp_path)
            assert name in added
