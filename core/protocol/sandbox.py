"""Sandbox 执行接口。Loop 不感知具体执行后端，只看到 SandboxResult。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int


@runtime_checkable
class SandboxRunner(Protocol):
    """执行一条 shell 命令，返回结构化结果。

    实现者：core/sandbox/bash_runner.py（v0: subprocess）。
    未来可换 docker exec / ssh 等后端，协议不变。
    """

    async def run(
        self,
        cmd: str,
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...
