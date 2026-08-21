"""run.py 装配校验测试。"""
from __future__ import annotations

import pytest

from run import run


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
            await run(str(cfg_path), False, None)
