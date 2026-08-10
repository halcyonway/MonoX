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


@dataclass(frozen=True)
class ChannelConfig:
    """channel.options 由各 adapter 自行解析，core 不耦合具体 channel schema。"""
    kind: str = "terminal"
    options: dict[str, Any] = field(default_factory=dict)


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

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "rb") as f:
            return cls.from_dict(_expand_env(tomllib.load(f)))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        return cls(
            session_key=data.get("session_key", "default"),
            llm=LLMConfig(**data.get("llm", {})),
            channel=ChannelConfig(
                kind=data.get("channel", {}).get("kind", "terminal"),
                options=data.get("channel", {}).get("options", {}),
            ),
            sandbox=SandboxConfig(**data.get("sandbox", {})),
        )


def session_paths(cfg: Config) -> dict[str, Path]:
    """基于 session_key 的所有隔离路径。"""
    base = Path(cfg.sandbox.workspace_root)
    return {
        "workspace": base / cfg.session_key,
        "memory": Path(cfg.sandbox.memory_root) / cfg.session_key,
        "checkpoint": Path(cfg.sandbox.memory_root) / cfg.session_key / "checkpoint.jsonl",
        "memory_index": Path(cfg.sandbox.memory_root) / cfg.session_key / "Memory.md",
        "memory_notes": Path(cfg.sandbox.memory_root) / cfg.session_key / "notes",
    }