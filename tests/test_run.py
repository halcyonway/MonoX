"""run.py 装配校验测试。

新 Runtime 模式：run() 装配 SessionManager + RuntimeServer + HealthServer +
按 config 启动 in-process channel adapter。channel 通过 RuntimeWSClient 连
localhost:8765 跟 RuntimeServer 通信。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from run import run


@dataclass
class _Args:
    config: str = ""
    debug: bool = False
    server_host: str | None = None
    server_port: int | None = None
    health_port: int | None = None
    idle_timeout: int | None = None
    no_channels: bool = True  # 默认不开 channel，便于单测


class TestRunValidation:
    async def test_missing_compression_raises(self, tmp_path):
        ws = tmp_path / "ws"
        mem = tmp_path / "mem"
        skills = tmp_path / "skills"
        tmp = tmp_path / "tmp"

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(f"""
[llm]
api_base = "https://example.com/v1"
api_key = "sk-test"
model = "gpt-4"

[sandbox]
workspace_root = "{ws}"
memory_root = "{mem}"
skills_root = "{skills}"
tmp_root = "{tmp}"
""")

        with pytest.raises(RuntimeError, match=r"llm\.compression"):
            await run(str(cfg_path), _Args(config=str(cfg_path)))


def test_run_supports_in_process_channels():
    """run.py 不构建 channel——channel 是独立进程，各自从 config.toml 读配置。"""
    import run as run_mod
    src = open(run_mod.__file__, "r").read()
    # 静态守护：run.py 只跑 Runtime，不拉 channel
    assert "_build_channel" not in src
    assert "asyncio.gather(server.run(), health.run())" in src


def test_server_config_compatible_with_runtime_server():
    """ServerConfig 必须有 RuntimeServer 内部访问的所有字段。

    run.py 传 cfg.server（ServerConfig），RuntimeServer 内部读 `cfg.host/port/max_clients`。
    这个 test 静态守护 ServerConfig 字段对齐，防止 'no attribute max_clients' 这类
    回归——只有手动跑 run.py 才能发现。
    """
    from core.config import ServerConfig
    cfg = ServerConfig(host="127.0.0.1", port=8765)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765
    assert hasattr(cfg, "max_clients"), "ServerConfig must have max_clients"
    assert isinstance(cfg.max_clients, int) and cfg.max_clients > 0


def test_run_stop_flag_and_pid_file():
    """run.py 必须有 --stop 入口 + PID_FILE 常量（防止静默丢失清理能力）。"""
    import run as run_mod
    src = open(run_mod.__file__, "r").read()
    assert '"--stop"' in src, "run.py should expose --stop CLI flag"
    assert "stop_run" in src, "run.py should define stop_run()"
    assert run_mod.PID_FILE.name == "runtime.pid"
    assert 8765 in run_mod.DEFAULT_RUNTIME_PORTS
    assert 8767 in run_mod.DEFAULT_RUNTIME_PORTS


def test_stop_run_is_noop_when_clean():
    """无残留进程 / 端口空闲时 stop_run() 应返回 0，不报错。"""
    from run import stop_run

    # 用一个几乎肯定没占的端口验证 happy path
    rc = stop_run(ports=(50999,))
    assert rc == 0
