"""CompressionService L1/L2 单元测试。

注：L3 已删除——Memory.md 不再由压缩自动落盘。
"""
from __future__ import annotations

from pathlib import Path

from core.loop.compression import CompressionService
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
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


def _make_service(tmp_path: Path, llm: LLMProxy) -> tuple[CompressionService, ReadToolResultBudgetTool]:
    budget = ReadToolResultBudgetTool()
    return CompressionService(budget_tool=budget, llm=llm), budget


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
        svc, _ = _make_service(tmp_path, _ScriptedLLM())
        result = ToolResult(call_id="c1", status="ok", stdout="short", stderr="", exit_code=0)
        out = svc.compress_tool_result(result)
        assert out is result
        assert out.truncated is False
        assert out.budget_id is None

    async def test_long_stdout_truncated_and_budgeted(self, tmp_path: Path):
        svc, budget = _make_service(tmp_path, _ScriptedLLM())
        out = svc.compress_tool_result(_long_result(stdout_len=5000))
        assert out.truncated is True
        assert out.budget_id is not None
        assert len(out.stdout) < 5000
        assert "read_tool_result_budget" in out.stdout

        # budget 往返能读回完整结果
        full = await budget.execute("c2", {"budget_id": out.budget_id})
        assert full.stdout == "a" * 5000

    async def test_long_stderr_truncated(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM())
        out = svc.compress_tool_result(_long_result(stdout_len=0, stderr_len=5000))
        assert out.truncated is True
        assert len(out.stderr) < 5000
        assert out.stdout == ""


class TestL2:
    async def test_should_compress_below_threshold_false(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM())
        messages = [{"role": "user", "content": "hello"}]
        assert svc.should_compress(messages) is False

    async def test_should_compress_above_threshold_true(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM())
        messages = [
            {"role": "user", "content": "x" * 10_000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "y" * 10_000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "z" * 10_000},
        ]
        assert svc.should_compress(messages) is True

    async def test_maybe_summarize_folds_earliest_turn(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM(text="folded summary"))
        messages = [
            {"role": "user", "content": "first turn " + "x" * 10_000},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second turn " + "y" * 10_000},
            {"role": "assistant", "content": "second reply"},
            {"role": "user", "content": "third turn " + "z" * 10_000},
        ]
        out = await svc.maybe_summarize(messages)
        # 最早一轮被折叠，保留最近 2 个 user turn
        assert any(m.get("role") == "user" and m["content"].startswith("first turn") for m in messages) is True
        assert any(m.get("role") == "user" and m["content"].startswith("first turn") for m in out) is False
        assert any(m.get("role") == "user" and m["content"].startswith("second turn") for m in out) is True
        assert any(m.get("role") == "user" and m["content"].startswith("third turn") for m in out) is True

    async def test_maybe_summarize_failure_returns_unchanged(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM(fail=True))
        messages = [
            {"role": "user", "content": "first turn " + "x" * 10_000},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second turn " + "y" * 10_000},
            {"role": "assistant", "content": "second reply"},
            {"role": "user", "content": "third turn " + "z" * 10_000},
        ]
        out = await svc.maybe_summarize(messages)
        assert out is messages

    async def test_summarize_for_trace_returns_summary_and_count(self, tmp_path: Path):
        svc, _ = _make_service(tmp_path, _ScriptedLLM(text="folded summary"))
        messages = [
            {"role": "user", "content": "first turn " + "x" * 10_000},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second turn " + "y" * 10_000},
            {"role": "assistant", "content": "second reply"},
            {"role": "user", "content": "third turn " + "z" * 10_000},
        ]
        out, summary, folded = await svc.summarize_for_trace(messages)
        assert summary == "folded summary"
        assert folded > 0
        # L3 已删除：summary 不再落 Memory.md，无需断言副作用
        assert any(m.get("role") == "user" and m["content"].startswith("third turn") for m in out) is True
