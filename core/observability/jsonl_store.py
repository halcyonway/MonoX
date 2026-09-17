"""JsonlTraceStore：per-session `<traces_root>/<sk>/traces.jsonl` append-only。

格式：每行一个完整 Run JSON（run_id 重复视为更新，新行覆盖旧 run）。

实现要点：
- 文件 I/O 走 `asyncio.to_thread`，不阻塞主事件循环。
- 用 in-process lock 串行化同一 store 的写，避免 race。
- `list_runs` / `restore` 只读文件末尾一定行数，避免大文件全量扫描。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.observability.store import RunSummary, TraceStore
from core.observability.types import Run

_log = logging.getLogger("monox.trace.jsonl")

# 单文件 max bytes，避免大文件阻塞 list；超过则丢弃旧的。
_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB
# restore / list 最多扫的尾部字节数（够用即可，太大会慢）。
_TAIL_SCAN_BYTES = 8 * 1024 * 1024  # 8MB


def _to_summary(run: Run) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        session_key=run.session_key,
        user_text=run.user_text,
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        status=run.status,
        turn_count=len(run.turns),
        schema_version=run.schema_version,
    )


def _parse_line(line: str) -> Run | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return Run.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def _line_schema_version(line: str) -> int | None:
    """peek 单行 schema_version；缺字段视为 v1。"""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return int(raw.get("schema_version", 1))


def _read_all_runs(path: Path) -> dict[str, Run]:
    """全量读文件，run_id → Run（后者覆盖前者）。

    v1 行（schema_version < 2）跳过并 warn——启动时全 v1 文件已归档，混合
    文件里残留的 v1 行也按"不读"处理。
    """
    out: dict[str, Run] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("trace file read failed: %s", exc)
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        sv = _line_schema_version(line)
        if sv is not None and sv < 2:
            _log.debug("skip legacy v1 trace line in %s", path)
            continue
        run = _parse_line(line)
        if run is None:
            continue
        out[run.run_id] = run
    return out


def _read_tail_runs(path: Path, max_bytes: int) -> dict[str, Run]:
    """读文件末尾最多 max_bytes，按 run_id 去重（后者覆盖前者）。

    大文件 list 性能优化：只关心最近 N 条不需要扫整个文件。
    v1 行跳过（见 _read_all_runs 注释）。
    """
    out: dict[str, Run] = {}
    if not path.exists():
        return out
    try:
        size = path.stat().st_size
        with path.open("rb") as fp:
            if size > max_bytes:
                fp.seek(size - max_bytes)
                # 跳过可能落在行中间的碎片
                nl = fp.readline()
                if nl and not nl.endswith(b"\n"):
                    pass  # 第一行碎片丢了，剩余行仍完整
            data = fp.read().decode("utf-8", errors="replace")
    except OSError as exc:
        _log.warning("trace file tail read failed: %s", exc)
        return out
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        sv = _line_schema_version(line)
        if sv is not None and sv < 2:
            _log.debug("skip legacy v1 trace line in tail of %s", path)
            continue
        run = _parse_line(line)
        if run is None:
            continue
        out[run.run_id] = run
    return out


@dataclass
class JsonlTraceStore:
    """per-session jsonl 实现的 TraceStore。

    path 由 SessionManager 在创建时给定（`traces_root / sk / traces.jsonl`）。
    """

    path: Path
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        self._path = Path(self.path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        # v1 归档：启动时扫一次，全是 v1 行就把整个文件重命名成 .v1.jsonl
        self._archive_v1_if_needed()

    def _archive_v1_if_needed(self) -> None:
        path = self._path
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("trace file read for v1-archive failed: %s", exc)
            return
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return
        # 没有任何 v2 行 → 整体归档；只要含一条 v2 就不归档（混合文件按可读处理）
        has_v2 = False
        for ln in lines:
            sv = _line_schema_version(ln)
            if sv is None:
                continue
            if sv >= 2:
                has_v2 = True
                break
        if has_v2:
            return
        # 全 v1：归档
        archived = path.with_suffix(path.suffix + ".v1.jsonl")
        if archived.exists():
            # 已存在归档：覆盖还是跳过？选"追加"-不，文件级操作用 replace + 序号
            # 简单起见：给归档再加一个 .1 后缀避免覆盖
            i = 1
            while True:
                cand = path.with_suffix(path.suffix + f".v1.{i}.jsonl")
                if not cand.exists():
                    archived = cand
                    break
                i += 1
        try:
            path.rename(archived)
            _log.info(
                "archived legacy v1 trace file %s -> %s (%d lines, v2 reader not backcompat)",
                path, archived, len(lines),
            )
        except OSError as exc:
            _log.warning("v1 archive rename failed: %s", exc)

    # ---- write ----

    async def save_run(self, run: Run) -> None:
        line = json.dumps(run.to_dict(), ensure_ascii=False, default=str)
        path = self._path

        def _write() -> None:
            # 在独占区：写 + 截断
            existing = ""
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError as exc:
                    _log.warning("trace file read for rewrite failed: %s", exc)
                    existing = ""
            # 去掉同 run_id 的旧行（upsert）
            new_lines: list[str] = []
            for ln in existing.splitlines():
                stripped = ln.strip()
                if not stripped:
                    continue
                try:
                    if json.loads(stripped).get("run_id") == run.run_id:
                        continue
                except json.JSONDecodeError:
                    continue
                new_lines.append(stripped)
            new_lines.append(line)
            text = "\n".join(new_lines) + "\n"
            # 超 max_file_bytes：从头部裁掉
            if len(text.encode("utf-8")) > self.max_file_bytes:
                enc = text.encode("utf-8")
                trimmed = enc[-self.max_file_bytes:]
                # 对齐到下一行
                nl = trimmed.find(b"\n")
                if nl >= 0:
                    trimmed = trimmed[nl + 1:]
                text = trimmed.decode("utf-8", errors="replace")
            try:
                path.write_text(text, encoding="utf-8")
            except OSError as exc:
                _log.warning("trace file write failed: %s", exc)

        async with self._write_lock:
            await asyncio.to_thread(_write)

    # ---- read ----

    async def get_run(self, session_key: str, run_id: str) -> Run | None:
        runs = await asyncio.to_thread(_read_all_runs, self._path)
        return runs.get(run_id)

    async def list_runs(self, session_key: str, limit: int = 20) -> list[RunSummary]:
        runs = await asyncio.to_thread(_read_tail_runs, self._path, _TAIL_SCAN_BYTES)
        sorted_runs = sorted(runs.values(), key=lambda r: r.start_ts, reverse=True)
        return [_to_summary(r) for r in sorted_runs[:limit]]

    async def restore(self, session_key: str, limit: int = 20) -> list[Run]:
        runs = await asyncio.to_thread(_read_tail_runs, self._path, _TAIL_SCAN_BYTES)
        sorted_runs = sorted(runs.values(), key=lambda r: r.start_ts, reverse=True)
        return sorted_runs[:limit]