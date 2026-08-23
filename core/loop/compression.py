"""CompressionService：L1/L2/L3 上下文压缩策略。

LoopEngine 在明确时机调用本服务：

- L1 `compress_tool_result`：tool.execute 之后、append tool 消息之前，截断超长结果。
- L2 `should_compress` / `maybe_summarize`：每步 assemble_messages 之前，折叠最早 N 轮。
- L3 `maintain_memory`：L2 产生 summary 后，追加到 Memory.md。
"""
from __future__ import annotations

import json
from typing import Any

from core.loop.context import compress_tool_result as _compress_l1
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.protocol import LLMProxy, MemoryStore, ToolResult


L1_TRUNCATE_LEN = 4000
L2_CHAR_THRESHOLD = 24_000
L2_KEEP_TURNS = 2

L2_SUMMARY_SYSTEM = (
    "You are a conversation compressor. "
    "Summarize the provided conversation turns. "
    "Preserve user goals, decisions, important facts, file paths, "
    "command outputs, errors, and unresolved questions. "
    "Write a concise summary in the same language as the conversation. "
    "Do not omit critical details."
)


class CompressionService:
    def __init__(
        self,
        *,
        budget_tool: ReadToolResultBudgetTool,
        llm: LLMProxy,
        memory: MemoryStore,
        l1_truncate_len: int = L1_TRUNCATE_LEN,
        l2_char_threshold: int = L2_CHAR_THRESHOLD,
        l2_keep_turns: int = L2_KEEP_TURNS,
        summary_options: dict[str, Any] | None = None,
    ) -> None:
        self._budget_tool = budget_tool
        self._llm = llm
        self._memory = memory
        self._l1_truncate_len = l1_truncate_len
        self._l2_char_threshold = l2_char_threshold
        self._l2_keep_turns = l2_keep_turns
        self._summary_options = summary_options

    # ------------------------------------------------------------------
    # L1
    # ------------------------------------------------------------------

    def compress_tool_result(self, result: ToolResult) -> ToolResult:
        return _compress_l1(result, self._budget_tool, self._l1_truncate_len)

    # ------------------------------------------------------------------
    # L2
    # ------------------------------------------------------------------

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        if self._estimate_chars(messages) < self._l2_char_threshold:
            return False
        return self._fold_earliest_turns(messages)[0] > 0

    async def _try_summarize(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """L2 折叠 + summary 的核心实现。返回 (new_messages, summary, folded_count)。

        folded_count 是被折叠的消息数（=fold_end）；没折叠时为 0。
        摘要失败 / 空摘要 → 不折叠，返回原 messages。
        """
        if self._estimate_chars(messages) < self._l2_char_threshold:
            return messages, None, 0

        fold_end, block = self._fold_earliest_turns(messages)
        if fold_end <= 0 or not block:
            return messages, None, 0

        try:
            summary = await self._summarize(block)
        except Exception:
            # 摘要失败不能打爆主 loop，降级为不折叠
            return messages, None, 0

        if not summary:
            return messages, None, 0

        await self.maintain_memory(session_key, summary)
        return messages[fold_end:], summary, fold_end

    async def maybe_summarize(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> list[dict[str, Any]]:
        """L2 折叠（公开 API，back-compat）。返回折叠后的 messages。

        想同时拿到 summary / folded_count（用于 trace），用 `summarize_for_trace`。
        """
        new, _, _ = await self._try_summarize(messages, session_key)
        return new

    async def summarize_for_trace(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """同 maybe_summarize，但额外返回 summary 文本和 folded_count。

        给可观测性（record_compress_span）用；引擎调用一次拿齐三样东西。
        """
        return await self._try_summarize(messages, session_key)

    @staticmethod
    def _estimate_chars(messages: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)

    def _fold_earliest_turns(
        self, messages: list[dict[str, Any]]
    ) -> tuple[int, list[dict[str, Any]]]:
        user_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "user"
        ]
        if len(user_indices) <= self._l2_keep_turns:
            return 0, []

        spans: list[tuple[int, int]] = []
        for k, idx in enumerate(user_indices):
            end = user_indices[k + 1] if k + 1 < len(user_indices) else len(messages)
            spans.append((idx, end))

        n = len(spans) - self._l2_keep_turns
        n = min(n, len(spans) - 1)  # 永远至少保留一个 user turn
        if n <= 0:
            return 0, []

        fold_end = spans[n - 1][1]
        return fold_end, messages[:fold_end]

    async def _summarize(self, block: list[dict[str, Any]]) -> str:
        payload = [
            {"role": "system", "content": L2_SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(block, ensure_ascii=False, indent=2),
            },
        ]

        parts: list[str] = []
        async for chunk in self._llm.stream(
            payload, tools=None, options=self._summary_options
        ):
            if chunk.delta_text:
                parts.append(chunk.delta_text)

        return "".join(parts).strip()

    # ------------------------------------------------------------------
    # L3
    # ------------------------------------------------------------------

    async def maintain_memory(self, session_key: str, summary: str) -> None:
        if not summary:
            return
        await self._memory.append_fact(session_key, summary)
