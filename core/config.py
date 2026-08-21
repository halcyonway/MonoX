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
    skills_root: str = "/var/agent/skills"
    tmp_root: str = "/var/agent/tmp"


@dataclass(frozen=True)
class Config:
    session_key: str = "default"
    llm: LLMConfig = field(default_factory=LLMConfig)
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    multi_channel: MultiChannelConfig = field(default_factory=MultiChannelConfig)

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

        return cls(
            session_key=data.get("session_key", "default"),
            llm=LLMConfig(
                api_base=llm_raw.get("api_base", ""),
                api_key=llm_raw.get("api_key", ""),
                model=llm_raw.get("model", "gpt-4"),
                timeout=llm_raw.get("timeout", 60),
                options=llm_raw.get("options", {}),
                extra_params=llm_raw.get("extra_params", {}),
            ),
            channel=single_ch,
            sandbox=SandboxConfig(**data.get("sandbox", {})),
            multi_channel=multi_ch,
        )


def session_paths(cfg: Config) -> dict[str, Path]:
    """所有 sandbox 路径。session 维度的自动按 session_key 隔离。"""
    base = Path(cfg.sandbox.workspace_root)
    return {
        # session 隔离
        "workspace": base / cfg.session_key,
        "memory": Path(cfg.sandbox.memory_root) / cfg.session_key,
        "checkpoint": Path(cfg.sandbox.memory_root) / cfg.session_key / "checkpoint.jsonl",
        "memory_index": Path(cfg.sandbox.memory_root) / cfg.session_key / "Memory.md",
        "memory_notes": Path(cfg.sandbox.memory_root) / cfg.session_key / "notes",
        # 共享，不按 session 隔离
        "skills_root": Path(cfg.sandbox.skills_root),
        "tmp_root": Path(cfg.sandbox.tmp_root),
    }