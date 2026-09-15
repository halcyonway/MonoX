# extension-cli-server: 原子能力 HTTP 网关 + exec_cli shell 客户端

## 问题

MonoX 的 LLM agent 通过 `bash` 工具执行 shell 命令调外部能力（搜索、文生图、语音识别等）。当前实现是把能力脚本放在 `extensions/skills/<name>/<script>.py`，LLM 通过 SKILL.md 学习命令格式，自己 `python <path>/<script>.py ...` 调用。

三个痛点：

1. **能力膨胀后 SKILL.md 变成命令清单**：i2i 有 list/show/add/edit/rm/apply/raw 七个子命令，asr 有 transcribe/list/show。每个都要 LLM 记住完整 argparse spec，错误率高。
2. **能力脚本之间无隔离**：i2i 跑长 prompt extend（30-60s）、asr 跑 WebSocket 长连接（30min 音频），都在 Runtime 进程的 bash sandbox 里——一个能力卡住影响 agent 当前 turn 的反应。
3. **能力更新要重启 Runtime**：SKILL.md 改了，`skill_sync` 只补缺失；helper script 改了 agent 也得重启才能看到新版本。

## 目标与边界

### 目标

1. **统一入口**：所有 extension 能力通过一个 shell 命令 `exec_cli <subcommand> [args...]` 调用，对 LLM 来说**只看一个命令**
2. **隔离执行**：能力跑在独立 HTTP server 进程（`extensions/cli/inner/server.py`），跟 Runtime 完全解耦——卡死 / 重启 / 升级能力不影响 agent 当前 turn
3. **能力注册通过 import side effect**：drop 一个 `extensions/cli/<name>/` 包 + 一行 `bootstrap_builtins()` import = 新能力上线；不动 core
4. **错误用 envelope 不用异常**：`{"ok": true, "data": ...}` 或 `{"ok": false, "error": {"message": ..., ...}}`，LLM 永远能 `jq` 解析

### 边界

- **不改**：`core/` 任何模块（CLI server 是 extensions/ 自己的事，runtime 不知道它存在）
- **不改**：`Channel` Protocol / `Tool` Protocol（exec_cli 是外部命令，bash 工具直接调，不进 OpenAI function-call schema）
- **不改**：`run.py`（CLI server 由 operator 手动或 supervisor 拉起，**不**由 Runtime spawn——见 §"为什么 Runtime 不 spawn CLI server"）
- **不引入** WebSocket / async——stdlib `http.server.ThreadingHTTPServer` + `urllib.request` 够用
- **不做** 进程内热重载——改 capability 脚本就重启 server（跟 Runtime 改完重启一个量级）

### 为什么 Runtime 不 spawn CLI server

表面上看起来 "Runtime 启动时顺便 spawn CLI server" 跟"feishu channel 由 Runtime 代拉"是同构的——但语义不同：

| 维度 | feishu channel | CLI server |
|---|---|---|
| 谁消费 Runtime 的输出 | 是（channel 把 ws 帧翻译成 IM） | 否（agent 根本不通过 wire 跟 CLI server 通信） |
| 挂了 Runtime 受影响吗 | 是（Runtime 知道 channel 死了要重连） | 否（agent 下次 bash 调用会失败，但 Runtime 不知道也不需要知道） |
| 生命周期归属 | Runtime 同生命周期 | 独立 operator 进程，独立重启 |

CLI server 跟 Runtime 是 **消费者关系**（Runtime 的 bash 工具 spawn 子进程调 exec_cli → exec_cli HTTP 到 server），不是 Runtime 的 child。Runtime 一旦 spawn CLI server，就违反了 §ARCHITECTURE §2 的依赖方向——extensions 应该被 Runtime 消费，不应该反过来拉起"兄弟"。

---

## 设计

### 进程拓扑

```
LLM (bash tool)
  └─ exec_cli mono_search "query" --count 5
       ↓ subprocess (sub-second, exec_cli 是薄客户端)
~/.local/bin/exec_cli  ── HTTP POST ──►  :8769/cli/mono_search
                                       │
                              ┌────────┴────────┐
                              │ cli server      │
                              │ (extensions/cli/│
                              │  inner/server)  │
                              │                 │
                              │ registry route  │
                              │ mono_search →   │
                              │ search handler  │
                              └────────┬────────┘
                                       │
                                       ▼
                              extensions/cli/search/search.py
                              └─ HTTPS POST bocha API
```

CLI server **完全独立进程**：
- 端口 `:8769`（避开 Runtime 8765 / health 8767 / debug 8768）
- 不连 Runtime ws server
- 不被 `run.py --stop` 管（用 PID 文件 / supervisor 自己管，或 operator `kill <pid>`）
- 重启不影响 Runtime 或 agent 当前 turn——下次 bash 调用才感知

### 文件结构

```
extensions/cli/
├── inner/                      # 基础设施，不放业务
│   ├── exec_cli.py            # shebang Python 脚本，urllib POST，安装到 ~/.local/bin/
│   ├── server.py              # ThreadingHTTPServer，POST /cli/<subcommand> 路由
│   ├── registry.py            # subcommand → handler 注册表 + @register 装饰器
│   └── common_util.py         # ok/err envelope，log_call，parse_request_body
│
├── search/                    # 一个能力一个目录（capability = 目录名）
│   ├── __init__.py           # import 副作用：register("mono_search")(main)
│   └── search.py             # main(args: list[str]) -> dict
│
├── i2i/                       # 同上结构 + templates/<name>.md
└── asr/                       # 同上结构
```

**新增能力的步骤**：
1. 创建 `extensions/cli/<name>/` 目录 + `__init__.py`（import 时 register）
2. 实现 `<name>/<module>.py` 的 `main(args) -> dict`
3. 在 `extensions/cli/inner/server.py:bootstrap_builtins()` 加一行 `import extensions.cli.<name>`
4. 创建 `extensions/skills/<name>/SKILL.md`（tier=1，LLM 渐进式披露）

不动 core。

### Wire 协议

#### HTTP envelope

请求：

```
POST /cli/<subcommand> HTTP/1.1
Content-Type: application/json

{"args": ["arg1", "--flag", "value", ...]}
```

成功响应（HTTP 200）：
```json
{"ok": true, "data": {...}}
```

错误响应（HTTP 4xx / 5xx，body 仍 JSON envelope）：
```json
{"ok": false, "error": {"message": "...", "hint": "...", ...}}
```

| HTTP 状态 | 含义 | 例子 |
|---|---|---|
| 200 | handler 正常返回（`ok` 看 body） | list/show/transcribe 成功 |
| 400 | 请求格式错（缺 subcommand / body 不是 list[str]） | `{"args": "oops"}` |
| 404 | unknown subcommand | `mono_unknown` |
| 500 | handler 抛异常（兜底） | upstream API 5xx |

**所有错误都是 envelope**，LLM `jq -r '.error.message'` 永远拿得到人类可读 reason。

#### exec_cli 客户端

```python
# extensions/cli/inner/exec_cli.py —— 装到 ~/.local/bin/exec_cli
import argparse, json, sys, urllib.request, urllib.error, os

DEFAULT_SERVER = os.environ.get("EXEC_CLI_SERVER", "http://127.0.0.1:8769")

def main(argv):
    p = argparse.ArgumentParser(prog="exec_cli")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("subcommand")
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)

    body = json.dumps({"args": a.args}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{a.server.rstrip('/')}/cli/{a.subcommand}",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        sys.stdout.buffer.write(e.read() or b"")
        sys.stderr.write(f"exec_cli: HTTP {e.code} from {a.server}\n")
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: cannot reach CLI server at {a.server}: {e.reason}\n")
        return 2

    sys.stdout.buffer.write(payload)
    if not payload.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    return 0
```

**为什么 stdlib urllib**：core 已依赖 httpx 但 extensions 不该传染 core 依赖；urllib 够用、零依赖。

### handler contract

```python
def main(args: list[str]) -> dict:
    """Registry handler entry point.
    
    Args:
        args: argv tail（不含 subcommand 本身），由 argparse 自己 parse
    
    Returns:
        一个 dict，要么是 envelope `{"ok": bool, "data"|"error": ...}`，
        要么是 raw dict（server 自动包成 `{ok: true, data: <dict>}`）
    
    Raises:
        任何未捕获异常 → server 包成 HTTP 500 + `{"ok": false, "error": {message, ...}}`
        SystemExit（argparse 解析失败）→ HTTP 400 + envelope
    """
```

`__main__.py` 直跑入口可选（standalone debug 用），server 不强制要求。

### handler 边界（什么放 CLI 什么放 core tool）

放进 `extensions/cli/<name>/` 的：
- 调外部 API（bocha / dashscope / volcengine / ...）
- 文件 CRUD + 子命令集合（i2i 模板管理、asr 结果查询）
- 需要长连接 / 长耗时的（asr 30min 音频）
- 需要特定二进制依赖的（ffmpeg / ffmpeg）

**不**放进 CLI、应该放 `core/loop/tools/` 的：
- agent runtime 强依赖的（bash / skill_load / fork_task / poll_task / cancel_task / wait_io / read_tool_result_budget / multimodal_understand）
- 任何不调外部 API 的纯本地操作

判断标准：**这个能力如果挂了，agent 还能继续干活吗？** 能 → CLI；不能 → core。

### install 行为

`scripts/install.sh` 把 `extensions/cli/inner/exec_cli.py` 拷贝（不是链接）到 `~/.local/bin/exec_cli`：
- 拷贝而不是 link：避免 server 重命名 / 移动 source 导致 link 失效
- `chmod +x` + 保留 shebang
- 提示用户 `$HOME/.local/bin` 不在 PATH 时给警告
- **不**启动 server——server 生命周期归 operator 自己管（手动 / supervisor / launchd）

不把 server 拉起放 install 里：同 Runtime 不 spawn CLI server 的理由——install 是 stateless 静态准备，不假设 server 跑（用户可能在 container 里跑、可能在 CI 里跑）。

---

## 验证

### 单元 / 集成测试（pytest）

```python
# tests/test_cli_server.py
def test_healthz_lists_subcommands():
    """GET /healthz → {"ok": true, "data": {"subcommands": [...]}}"""
    server = CliServer(port=0).run_in_thread()
    port = server.port
    try:
        with urllib.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            body = json.loads(r.read())
        assert body["ok"] is True
        assert "mono_search" in body["data"]["subcommands"]
    finally:
        server.shutdown()

def test_unknown_subcommand_returns_404_envelope():
    """POST /cli/mono_unknown → 404 + {ok:false, error:{message, available:[...]}}"""
    ...

def test_bad_args_returns_400_envelope():
    """POST /cli/mono_search body={"args": "not-a-list"} → 400 + envelope"""
    ...

def test_handler_exception_returns_500_envelope():
    """handler 抛 Exception → server 不崩，返 500 + envelope；server 仍可服务后续请求"""
    ...

def test_exec_cli_install_path_exists():
    """static: ~/.local/bin/exec_cli 存在（CI 跑过 install.sh 后检查）"""
    install_bin = Path.home() / ".local" / "bin" / "exec_cli"
    assert install_bin.exists() and os.access(install_bin, os.X_OK)
```

### 手动端到端

**1. Server 起停**

```bash
# 启动
uv run python -m extensions.cli.inner.server &
SERVER_PID=$!
sleep 1

# 健康检查
curl -s http://127.0.0.1:8769/healthz | jq .
# → {"ok":true, "data":{"subcommands":["mono_asr","mono_i2i","mono_search"]}}

# 调用
exec_cli mono_i2i list | jq '.data.templates | length'
# → 4

# 停止
kill $SERVER_PID
# 端口 8769 应释放
lsof -ti tcp:8769 -sTCP:LISTEN
# → (空)
```

**2. Handler 隔离**

```bash
# 故意让 mono_search 慢（mock 一个 sleep）→ Runtime 的 bash tool 不会卡
# （这是设计目标，实际不需测，架构上 bash sub-second 返回，handler server-side 长耗时）
```

**3. Error envelope**

```bash
unset BOCHA_API_KEY
exec_cli mono_search "test"
# → {"ok":false, "error":{"message":"BOCHA_API_KEY not set","hint":"..."}}
# exit 0（envelope 即结果，exec_cli 不翻译）
```

**4. e2e 不破**

```bash
uv run python tests/test_e2e.py
# → 6/6 passing（CLI server 跟 Runtime 完全独立，e2e 跑时不启动 server 也得过）
```

---

## 风险

- **CLI server 单点**：没有 supervisor / failover；挂了 = 所有 mono_* 调用失败直到 operator 重启
  - 缓解：未来加 launchd / systemd unit；当前用 nohup + 手动
- **exec_cli 跟 server 版本不匹配**：升级 server 后老的 exec_cli 可能调不存在的 subcommand
  - 缓解：exec_cli 走 registry 动态枚举，`available` 字段告诉 agent 当前 server 支持什么
- **`extensions/cli/<name>/` 写错不报错**：bootstrap_builtins 加错 import → server 起不来 → 用户得看 stderr
  - 缓解：CI 加 import smoke test（已由 test_cli_server 覆盖）
- **`httpx` 诱惑**：有人想"反正 core 已经依赖了，给 cli 也用"——会扩大 extensions 依赖面
  - 缓解：本 spec 明确"urllib 够用"；code review 时拦
- **envelope shape 不一致**：有的 handler 返回 envelope，有的返回 raw dict（server 自动包）——LLM 解析时要兼容两种
  - 缓解：本 spec 明确"handler 优先返回 envelope"；server 兼容 raw dict 是 fallback
- **LLM 不会调 exec_cli**：如果 SKILL.md 没教，agent 不知道有这个命令
  - 缓解：tier=1 skill 必须在 system prompt 里写出"`exec_cli mono_<name>`"调用示例

---

## 进度

- [x] `extensions/cli/inner/{exec_cli.py, server.py, registry.py, common_util.py}` 骨架
- [x] `extensions/cli/{search,i2i,asr}/` 三个能力迁移
- [x] `extensions/skills/{search,i2i,asr}/SKILL.md` 改为 `exec_cli mono_*` 调用形式
- [x] `scripts/install.sh` 装 exec_cli 到 `~/.local/bin/`
- [ ] `tests/test_cli_server.py` 自动化（v1.1）
- [ ] launchd / systemd unit 模板（v1.1）
- [ ] streaming 响应支持（v1.1，Bocha 支持流式）
- [ ] `extensions/cli/<name>/` 新增能力模板（cookiecutter）（v1.2）

---

## 相关 spec

- `ARCHITECTURE.md §2` 架构总览 + 依赖方向（CLI server 是 extensions 层的独立进程）
- `ARCHITECTURE.md §9` extensions 细化（cli/ 作为新子模块加进列表）
- `requirements/runtime-lifecycle.md` PID / --stop 模式（CLI server 借鉴同一思路但不共享代码）
- `requirements/async-task.md` 异步任务（fork_task / poll_task）—— LLM 调长耗时能力的另一条路，跟 CLI server 正交
