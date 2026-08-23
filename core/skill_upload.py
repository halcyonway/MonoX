"""Skill zip 上传解压工具。

MonoDesk「Upload .zip」功能用：上传一个 skill pack zip，MonoX 端解压到 skills_root。

约定 zip 结构：
    <skill_name>/SKILL.md           # 单个 skill
    <skill_name>/scripts/foo.sh     # 附加文件（可选）

- 顶层必须是 N 个目录，每个目录名 = skill name
- 每个顶层目录里必须有 SKILL.md
- skill name 必须是合法标识（只允许 [A-Za-z0-9._-]）

不接受 zip 根目录直接放 SKILL.md（避免命名歧义；想要单 skill 时也请包一层）。
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

from core.skill_service import _is_valid_skill_name

_log = logging.getLogger("monox.skill_upload")

# 用 SkillService 同一份 name 校验逻辑，保证两端对「合法 skill 目录名」的认知一致。
# （zip 顶层目录名必须通过它才能解压；这同时阻止 zip slip — '.' 开头被拒。）


class SkillUploadError(ValueError):
    """zip 结构 / 命名违反约定的错误。"""


def extract_skill_zip(zip_bytes: bytes, skills_root: Path) -> list[str]:
    """解压 skill zip 到 `skills_root`。返回成功添加的 skill 名（按字典序）。

    Raises:
        zipfile.BadZipFile: 不是合法 zip 文件。
        SkillUploadError: 结构违反约定（顶层无目录、缺 SKILL.md、命名非法等）。
        OSError: 写文件失败。
    """
    if not zip_bytes:
        raise SkillUploadError("empty body")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise zipfile.BadZipFile(f"invalid zip: {exc}") from exc

    # 第一遍：收集顶层目录名，校验「所有 entry 都在某个顶层目录内」
    top_dirs: set[str] = set()
    for name in zf.namelist():
        parts = name.split("/", 1)
        if len(parts) < 2:
            raise SkillUploadError(
                f"all entries must be inside a top-level skill dir; "
                f"found stray entry at zip root: {name!r}"
            )
        top_dirs.add(parts[0])

    if not top_dirs:
        raise SkillUploadError("zip contains no entries")

    # 第二遍：先校验目录名（zip slip 防御），再校验 SKILL.md 存在
    for d in top_dirs:
        if not _is_valid_skill_name(d):
            raise SkillUploadError(
                f"invalid skill name in zip: {d!r} "
                f"(no '.' prefix, no path separators)"
            )
        skill_md_path = f"{d}/SKILL.md"
        if skill_md_path not in zf.namelist():
            raise SkillUploadError(
                f"top-level dir {d!r} does not contain SKILL.md"
            )

    # 三段校验通过后才写盘：先 name 再结构，避免半污染
    # （上面两个 loop 已经把全部 top_dirs 校验完了 → 可以放心解压）

    # 第三遍：按顶层目录逐个解压。`d` 已经 _NAME_RE 校验过 → 无 zip slip 风险。
    added: list[str] = []
    for d in sorted(top_dirs):
        target = skills_root / d
        target.mkdir(parents=True, exist_ok=True)
        for entry in zf.namelist():
            if not entry.startswith(f"{d}/"):
                continue
            rel = entry[len(f"{d}/"):]
            if not rel:
                continue  # dir entry itself
            if rel.endswith("/"):
                (target / rel).mkdir(parents=True, exist_ok=True)
                continue
            out_path = target / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(zf.read(entry))
        added.append(d)

    return added
