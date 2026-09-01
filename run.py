"""MonoX Runtime 启动入口。

Runtime 进程做三件事：
  1) 起 RuntimeServer（ws server，:8765，给 channel client 连 + 外部工具）
  2) 起 HealthServer（HTTP `/health` 端点，:8767）
  3) 起 SessionManager（管理多 session_key 的 LoopEngine，idle 自动销毁 + 恢复）

channel 是独立进程，各自从 config.toml 读配置。terminal / textual 手动启动；
feishu 由 run.py 代拉起（config 配了 app_id/app_secret 时 spawn 子进程）。
各 channel 启动方式：
  terminal:   uv run python -m extensions.channels.terminal
  monodesk:    MonoDesk 桌面 app（直接连 Runtime ws://127.0.0.1:8765，无独立进程）
  feishu:      run.py 自动拉起（[[channels]] kind="feishu" 配好 app_id/app_secret）
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

from core.async_task import AsyncTaskManager
from core.config import Config, session_paths
from core.logging_setup import setup_logging
from core.debug_server import DebugServer, DebugServerConfig, FsTraceProvider
from core.health_server import HealthServer, HealthServerConfig
from core.llm_proxy import LlmProxy
from core.loop import (
    BashTool,
    MultimodalUnderstandTool,
    ReadToolResultBudgetTool,
    SkillLoadTool,
    ToolRegistry,
    WaitIoTool,
)
from core.loop.compression import CompressionService
from core.loop.tools.cancel_task import CancelTaskTool
from core.loop.tools.fork_task import ForkTaskTool
from core.loop.tools.poll_task import PollTaskTool
from core.memory import FsMemoryStore
from core.protocol.wire_frames import FrameType
from core.runtime_server import RuntimeServer
from core.sandbox import BashRunner
from core.session_manager import SessionManager
from core.skill_service import SkillService
from core.skill_sync import sync_extension_skills
import core.loop.event_format  # noqa: F401 — used by DEFAULT_SYSTEM_TEMPLATE 字符串拼接


DEFAULT_SYSTEM_TEMPLATE = """You are MonoX, a coding agent. You run inside a sandboxed bash environment.

Plan briefly, then execute. Use bash for all I/O. Use skill_load to fetch details of a skill before invoking it.

For long-running tasks (e.g. build/test servers, long compiles, background daemons), use fork_task to run them asynchronously instead of blocking the main loop.

You can receive images as <attachment url="..."> elements in user events. To understand an image, call multimodalunderstand(attachment_url="...") with the file path or URL shown in the attachment's `url` attribute.

Tool results may be L1-compressed; if you see budget_id, call read_tool_result_budget(budget_id=...) for the full version.

When you are done with the current turn and ready to receive the next message, call wait_io. If the user sends a new message while you are mid-task, it will be appended to the conversation and you can keep going.

Sandbox paths (absolute paths resolved by Runtime; placeholders below get substituted with concrete
paths from `SandboxConfig` at session start — never resolve them relative to your cwd):

- workspace cwd:  `{MONOX_WORKSPACE_DIR}`   (your `cd` lands here; session-isolated)
- memory:         `{MONOX_MEMORY_DIR}`      (cross-session, long-term; do NOT `cd` here)
- skills:         `{MONOX_SKILLS_DIR}`
- scratch tmp:    `{MONOX_TMP_DIR}`
- monox home:     `{MONOX_HOME}`

Use the absolute paths **as-is** in commands: `cat {MONOX_MEMORY_DIR}/Memory.md`, `ls {MONOX_SKILLS_DIR}`,
write notes under `{MONOX_MEMORY_DIR}/notes/<topic>.md`. Never prepend your cwd to these paths —
they are already absolute, and concatenating them produces nested `…/cwd/.monox/...` dirs that
Runtime cannot see. State and trace roots exist for Runtime internals only and are not in this prompt.

""" + core.loop.event_format.EVENT_SCHEMA_DOC


def build_path_vars(cfg: Config) -> dict[str, str]:
    """从 SandboxConfig 构造 prompt 的 `{MONOX_*}` 占位符替换表。

    只塞「可见组」：workspace / memory / skills / tmp / home。state / traces 由
    Runtime 代码直接使用 cfg.sandbox.state_root / traces_root，不进 prompt、不进替换表。

    **强制 .resolve() 成绝对路径**——LLM cwd = `<workspace>/<sk>/`，相对路径
    在 prompt 里出现时 LLM 一旦 `cd` 进去就会嵌到 cwd 里去，导致 `.monox/` 嵌套、
    内存分散到错地方。绝对路径从任何 cwd 出发 resolve 都一样。
    """
    def _abs(p: str | Path) -> str:
        return str(Path(p).expanduser().resolve())

    ws_root = Path(cfg.sandbox.workspace_root).expanduser().resolve()
    return {
        "MONOX_HOME": str(ws_root.parent),
        "MONOX_WORKSPACE_DIR": str(ws_root / cfg.session_key),
        "MONOX_MEMORY_DIR": _abs(cfg.sandbox.memory_root),
        "MONOX_SKILLS_DIR": _abs(cfg.sandbox.skills_root),
        "MONOX_TMP_DIR": _abs(cfg.sandbox.tmp_root),
    }


def render_default_system(cfg: Config) -> str:
    """用 cfg 把占位符替换成真实路径，构造最终 system prompt。"""
    return DEFAULT_SYSTEM_TEMPLATE.format(**build_path_vars(cfg))


_log = logging.getLogger("monox.runtime")


# run.py 自己的 PID 文件路径 + Runtime 默认占用的两个端口。
# `--stop` 用 PID 文件找本进程；用端口扫残留（PID 文件丢失或之前 crash 留下的进程）。
PID_FILE = Path(".monox/runtime.pid")
DEFAULT_RUNTIME_PORTS = (8765, 8767, 8768)  # ws server / health / debug (trace)


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
        help="Kill any running Runtime (PID file + port sweep on :8765/:8767/:8768) and exit.",
    )
    return parser.parse_args()


def _ensure_dirs(paths: dict[str, Path]) -> None:
    """Runtime 启动时把目录骨架建好——LLM 任何 cwd 都不会踩到不存在的目录。

    注意 state/ 是 Runtime 内部 state，cwd 也叫 `<sk>/`，但跟 workspace/<sk>/
    完全分开的 root，不要合并。
    """
    # 四个 root
    for key in ("workspace", "memory", "state", "traces_root", "tmp_root", "skills_root"):
        if paths[key] is not None:
            paths[key].mkdir(parents=True, exist_ok=True)
    # per-session 子目录
    paths["workspace"].mkdir(parents=True, exist_ok=True)
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    if "traces" in paths:
        # traces 是 per-session 文件路径；其 parent 就是 traces/<sk>/
        paths["traces"].parent.mkdir(parents=True, exist_ok=True)
    # memory 是全局根；notes/ 在下面
    paths["memory_notes"].mkdir(parents=True, exist_ok=True)

    # Memory.md 首次预填一行「empty」——agent cat 时能看到状态，不用 ls 探测
    mem_index = paths["memory_index"]  # → Memory.md
    if not mem_index.exists():
        mem_index.write_text("# Memory index\n(empty — write your first topic when ready)\n")


def _spawn_feishu(cfg: Config) -> subprocess.Popen | None:
    """run.py 代拉起 feishu channel（仍是独立进程）。

    从 config 的 [[channels]] 找 kind="feishu"，配了 app_id/app_secret 就 spawn
    `python -m extensions.channels.feishu`；没配（注释掉或留空）则跳过。
    feishu 子进程内部自带 supervisor（run_channel 崩了退避重启），run.py 只管起 +
    shutdown 时 SIGTERM。
    """
    for ch in cfg.multi_channel.channels:
        if ch.kind != "feishu":
            continue
        app_id = ch.options.get("app_id", "")
        app_secret = ch.options.get("app_secret", "")
        if not app_id or not app_secret:
            _log.warning(
                "[feishu] app_id/app_secret 为空，跳过启动；请在 config 的 "
                '[[channels]] kind="feishu" 里填真实凭据'
            )
            return None
        allowed = ch.options.get("allowed_chats", [])
        cmd = [
            sys.executable, "-m", "extensions.channels.feishu",
            f"--runtime-url=ws://{cfg.server.host}:{cfg.server.port}",
            f"--app-id={app_id}",
            f"--app-secret={app_secret}",
            f"--allowed-chats={','.join(allowed)}",
        ]
        proc = subprocess.Popen(cmd)
        _log.info("[feishu] spawned pid=%d", proc.pid)
        return proc
    return None


async def run(cfg_path: str, args: argparse.Namespace) -> None:
    # 文件日志：写到 .monox/logs/monox-YYYY-MM-DD.log，按日期分。
    # 调试 LLM 用量 / wire frame 时直接 tail 这个文件即可，不必去翻 systemd / docker。
    log_dir = Path(".monox/logs")
    out_path = setup_logging(log_dir=log_dir, level=logging.INFO)
    if out_path is not None:
        _log.info("file logging enabled → %s", out_path)

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
    skills_root = Path(cfg.sandbox.skills_root)
    # 启动时把公共 skill 库 sync 到 runtime：补缺失，不覆盖现有。
    # 详见 spec/ARCHITECTURE.md 9.1
    sync_extension_skills(
        Path(cfg.sandbox.extensions_skills_dir),
        skills_root,
        state_path=Path(cfg.sandbox.state_root) / "skill-sync.json",
    )
    skill_service = SkillService(skills_root, max_l1=cfg.sandbox.skills_max_l1)
    tools = ToolRegistry(
        [
            BashTool(runner, paths["workspace"]),
            SkillLoadTool(skill_service),
            MultimodalUnderstandTool(),
            WaitIoTool(),
            budget_tool,
        ]
    )

    memory = FsMemoryStore(Path(cfg.sandbox.memory_root))
    if cfg.compression_llm is None:
        raise RuntimeError(
            "missing [llm.compression]: a compression model is required; "
            "configure it in config.toml"
        )

    # LLMConfig 在 Config.from_dict() 里已经注入了 providers，直接用即可
    llm = LlmProxy(cfg.llm)
    compression_llm = LlmProxy(cfg.compression_llm)

    compression = CompressionService(
        budget_tool=budget_tool,
        llm=compression_llm,
    )

    workspace_root = Path(cfg.sandbox.workspace_root)
    state_root = Path(cfg.sandbox.state_root)
    traces_root = Path(cfg.sandbox.traces_root)

    server = RuntimeServer(
        cfg.server,
        default_session_key=cfg.session_key,
        default_model=cfg.llm.model,
        providers=cfg.providers,
    )

    # SessionManager ↔ RuntimeServer 通过 async 回调协作
    async def _register(sk: str, q: asyncio.Queue) -> None:
        if sk.startswith("async:"):
            return  # child session：不注册 RuntimeServer consumer，output_q 归 AsyncTaskBridge
        await server.register_outbound_queue(sk, q)

    async def _unregister(sk: str) -> None:
        await server.unregister_outbound_queue(sk)

    session_mgr = SessionManager(
        llm=llm,
        compression_llm=compression_llm,
        tools=tools,
        compression=compression,
        memory=memory,
        state_root=state_root,
        traces_root=traces_root,
        system_prompt=render_default_system(cfg),
        skill_service=skill_service,
        path_vars=build_path_vars(cfg),
        outbound_register=_register,
        outbound_unregister=_unregister,
        idle_timeout_sec=float(args.idle_timeout) if args.idle_timeout else SessionManager.IDLE_TIMEOUT_SEC,
    )
    server.set_inbound_handler(session_mgr.dispatch_inbound)

    # AsyncTask（subagent 是经典场景）：见 spec/requirements/async-task.md
    async def _broadcast_async_task(ftype: str, data: dict) -> None:
        await server.broadcast_async_task(ftype, data)

    async def _handle_async_task_inbound(ftype: str, data: dict) -> None:
        _log.info("[async_task_inbound] ftype=%s data=%s", ftype, data)
        if ftype == FrameType.ASYNC_TASK_CANCEL:
            ok = await async_task_mgr.cancel(
                data.get("task_id") or "", reason=data.get("reason") or "user"
            )
            _log.info("[async_task_inbound] cancel result: task_id=%s ok=%s", data.get("task_id"), ok)
        elif ftype == FrameType.ASYNC_TASK_LIST_QUERY:
            filt = data.get("filter") or {}
            await async_task_mgr.emit_list(
                session_key=data.get("session_key") or "",
                status=filt.get("status"),
            )

    async_task_mgr = AsyncTaskManager(
        session_manager=session_mgr,
        state_root=state_root,
        on_event=_broadcast_async_task,
        default_timeout_sec=cfg.async_task.default_timeout_sec,
        bash_cwd=paths["workspace"],  # bash_long 的 cwd 与 agent 的 bash tool 一致
    )
    async_task_mgr.load_from_disk()
    server.set_async_task_handler(_handle_async_task_inbound)
    tools.add(ForkTaskTool(async_task_mgr))
    tools.add(PollTaskTool(async_task_mgr))
    tools.add(CancelTaskTool(async_task_mgr))

    health_port = args.health_port if args.health_port is not None else 8767
    health = HealthServer(
        HealthServerConfig(port=health_port),
        session_provider=session_mgr.active_sessions,
    )

    # 可观测性 debug server（:8768）：trace / debug 接口给 MonoDesk 用。
    debug_port = int(os.environ.get("MONOX_DEBUG_PORT", "8768"))
    debug = DebugServer(
        DebugServerConfig(host=cfg.server.host, port=debug_port),
        trace_provider=FsTraceProvider(traces_root),
        skill_service=skill_service,
        attachments_root=Path(cfg.sandbox.tmp_root),
    )

    print(
        f"[monox-runtime] ws :{cfg.server.port} (default_session_key={cfg.session_key!r}), "
        f"health :{health_port}, debug :{debug_port}, idle_timeout={session_mgr._idle_timeout_sec}s, "
        f"async_tasks_restored={len(async_task_mgr.list())}",
        flush=True,
    )

    # feishu 由 run.py 代拉起（config 配了 app_id/app_secret 才 spawn；否则 None）
    feishu_proc = _spawn_feishu(cfg)

    await session_mgr.start()
    try:
        await asyncio.gather(server.run(), health.run(), debug.run_server())
    finally:
        if feishu_proc is not None and feishu_proc.poll() is None:
            feishu_proc.terminate()
        await async_task_mgr.shutdown()  # 先收摊 async task（flush task.json）
        await session_mgr.stop()
        await server.stop()
        await health.stop()
        await debug.stop()


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
