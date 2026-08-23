"""统一配置加载。单一 config.toml 入口。

字段都不可变，缺省值保证最小可启动。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env(data: Any) -> Any:
    """递归展开 string 里的 ${VAR} → os.environ[VAR]。未设置返回空串。"""
    if isinstance(data, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), data)
    if isinstance(data, dict):
        return {k: _expand_env(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_expand_env(v) for v in data]
    return data


@dataclass(frozen=True)
class LLMConfig:
    api_base: str = ""
    api_key: str = ""
    model: str = "gpt-4"
    timeout: int = 60
    options: dict[str, Any] = field(default_factory=dict)  # 默认 sampling 参数
    extra_params: dict[str, Any] = field(default_factory=dict)  # 模型特定参数（透传 API）
    custom: dict[str, Any] = field(default_factory=dict)  # 自定义参数，透传 request body


@dataclass(frozen=True)
class ChannelConfig:
    """channel.options 由各 adapter 自行解析，core 不耦合具体 channel schema。
    channel_raw 保留原始 channel dict（含 [channel.feishu] 等子表）。
    """
    kind: str = "terminal"
    options: dict[str, Any] = field(default_factory=dict)
    channel_raw: dict[str, Any] = field(default_factory=dict)  # 包含 feishu 等子表


@dataclass(frozen=True)
class MultiChannelConfig:
    """多 channel 配置，支持 [[channels]] 列表格式。"""
    channels: list[ChannelConfig] = field(default_factory=list)
    default_channel: str = "terminal"  # 无 <send> 标签时默认发到这个 channel


@dataclass(frozen=True)
class SandboxConfig:
    workspace_root: str = "/var/agent/workspace"
    memory_root: str = "/var/agent/memory"
    state_root: str = "/var/agent/state"
    traces_root: str = "/var/agent/traces"
    skills_root: str = "/var/agent/skills"
    tmp_root: str = "/var/agent/tmp"
    skills_max_l1: int = 50  # L1 skill 注入 system prompt 的上限


@dataclass(frozen=True)
class ServerConfig:
    """Runtime ws server 配置。

    Runtime 进程启动时绑这里指定的 host:port，给 channel ws client 连。
    """
    host: str = "127.0.0.1"
    port: int = 8765
    max_clients: int = 16


@dataclass(frozen=True)
class Config:
    session_key: str = "default"
    llm: LLMConfig = field(default_factory=LLMConfig)
    compression_llm: LLMConfig | None = None  # 独立压缩模型；run 启动校验必填
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    multi_channel: MultiChannelConfig = field(default_factory=MultiChannelConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "rb") as f:
            return cls.from_dict(_expand_env(tomllib.load(f)))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        llm_raw = data.get("llm", {})

        # 解析 [[channels]]（新格式）
        channel_list = data.get("channels", [])
        multi_ch = MultiChannelConfig(
            channels=[
                ChannelConfig(
                    kind=c.get("kind", "terminal"),
                    options=c.get("options", {}),
                    channel_raw=c,
                )
                for c in channel_list
            ],
            default_channel=data.get("channels_default", "terminal"),
        )

        # 解析 [channel]（旧格式，兼容单 channel）
        ch_data = data.get("channel", {})
        single_ch = ChannelConfig(
            kind=ch_data.get("kind", "terminal"),
            options=ch_data.get("options", {}),
            channel_raw=ch_data,
        )

        llm = LLMConfig(
            api_base=llm_raw.get("api_base", ""),
            api_key=llm_raw.get("api_key", ""),
            model=llm_raw.get("model", "gpt-4"),
            timeout=llm_raw.get("timeout", 60),
            options=llm_raw.get("options", {}),
            extra_params=llm_raw.get("extra_params", {}),
            custom=llm_raw.get("custom", {}),
        )

        # 独立压缩模型：未配置的字段回退到主 llm
        comp_raw = llm_raw.get("compression")
        compression_llm = None
        if comp_raw:
            compression_llm = LLMConfig(
                api_base=comp_raw.get("api_base", llm.api_base),
                api_key=comp_raw.get("api_key", llm.api_key),
                model=comp_raw.get("model", llm.model),
                timeout=comp_raw.get("timeout", llm.timeout),
                options=comp_raw.get("options", llm.options),
                extra_params=comp_raw.get("extra_params", llm.extra_params),
                custom=comp_raw.get("custom", llm.custom),
            )

        return cls(
            session_key=data.get("session_key", "default"),
            llm=llm,
            compression_llm=compression_llm,
            channel=single_ch,
            sandbox=SandboxConfig(**data.get("sandbox", {})),
            multi_channel=multi_ch,
            server=ServerConfig(**data.get("server", {})),
        )


def session_paths(cfg: Config) -> dict[str, Path]:
    """所有 sandbox 路径。

    四个 root 互不嵌套：

    - `workspace/<sk>/`：LLM 视角的 shell cwd（可改、可删）。
    - `state/<sk>/`：Runtime 视角的 internal state（LLM 不应该看到，更不能 rm）。
      - checkpoint.jsonl 在这里。
    - `traces/<sk>/`：开发者视角的可观测 trace，独立 root。
    - `memory/`：用户视角的长期记忆，**跨会话全局**。
    """
    ws_root = Path(cfg.sandbox.workspace_root)
    state_root = Path(cfg.sandbox.state_root)
    mem_root = Path(cfg.sandbox.memory_root)
    traces_root = Path(cfg.sandbox.traces_root)
    return {
        # session 隔离：LLM shell cwd
        "workspace": ws_root / cfg.session_key,
        # session 隔离：Runtime internal state（LLM 不应见）
        "state": state_root,
        "state_dir": state_root / cfg.session_key,
        "checkpoint": state_root / cfg.session_key / "checkpoint.jsonl",
        # session 隔离：可观测 trace
        "traces": traces_root / cfg.session_key / "traces.jsonl",
        "traces_root": traces_root,
        # 跨会话全局：用户长期记忆
        "memory": mem_root,
        "memory_index": mem_root / "Memory.md",
        "memory_notes": mem_root / "notes",
        # 共享
        "skills_root": Path(cfg.sandbox.skills_root),
        "tmp_root": Path(cfg.sandbox.tmp_root),
    }