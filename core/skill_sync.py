"""Skill sync — extensions/skills/ → .monox/skills/.

设计：
- `extensions/skills/<name>/` 是 skill 的 source of truth（git-tracked）。
- `.monox/skills/<name>/` 是 runtime 副本（gitignored，`SkillService` 只读这个）。
- `run.py` 启动时调 `sync_extension_skills(extensions_dir, runtime_dir)`：
  * runtime 不存在 → `shutil.copytree` 整目录拷过去（auto-resurrect on delete）
  * runtime 存在 → 跳过不动（用户可能改过；想刷回 extensions 就 rm -rf 后重启）
- 不做「always overwrite」：extensions 是 source of truth，但用户编辑工作流
  是「改 extensions → 重启 sync」，而不是「改 runtime 然后被覆盖回去」；
  sync 只补缺失，让用户明确删除后才能回到 extensions 版本。
- Per-skill try/except：单个 skill 拷失败不阻塞启动，剩下的继续。
- 结果写到 `.monox/state/skill-sync.json`：timestamp + copied/skipped/errors 名单，
  debug 时知道上次启动 sync 发生了什么。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

_log = logging.getLogger("monox.skill_sync")


@dataclass
class SyncResult:
    """单次 sync 的结果。"""
    copied: list[str] = field(default_factory=list)       # runtime 不存在 → 整目录拷过去
    skipped_existing: list[str] = field(default_factory=list)  # runtime 已存在 → 不动
    skipped_invalid: list[str] = field(default_factory=list)   # extensions 里没有 SKILL.md → 不算 skill
    errors: list[dict] = field(default_factory=list)      # [{"name": ..., "error": "..."}]

    def write_state(self, state_path: Path) -> None:
        """写 .monox/state/skill-sync.json，覆盖写入。debug 时看上次启动 sync 行为。"""
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            **asdict(self),
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def log_summary(self) -> None:
        _log.info(
            "skill-sync extensions→runtime: copied=%d skipped_existing=%d skipped_invalid=%d errors=%d",
            len(self.copied),
            len(self.skipped_existing),
            len(self.skipped_invalid),
            len(self.errors),
        )
        for name in self.copied:
            _log.info("  copied: %s", name)
        for err in self.errors:
            _log.warning("  error: %s — %s", err.get("name"), err.get("error"))


def sync_extension_skills(
    extensions_dir: Path | str,
    runtime_dir: Path | str,
    state_path: Path | str | None = None,
) -> SyncResult:
    """extensions/skills/<name>/ → runtime_dir/<name>/，补缺失。

    Args:
        extensions_dir: source of truth（git-tracked）。不存在时静默 noop。
        runtime_dir: 运行时副本根目录（gitignored）。会自动 mkdir。
        state_path: 写 SyncResult 的位置（默认 `<runtime_dir>/../state/skill-sync.json`）。
                    None 时跳过写 state（测试场景用）。

    Returns:
        SyncResult，包含 copied / skipped_existing / skipped_invalid / errors。
    """
    ext = Path(extensions_dir)
    rt = Path(runtime_dir)
    result = SyncResult()

    if not ext.exists():
        _log.debug("skill-sync: extensions_dir %s missing; skip", ext)
        result.log_summary()
        _maybe_write_state(result, state_path, ext, rt)
        return result

    # 扫 extensions/<name>/SKILL.md（必须存在才算合法 skill）
    for entry in sorted(ext.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            result.skipped_invalid.append(entry.name)
            continue
        target = rt / entry.name
        if target.exists():
            result.skipped_existing.append(entry.name)
            continue
        # 补缺失：runtime 没有这个 skill → 整目录拷过去（auto-resurrect on delete）
        try:
            rt.mkdir(parents=True, exist_ok=True)
            shutil.copytree(entry, target)
            result.copied.append(entry.name)
        except OSError as exc:
            result.errors.append({"name": entry.name, "error": str(exc)})

    result.log_summary()
    _maybe_write_state(result, state_path, ext, rt)
    return result


def _maybe_write_state(
    result: SyncResult,
    state_path: Path | str | None,
    ext: Path,
    rt: Path,
) -> None:
    if state_path is None:
        return
    p = Path(state_path)
    # 默认 state_path = <runtime_dir>/../state/skill-sync.json（与 monox state 目录约定一致）
    if str(state_path) == "":
        p = rt.parent / "state" / "skill-sync.json"
    try:
        result.write_state(p)
    except OSError as exc:
        _log.warning("skill-sync: failed to write state %s: %s", p, exc)