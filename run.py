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
    ReadDocTool,
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


DEFAULT_SYSTEM_TEMPLATE = """You are MonoX, a personal agent runtime. You serve one user across sessions; your role grows with theirs over time.

You run inside a sandboxed bash environment. Plan briefly, then execute. Use bash for all I/O. Use skill_load to fetch details of a skill before invoking it.

## Async tasks (fork / poll / cancel)

Long-running commands should run via `fork_task`, not directly via `bash` —
the parent loop returns immediately, the task runs in the background. This
includes:
- Builds, compiles, test suites, dev servers
- **CLI calls that take >10s** — `exec_cli mono_i2i apply`, `mono_i2i raw`,
  `mono_asr transcribe` (long audio), any `mono_*` with 30-60s upstream latency

**Pattern:**
1. Call `fork_task(description=..., kind='subagent', meta={{'kind': '...'}})` —
   returns `{{task_id, status}}`. The child's first user turn IS your description.
2. Continue working. When the task completes you receive an
   `<event kind='system' event_type='async-task-result' meta='{{task_id, status, kind}}'>`
   in your input — the engine re-injects it, waking the loop.
3. `poll_task(task_ids=[id])` for progress; `cancel_task(task_id=id)` to abort.

**Parallelize independent work.** If you have N independent long-running calls
(e.g. 3 `i2i apply` requests for different templates, or 3 audio files to
transcribe), `fork_task` all of them in ONE assistant turn (N parallel tool
calls), then wait for N `async-task-result` events. Don't serialize.

**Subagent contract:** forked tasks must NOT call `wait_io` mid-task —
the subagent's final message IS the deliverable. A subagent that ends its
turn via `wait_io` is marked completed with a partial result.

You can receive images as <attachment path="..."> elements in user events. The `path` is a local absolute file path. To understand an image, call multimodalunderstand(attachment_url="<path>") — pass the `path` attribute value as-is.

## Image preview — show, don't just describe

When you generate, reference, or otherwise surface an image, embed it with markdown
image syntax `![alt](path)` so MonoDesk renders it inline. Use the local `path` (Tauri loads it via asset protocol). Plain text like `输出路径: /path/xxx.png` or `链接: https://...` will NOT preview — the UI only
honors the `![alt](url)` markdown form.

**Three URL flavors, three rules:**

1. **Public HTTPS (best, use this first):** OSS / CDN URLs returned by upstream APIs
   (e.g. `image_url` field from `mono_i2i apply`, 24h-valid but CORS-friendly).
   Embed directly:
   ```
   ![风格化结果](https://dashscope-...xxx.png)
   ```

2. **Local attachment (good):** user-attached files are uploaded to a local
   tmp dir; the attachment's `path` attribute (or `path` field) is the local
   absolute file path. Tauri loads it via asset protocol, so embedding the
   `path` directly renders inline:
   ```
   ![原始图](/Users/.../.monox/tmp/attachments/xxx.png)
   ```

3. **Local file path (also works):** any local absolute path under the
   workspace is fine — Tauri loads via asset protocol, no upload needed.

**`mono_i2i apply` / `mono_i2i raw` output specifically:** response includes both
`saved_path` (local, won't preview) and `image_url` (OSS, 24h valid). Always embed
`image_url` directly. If the user may want a persistent copy beyond 24h, also upload
`saved_path` and embed the debug URL too.

**Long URLs must use `<url>` form, never wrap across lines.** OSS URLs are 200+ chars
with `?Expires=&Signature=...`. Plain `![alt](url)` form has two failure modes:
1. You auto-wrap the URL at a line break — the renderer then sees a malformed image
   and shows a broken icon.
2. You put the URL on one line but the line is too long — the renderer captures it
   fine but the chat history looks ugly.

Use the CommonMark angle-bracket form: `![alt](<url>)`. The `<>` lets the URL contain
whitespace and survive line wrapping in the renderer.

Examples:
- Short URL fine as-is: `![原图](https://x.com/foo.png)`
- Long OSS URL **always** use `<>`: `![油画](<https://dashscope-a717.oss-accelerate.aliyuncs.com/1d/7f/x.png?Expires=1789573371&OSSAccessKeyId=LTAI5tPxpi>)`
- Same applies to plain `[link](<url>)` if the URL is long.

## Evidence Chain（ref）

调研 / 总结 / 多源对比场景下，**关键结论必须给出 ref**，让用户能验证来源。

### 语法

```
[[ref id=N type=TYPE key=value ...]]
```

- 紧跟被标注的观点之后（行内）
- `id` 从 1 开始递增（同一 final answer 内唯一）
- `type` 与 key 见下方

### 什么时候 emit

- **关键结论**（不是显而易见的陈述）：✓ emit
- **常识 / 简单事实**（如「Python 是动态类型语言」）：✗ 不 emit
- **数据 / 引用 / 数字**（如「2024 年全球 AI 市场规模 X 亿」）：✓ emit
- **用户原文 / 之前对话片段**（如「你之前提到…」）：✓ emit snippet
- **闲聊 / 单步工具调用结果汇报**：✗ 不 emit（除非结果是关键决策依据）

### 常用 type

| type | 场景 | 必填字段 |
|---|---|---|
| `link` | 外部文章 / 文档 / GitHub URL | url, title |
| `memory` | 你从 memory 里读到的关键事实 | key（memory 索引）, snippet（≤ 200 字符） |
| `snippet` | 用户之前对话 / 某段上下文 | from（来源描述）, content |
| `tool` | 之前某次 tool 调用的关键返回 | tool_name, call_id, result_summary |

未识别的 type 也允许 —— 前端会降级显示所有 key=value。

### 正确示例

```
MonoX 是 2022 年成立的 AI agent runtime [1]，核心定位是自托管 ReAct 循环 [2]。
[[ref id=1 type=link url="https://monox.dev/about" title="MonoX 官网 About"]]
[[ref id=2 type=memory key="identity/monox" snippet="MonoX 2022 年成立，定位 self-hosted agent runtime"]]
[[ref id=3 type=snippet from="你之前提到想要 self-hosted agent" content="想要一个能在本地跑的 AI agent runtime"]]
[[ref id=4 type=tool tool_name="mono_search" call_id="c42" result_summary="5 篇关于 AI agent runtime 的文章"]]
```

### 错误示例

- `MonoX 是 2022 年成立的 [[ref id=1 type=link url=...]]` —— ref 应该放在观点**之后**，不是插入观点中间
- 整段文字一个 ref 也没有，但里面包含「2022 年成立」「AI agent runtime」等关键事实 —— 关键结论必须 ref
- `[[ref id=1 type=link url="..."]]` 不带 title —— 前端只显示 URL，不直观
- ref 出现在 reasoning 或 tool_call 里 —— **ref 只能出现在 final answer 的文本流**（reasoning / tool_call 里的 ref 不会被前端解析）

### 适用边界

- final answer 是 **文本流**（renderMarkdown 会扫到）；reasoning / tool_call args / system note 里出现的 ref token **不会被前端解析**（这些 channel 不走 markdown pipeline）
- 如果 final answer 里**完全没有任何可标注的来源**（如纯闲聊 / 单句回复 / 你自己推理得出的结论），整段可以零 ref—— 不要为了凑数硬塞
- 推断 / 推测（inference）标注 ref 时用 `snippet` + `from="模型推断"` 让用户知道这是模型自己的推测，不是外部来源

Tool results may be L1-compressed; if you see budget_id, call read_tool_result_budget(budget_id=...) for the full version.

## Reading documents

Use `read_doc` for attached documents (PDF / txt / md / csv / json) — it
extracts text via `pypdf` (cheap, offline). Reserve `multimodal_understand`
for images, and for scanned PDFs where `read_doc` returns empty stdout
(no text layer — fall back to vision OCR).

## bash tool `target` field (MonoDesk display)

When you call `bash`, fill the optional `target` parameter with a one-line
human-readable summary of what the command does — MonoDesk shows it next to
the BASH label so the user can scan a long tool sequence at a glance.

- Keep it under ~10 Chinese characters (or ~30 ASCII). MonoDesk truncates
  beyond that, but writing long wastes tokens.
- Describe the *intent* (what / why), not the command itself.
  - Good: "列出 workspace 内容" / "run unit tests" / "install pypdf"
  - Bad:  "ls -la workspace" (echoes cmd) / "ls" (too vague)
- If unsure, skip it — `target` is optional. MonoDesk falls back to the
  first ~30 chars of `cmd` when `target` is missing.
- `target` is display-only; the tool itself ignores it.

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
DEFAULT_RUNTIME_PORTS = (8765, 8767, 8768, 8769)  # ws server / health / debug / cli


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
        "--debug-port",
        type=int,
        default=None,
        help="Override debug server port (default: 8768).",
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
        help="Kill any running Runtime (PID file + port sweep on :8765/:8767/:8768/:8769) and exit.",
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
        feishu_cfg = ch.channel_raw.get("feishu", {})
        app_id = feishu_cfg.get("app_id", "")
        app_secret = feishu_cfg.get("app_secret", "")
        if not app_id or not app_secret:
            _log.warning(
                "[feishu] app_id/app_secret 为空，跳过启动；请在 config 的 "
                '[[channels]] kind="feishu" 里填真实凭据'
            )
            return None
        allowed = feishu_cfg.get("allowed_chats", [])
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
            ReadDocTool(paths["workspace"]),
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

    # CLI server 端口（:8769）：LLM 通过 exec_cli 调用 mono_* 子命令需要它。
    cli_port = int(os.environ.get("MONOX_CLI_PORT", "8769"))

    print(
        f"[monox-runtime] ws :{cfg.server.port} (default_session_key={cfg.session_key!r}), "
        f"health :{health_port}, debug :{debug_port}, cli :{cli_port}, "
        f"idle_timeout={session_mgr._idle_timeout_sec}s, "
        f"async_tasks_restored={len(async_task_mgr.list())}",
        flush=True,
    )

    # feishu 由 run.py 代拉起（config 配了 app_id/app_secret 才 spawn；否则 None）
    feishu_proc = _spawn_feishu(cfg)

    # CLI server 由 run.py 代拉起（extension 能力的 HTTP 入口，:8769）。
    # 无条件起 —— LLM 通过 exec_cli 调用 mono_* 子命令需要它。
    cli_proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.cli.inner.server",
         f"--host={cfg.server.host}", f"--port={cli_port}"],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    _log.info("[cli-server] spawned pid=%d on %s:%d", cli_proc.pid, cfg.server.host, cli_port)

    await session_mgr.start()
    try:
        await asyncio.gather(server.run(), health.run(), debug.run_server())
    finally:
        if feishu_proc is not None and feishu_proc.poll() is None:
            feishu_proc.terminate()
        if cli_proc.poll() is None:
            cli_proc.terminate()
            try:
                cli_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cli_proc.kill()
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
        if args.debug_port is not None:
            ports = tuple({args.debug_port, *(p for p in ports if p != 8768)})
        cli_port_env = int(os.environ.get("MONOX_CLI_PORT", "8769"))
        ports = tuple({cli_port_env, *(p for p in ports if p != 8769)})
        sys.exit(stop_run(ports))
    asyncio.run(run(args.config, args))
