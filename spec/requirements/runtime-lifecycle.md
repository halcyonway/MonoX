# runtime-lifecycle: 进程生命周期管理（PID 文件 / `--stop` / channel supervisor）

## 问题

MonoX Runtime 跑长时遇到两个操作层面痛点：

1. **端口残留**：`uv run python run.py` 启动时 `monoDeskChannel.start()` 报 `OSError: [Errno 48] Address already in use (127.0.0.1, 8766)`——之前某次 crash / kill -9 留下的 run.py 子进程（或自己）还在占着端口。用户除了 `lsof | grep | kill` 没别的招；下次再启还会撞同样的端口。
2. **Channel crash 拖死整个 Runtime**：monoDesk channel 内部用 `websockets.sync.server` 在独立线程起 ws server（`:8766`），飞书 channel 依赖 `lark-oapi` 长连接。任一 channel 内部抛异常（比如端口冲突、暂时性断连），supervising 的 `asyncio.gather` 直接 cancel 整个 Runtime——其他 channel 跟着死，用户体验上是"我什么都没干 Runtime 就崩了"。
3. **没有"停止"语义**：用户想关 Runtime，除了 Ctrl+C / `kill <pid>` 没别的；PID 文件从来没写过，重启后又得手动找老进程。

`spec/requirements/shutdown.md` 描述的是另一回事（"engine 响应 channel 关闭 → 干净终止"），跟本篇操作员级进程管理不重叠。

## 目标与边界

### 目标

1. **PID 文件持久化**：Runtime 启动时写 `.monox/runtime.pid`，干净退出时自动删；操作员可读 PID 文件 → 直接 kill
2. **`--stop` CLI 子命令**：单命令清理所有 Runtime 相关进程（PID 文件进程 + 端口残留）
3. **Channel supervisor**：channel 内部抛异常时按指数退避自动重启，`CancelledError` 不重启（让 Runtime 整体 shutdown 时能干净退出）
4. **SIGINT/SIGTERM 干净退出**：用户 Ctrl+C / `kill <pid>` 触发 Runtime 走正常 shutdown 路径（清 PID 文件、关 ws server、drain in-process channel）

### 边界

- **不改**：`core/runtime_server.py` / `core/session_manager.py` / `core/health_server.py`（本次没动）
- **不改** `Channel` Protocol（supervisor 在 `_runtime.run_channel` 包，不侵入 adapter）
- **不引入** 进程间 socket 通信 / 信号 handler 链——只走 PID + 端口
- **不跨平台硬要求**：PID / signal / `os.kill` 走 POSIX；`lsof` 用于端口扫描，macOS / Linux 自带（CI 也跑 Linux），Windows 不支持（已知限制）

---

## 设计

### 进程拓扑（in-process channel 模型下）

```
uv run python run.py
  └─ Runtime 进程 (single process)
       ├─ RuntimeServer  :8765  (ws server)
       ├─ HealthServer   :8767  (HTTP /health)
       ├─ SessionManager (in-process, 内部跑 N 个 LoopEngine)
       └─ per-channel supervisor task (in-process)
             ├─ terminal   ──>  TerminalChannel (stdio TUI)
             ├─ monodesk   ──>  MonoDeskChannel (ws server :8766)
             ├─ feishu     ──>  FeishuChannel (lark-oapi WS + HTTP)
             └─ textual_chat ─> TextualChannel (textual App)
```

每个 channel supervisor = `asyncio.create_task(run_channel(ch, ws_client=ws))`；
`run_channel` 内部用 `_run_once` + 退避循环包 crash。

### PID 文件

**路径**：`PID_FILE = Path(".monox/runtime.pid")`

**生命周期**：
- `run()` 启动时（logging 之后）：`PID_FILE.parent.mkdir(parents=True, exist_ok=True)` + `PID_FILE.write_text(f"{os.getpid()}\n")`
- 干净退出：`atexit.register(_remove_pid)` + SIGINT/SIGTERM signal handler 调 `_remove_pid`
- 进程被 `kill -9` / SIGKILL：atexit 不触发，PID 文件残留——这是 `--stop` 必须能处理的情况

**配套常量**（`run.py`）：
```python
PID_FILE = Path(".monox/runtime.pid")
DEFAULT_RUNTIME_PORTS = (8765, 8766, 8767)  # ws server / monodesk ws / health
```

### `--stop` 子命令

```bash
uv run python run.py --stop
```

**实现**（`run.py:stop_run(ports)`）：

| 步骤 | 行为 | 失败处理 |
|---|---|---|
| 1. PID 文件 | 读 PID → 加到 `targets: set[int]`；删 PID 文件 | PID 不存在 / 不活 → 跳过；PID 文件不是数字 → 跳过 |
| 2. 端口扫 | `lsof -ti tcp:{port} -sTCP:LISTEN` 逐端口扫；命中的 PID 也加到 `targets` | lsof 不可用 / 超时 → 该端口跳过 |
| 3. SIGTERM | 给所有 `targets` 发 SIGTERM | 进程不存在 → 忽略 |
| 4. 等 5s | `time.sleep(0.2)` 轮询；5s 内全死 → 跳到步骤 6 | 部分还活 → 继续 |
| 5. SIGKILL | 还活的发 SIGKILL，记录 `survivors` | — |
| 6. 复扫端口 | 300ms 后再扫一次；全空 → exit 0；非空 → exit 1 | exit 1 表示有进程僵尸化（罕见，手 kill） |

**端口可覆盖**：`__main__` 块读 `--server-port` / `--health-port` CLI 覆盖默认 ports tuple。

**退出码**：
- `0` —— 全部清干净
- `1` —— 有进程没杀掉 / 端口仍被占

### Signal handler + atexit

`run()` 在写 PID 文件后注册：

```python
def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

atexit.register(_remove_pid)

def _on_signal(signum, _frame):
    _log.info("received signal %d, shutting down", signum)
    _remove_pid()
    os._exit(0)   # signal handler 在主线程触发；不走 asyncio 取消路径

signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)
```

设计动机：
- `os._exit(0)` 而不是 `raise`/`loop.stop()`：signal handler 在主线程触发时 event loop 调度可能还没醒；`os._exit` 立即终止进程，保证 PID 文件被清、ws port 释放
- `atexit` 兜底：正常 Runtime 走 shutdown 路径（`asyncio.gather` cancel → server.stop() → session_mgr.stop()），atexit 仍会被调，PID 文件被清

### Channel supervisor

**位置**：`extensions/channels/_runtime.py:run_channel(channel, *, ws_client)`

**行为**（伪代码）：

```python
SUPERVISOR_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

async def run_channel(channel, *, ws_client):
    attempt = 0
    while True:
        try:
            await _run_once(channel, ws_client)   # 起 channel + 双 pump gather
            return                                   # 正常退出（不应发生）→ 不重启
        except asyncio.CancelledError:
            raise                                    # Runtime 整体 shutdown → 不重启
        except Exception as e:
            attempt += 1
            delay = SUPERVISOR_BACKOFF[min(attempt - 1, len(SUPERVISOR_BACKOFF) - 1)]
            _log.error("channel crashed (%s: %s); restart in %.1fs (attempt %d)",
                       type(e).__name__, e, delay, attempt)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
```

**`_run_once` 职责**：原 main_loop——`await channel.start()` + 起 `ws_client.run()` task + `gather(pump_inbound, pump_outbound)` + finally `await ws_client.stop()` + `channel.stop()`。

**退避表语义**：
- 第 1 次崩溃 → 等 0.5s 重启
- 第 2 次 → 1s
- ...
- 第 6 次及以后 → 16s（封顶）
- 退出条件：`CancelledError`（Runtime shutdown 时 `asyncio.gather` 触发 cancel，supervisor 立即退出，不重启）

**supervisor 重启会发生什么**：
- `_run_once` finally 块保证 `ws_client.stop()` + `channel.stop()` 跑过
- channel 实例状态可能已坏（比如 monoDesk 的 `_server` 线程 join 失败）——channel adapter 的 `start()` 必须 idempotent / 重新建 socket
- monoDesk 现状：`start()` 不重置 `_server`/`_ws_thread`，假设 `stop()` 干净；如果 stop 没干净，二次 start 会因端口冲突再抛 OSError——supervisor 又捕获进入退避（最终能 recover，因为 OSError → 等退避 → 重试期间旧 socket 被 TIME_WAIT 释放）

**适用 vs 不适用**：
- ✅ `Errno 48 Address already in use`（端口残留 / TIME_WAIT）
- ✅ `lark-oapi` 长连接暂时断
- ✅ monoDesk ws server bind 失败
- ❌ 用户 Ctrl+C Runtime（`CancelledError` 不重启，正常退出）
- ❌ Channel 代码 bug（无限循环重启浪费 CPU；当前没设上限，未来可加 `max_restart`）

---

## 验证

### 单元 / 集成测试（pytest）

```python
# tests/test_channels_launch.py
def test_supervisor_restarts_channel_after_crash():
    """_run_once 抛 OSError → supervisor 退避重启 → 下次 _run_once 应被再调用。"""
    # mock channel.start 第一次抛 OSError(48)，第二次挂住
    # supervisor 应至少重启一次

# tests/test_run.py
def test_run_stop_flag_and_pid_file():
    """静态守护：--stop flag + PID_FILE 常量 + DEFAULT_RUNTIME_PORTS 包含 8765/8766/8767。"""

def test_stop_run_is_noop_when_clean():
    """无残留进程 / 端口空闲时 stop_run() 应返回 0，不报错。"""
```

### 手动端到端（macOS / Linux）

**1. 端口残留恢复**
```bash
# 故意启动一次然后 kill -9 模拟崩溃残留
uv run python run.py &
PID=$!
kill -9 $PID
lsof -ti tcp:8766    # → 看到残留 PID（应为 None，因为 kill -9 一起死了；但 monoDesk 子线程可能残留）

# 启动 → 端口被占 → 报错
uv run python run.py    # → OSError: [Errno 48] Address already in use

# 清理
uv run python run.py --stop
# → "port 8766 held by pids=[...]" + SIGTERM + "all clean"

# 再启
uv run python run.py    # → 干净启动
```

**2. Channel crash 不拖死 Runtime**
```bash
# 临时改 monodesk 端口到 8765（已被 RuntimeServer 占）→ 触发启动失败
# config.toml: [[channels]] kind = "monodesk" port = 8765
uv run python run.py
# 观察日志：
#   - terminal channel 仍正常
#   - monodesk 持续报 "channel crashed (OSError: ...); restart in 0.5s (attempt 1)"...
#   - Runtime 不退出
```

**3. Ctrl+C 干净退出**
```bash
uv run python run.py
# Ctrl+C
# → "received signal 2, shutting down" → exit 0
# → .monox/runtime.pid 文件被删
ls .monox/runtime.pid    # → No such file
```

**4. `--stop` exit code**
```bash
# 占用 8766 然后 --stop
nc -l 8766 &
uv run python run.py --stop
# → 杀掉 nc + 任何 runtime 残留 → exit 0
# 端口仍被 nc 占 → exit 1（应该不会发生因为 nc 不在 targets 里——port sweep 会再扫一次）
```

---

## 风险

- **`lsof` 不在镜像 / Windows**：port sweep 退化为空，依赖 PID 文件路径兜底；用户得手动 `netstat` + `taskkill`（文档已知限制）
- **`os._exit` 跳过 asyncio 取消**：signal handler 立刻退出会话，会话 loop 内的 cleanup 不一定跑完；当前 `_remove_pid` 是唯一 cleanup（足够，因为我们关心的是"端口释放 + PID 文件不残留"，RuntimeServer 的优雅关闭不依赖退出码）
- **supervisor 无限重启**：channel 代码 bug（比如 `start()` 永远抛同一种异常）会每 16s 重启一次浪费 CPU；当前没上限。下一轮可加 `MAX_RESTART` + 熔断
- **PID 文件被外部修改 / 损坏**：`--stop` 会把异常 PID 跳过（`int()` try/except），但不会主动清理——下次 `--stop` 还会再尝试一次
- **端口扫描不区分 LISTEN 状态以外的 socket**：`lsof -sTCP:LISTEN` 只看监听者；连接中的 socket（CLOSE_WAIT 等）可能残留但不阻塞重启，可接受
- **`time.sleep` 在 signal handler 里**：signal handler 只调 `os._exit` 不 sleep；`stop_run` 主流程的 sleep 在主线程、signal 不打断

---

## 进度

- [x] `extensions/channels/_runtime.py:run_channel` supervisor + 退避表
- [x] `tests/test_channels_launch.py::test_supervisor_restarts_channel_after_crash`
- [x] `run.py:PID_FILE` + `DEFAULT_RUNTIME_PORTS` + `stop_run()`
- [x] `run.py:run()` 注册 atexit + SIGINT/SIGTERM handler
- [x] `run.py:parse_args()` 加 `--stop` flag + `__main__` 分支
- [x] `tests/test_run.py::test_run_stop_flag_and_pid_file` + `test_stop_run_is_noop_when_clean`
- [x] `spec/requirements/runtime-lifecycle.md`（本文档）
- [ ] supervisor `MAX_RESTART` 熔断（v1.1 候选）
- [ ] 跨平台 port scan 替代方案（Windows / 无 lsof 环境）
- [ ] `os._exit` 路径下 RuntimeServer 优雅关闭（当前靠 OS 回收 socket）