"""BashRunner: subprocess 后端，实现 SandboxRunner Protocol。

未来可换 docker exec / ssh 实现，协议不变。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from core.protocol import SandboxResult, SandboxRunner


class BashRunner(SandboxRunner):
    async def run(
        self,
        cmd: str,
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(stdout="", stderr=f"timeout after {timeout}s", exit_code=124)

        return SandboxResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else 0,
        )