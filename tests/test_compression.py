"""CompressionService L1/L2/L3 单元测试。"""
from __future__ import annotations

from pathlib import Path

from core.loop.compression import CompressionService
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.memory import FsMemoryStore
from core.protocol import LlmChunk, LLMProxy, ToolResult


class _ScriptedLLM(LLMProxy):
    """固定返回一段摘要；可注入异常。"""

    def __init__(self, text: str = "summary text", fail: bool = False) -> None:
        self._text = text
        self._fail = fail

    async def stream(self, messages, tools=None, options=None):
        if self._fail:
            raise RuntimeError("summary failed")
        yield LlmChunk(delta_text=self._text, finish_reason="stop")


def _make_service(tmp_path: Path, llm: LLMProxy) -> tuple[CompressionService, ReadToolResultBudgetTool, FsMemoryStore]:
    budget = ReadToolResultBudgetTool()
    memory = FsMemoryStore(tmp_path / "mem")
    return CompressionService(budget_tool=budget, llm=llm, memory=memory), budget, memory


def _long_result(stdout_len: int = 5000, stderr_len: int = 0) -> ToolResult:
    return ToolResult(
        call_id="c1",
        status="ok",
        stdout="a" * stdout_len,
        stderr="b" * stderr_len,
        exit_code=0,
    )


class TestL1:
    async def test_short_result_unchanged(self, tmp_path: Path):
        svc, _, _ = _make_service(tmp_path, _ScriptedLLM())
        result = ToolResult(call_id="c1", status="ok", stdout="short", stderr="", exit_code=0)
        out = svc.compress_tool_result(result)
        assert out is result
        assert out.truncated is False
        assert out.budget_id is None

    async def test_long_stdout_truncated_and_budgeted(self, tmp_path: Path):
        svc, budget, _ = _make_service(tmp_path, _ScriptedLLM())
        out = svc.compress_tool_result(_long_result(stdout_len=5000))
        assert out.truncated is True
        assert out.budget_id is not None
        assert len(out.stdout) < 5000
        assert "read_tool_result_budget" in out.stdout

        # budget 往返能读回完整结果
        full = await budget.execute("c2", {"budget_id": out.budget_id})
        assert full.stdout == "a" * 5000

    async def test_long_stderr_truncated(self, tmp_path: Path):
        svc, _, _ = _make_service(tmp_path, _ScriptedLLM())
        out = svc.compress_tool_result(_long_result(stdout_len=0, stderr_len=5000))
        assert out.truncated is True
        assert len(out.stderr) < 5000
        assert out.stdout == ""


class TestL2:
    async def test_should_compress_below_threshold_false(self, tmp_path: Path):
        svc, _, _ = _make_service(tmp_path, _ScriptedLLM())
        messages = [{"role": "user", "content": "hello"}]
        assert svc.should_compress(messages) is False

    async def test_should_compress_above_threshold_true(self, tmp_path: Path):
        svc, _, _ = _make_service(tmp_path, _ScriptedLLM())
        messages = [
            {"role": "user", "content": "x" * 10_000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "y" * 10_000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "z" * 10_000},
        ]
        assert svc.should_compress(messages) is True

    async def test_maybe_summarize_folds_earliest_turn(self, tmp_path: Path):
        svc, _, memory = _make_service(tmp_path, _ScriptedLLM(text="folded summary"))
        messages = [
            {"role": "user", "content": "first turn " + "x" * 10_000},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second turn " + "y" * 10_000},
            {"role": "assistant", "content": "second reply"},
            {"role": "user", "content": "third turn " + "z" * 10_000},
        ]
        out = await svc.maybe_summarize(messages, "default")
        # 最早一轮被折叠，保留最近 2 个 user turn
        assert any(m.get("role") == "user" and m["content"].startswith("first turn") for m in messages) is True
        assert any(m.get("role") == "user" and m["content"].startswith("first turn") for m in out) is False
        assert any(m.get("role") == "user" and m["content"].startswith("second turn") for m in out) is True
        assert any(m.get("role") == "user" and m["content"].startswith("third turn") for m in out) is True

        # summary 落到 Memory.md
        index = await memory.read_index("default")
        assert "folded summary" in index

    async def test_maybe_summarize_failure_returns_unchanged(self, tmp_path: Path):
        svc, _, _ = _make_service(tmp_path, _ScriptedLLM(fail=True))
        messages = [
            {"role": "user", "content": "first turn " + "x" * 10_000},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second turn " + "y" * 10_000},
            {"role": "assistant", "content": "second reply"},
            {"role": "user", "content": "third turn " + "z" * 10_000},
        ]
        out = await svc.maybe_summarize(messages, "default")
        assert out is messages


class TestL3:
    async def test_maintain_memory_appends_section(self, tmp_path: Path):
        svc, _, memory = _make_service(tmp_path, _ScriptedLLM())
        await svc.maintain_memory("default", "first summary")
        await svc.maintain_memory("default", "second summary")

        index = await memory.read_index("default")
        assert "## Conversation Summaries" in index
        assert "- first summary" in index
        assert "- second summary" in index
