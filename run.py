"""MonoX Runtime 启动入口。

Runtime 进程做三件事：
  1) 起 RuntimeServer（ws server，:8765，给 channel client 连 + 外部工具）
  2) 起 HealthServer（HTTP `/health` 端点，:8767）
  3) 起 SessionManager（管理多 session_key 的 LoopEngine，idle 自动销毁 + 恢复）

channel 是独立进程，各自从 config.toml 读配置，不由 run.py 拉起。
各 channel 启动方式：
  terminal:   uv run python -m extensions.channels.terminal
  monodesk:    monoDesk 桌面 app（连 ws://127.0.0.1:8766）
  feishu:      uv run python -m extensions.channels.feishu
  textual:     uv run python -m extensions.channels.textual_chat

启动：
    uv run python run.py [config.toml] [--server-host HOST] [--server-port PORT]
                          [--health-port PORT] [--idle-timeout SEC]
    uv run python run.py --stop   # 清理残留进程 + 端口
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import dataclasses
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from core.config import Config, session_paths
from core.health_server import HealthServer, HealthServerConfig
from core.llm_proxy import OpenAIStreamProxy
from core.loop import (
    BashTool,
    ReadToolResultBudgetTool,
    SkillLoadTool,
    ToolRegistry,
    WaitIoTool,
)
from core.loop.compression import CompressionService
from core.loop.skill_summary import SkillSummaryLoader
from core.memory import FsMemoryStore
from core.runtime_server import RuntimeServer
from core.sandbox import BashRunner
from core.session_manager import SessionManager


DEFAULT_SYSTEM = """You are MonoX, a coding agent. You run inside a sandboxed bash environment.

Plan briefly, then execute. Use bash for all I/O. Use skill_load to fetch details of a skill before invoking it.

Tool results may be L1-compressed; if you see budget_id, call read_tool_result_budget(budget_id=...) for the full version.

When you are done with the current turn and ready to receive the next message, call wait_io. If the user sends a new message while you are mid-task, it will be appended to the conversation and you can keep going."""


_log = logging.getLogger("monox.runtime")


# run.py 自己的 PID 文件路径 + Runtime 默认占用的三个端口。
# `--stop` 用 PID 文件找本进程；用端口扫残留（PID 文件丢失或之前 crash 留下的进程）。
PID_FILE = Path(".monox/runtime.pid")
DEFAULT_RUNTIME_PORTS = (8765, 8766, 8767)  # ws server / monodesk ws / health


def _pid_alive(pid: int) -> bool:
    """POSIX process 存活判定（pid 0 / 自己 / 不存在都判否）。"""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 有进程但不是我们——视为"活"
    return True


def _kill_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _pids_on_port(port: int) -> list[int]:
    """用 lsof 找监听给定端口的进程 PID（macOS / Linux 都自带）。

    返回 [] 表示端口空闲或 lsof 不可用。
    """
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def stop_run(ports: tuple[int, ...] = DEFAULT_RUNTIME_PORTS) -> int:
    """`run.py --stop` 实现：先杀 PID 文件，再扫端口残留。

    返回 0 表示干净退出，1 表示有进程没杀掉（用户可重试 / 手 kill）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monox:%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    targets: set[int] = set()

    # 1) PID 文件
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
        except ValueError:
            pid = 0
        if _pid_alive(pid):
            targets.add(pid)
            _log.info("pid file points to live pid=%d", pid)
        else:
            _log.info("pid file stale (pid=%d not alive); removing", pid)
        PID_FILE.unlink(missing_ok=True)
    else:
        _log.info("no pid file at %s", PID_FILE)

    # 2) 端口扫
    for port in ports:
        holders = _pids_on_port(port)
        if holders:
            _log.info("port %d held by pids=%s", port, holders)
            targets.update(holders)
        else:
            _log.info("port %d free", port)

    if not targets:
        _log.info("nothing to stop")
        return 0

    # 3) SIGTERM → 等 → SIGKILL
    survivors: list[int] = []
    for pid in sorted(targets):
        _log.info("SIGTERM pid=%d", pid)
        _kill_pid(pid, signal.SIGTERM)

    import time
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if all(not _pid_alive(p) for p in targets):
            break
        time.sleep(0.2)
    else:
        for pid in sorted(targets):
            if _pid_alive(pid):
                _log.warning("pid=%d still alive; SIGKILL", pid)
                _kill_pid(pid, signal.SIGKILL)
                survivors.append(pid)

    # 再扫一次端口，确认释放
    time.sleep(0.3)
    leftover = {p: _pids_on_port(p) for p in ports}
    still_held = {p: ps for p, ps in leftover.items() if ps}
    if still_held:
        _log.error("after kill, ports still held: %s", still_held)
        return 1
    _log.info("all clean")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MonoX runtime (SessionManager + ws server + /health + config-driven channels)",
    )
    parser.add_argument("config", nargs="?", default="config.toml", help="config.toml path")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show loop internals (state transitions, step metrics). Reasoning is always shown.",
    )
    parser.add_argument(
        "--server-host",
        default=None,
        help="Override [server].host from config (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=None,
        help="Override [server].port from config (default: 8765).",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=None,
        help="Override health server port (default: 8767).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        help="Seconds before an idle session is destroyed (default: 300).",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Kill any running Runtime (PID file + port sweep on :8765/:8766/:8767) and exit.",
    )
    return parser.parse_args()


def _ensure_dirs(paths: dict[str, Path]) -> None:
    for key in ("workspace", "memory", "memory_notes", "tmp_root", "skills_root"):
        paths[key].mkdir(parents=True, exist_ok=True)


async def run(cfg_path: str, args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monox:%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # 写 PID 文件——`run.py --stop` 用它定位本进程；atexit + SIGINT/SIGTERM 触发清理。
    # 写在 logging 之后：日志初始化失败不会留孤儿 PID 文件。
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n")

    def _remove_pid() -> None:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_remove_pid)


    cfg = Config.load(cfg_path)
    if args.server_host or args.server_port:
        cfg = dataclasses.replace(
            cfg,
            server=dataclasses.replace(
                cfg.server,
                host=args.server_host or cfg.server.host,
                port=args.server_port or cfg.server.port,
            ),
        )
    paths = session_paths(cfg)
    _ensure_dirs(paths)

    runner = BashRunner()
    budget_tool = ReadToolResultBudgetTool()
    tools = ToolRegistry(
        [
            BashTool(runner, paths["workspace"]),
            SkillLoadTool(Path(cfg.sandbox.skills_root)),
            WaitIoTool(),
            budget_tool,
        ]
    )

    memory = FsMemoryStore(Path(cfg.sandbox.memory_root))
    skill_summary = SkillSummaryLoader(Path(cfg.sandbox.skills_root)).summary()
    if cfg.compression_llm is None:
        raise RuntimeError(
            "missing [llm.compression]: a compression model is required; "
            "configure it in config.toml"
        )

    llm = OpenAIStreamProxy(cfg.llm)
    compression_llm = OpenAIStreamProxy(cfg.compression_llm)

    compression = CompressionService(
        budget_tool=budget_tool,
        llm=compression_llm,
        memory=memory,
    )

    memory_root = Path(cfg.sandbox.memory_root)

    server = RuntimeServer(
        cfg.server,
        default_session_key=cfg.session_key,
    )

    # SessionManager ↔ RuntimeServer 通过 async 回调协作
    async def _register(sk: str, q: asyncio.Queue) -> None:
        await server.register_outbound_queue(sk, q)

    async def _unregister(sk: str) -> None:
        await server.unregister_outbound_queue(sk)

    session_mgr = SessionManager(
        llm=llm,
        compression_llm=compression_llm,
        tools=tools,
        compression=compression,
        memory=memory,
        memory_root=memory_root,
        system_prompt=DEFAULT_SYSTEM,
        skill_summary=skill_summary,
        outbound_register=_register,
        outbound_unregister=_unregister,
        idle_timeout_sec=float(args.idle_timeout) if args.idle_timeout else SessionManager.IDLE_TIMEOUT_SEC,
    )
    server.set_inbound_handler(session_mgr.dispatch_inbound)

    health_port = args.health_port if args.health_port is not None else 8767
    health = HealthServer(
        HealthServerConfig(port=health_port),
        session_provider=session_mgr.active_sessions,
    )

    print(
        f"[monox-runtime] ws :{cfg.server.port} (default_session_key={cfg.session_key!r}), "
        f"health :{health_port}, idle_timeout={session_mgr._idle_timeout_sec}s",
        flush=True,
    )

    await session_mgr.start()
    try:
        await asyncio.gather(server.run(), health.run())
    finally:
        await session_mgr.stop()
        await server.stop()
        await health.stop()


if __name__ == "__main__":
    args = parse_args()
    if args.stop:
        ports: tuple[int, ...] = DEFAULT_RUNTIME_PORTS
        # 尊重 CLI 端口覆盖
        if args.server_port is not None:
            ports = tuple({args.server_port, *(p for p in ports if p != 8765)})
        if args.health_port is not None:
            ports = tuple({args.health_port, *(p for p in ports if p != 8767)})
        sys.exit(stop_run(ports))
    asyncio.run(run(args.config, args))
