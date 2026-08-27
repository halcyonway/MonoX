"""LlmProxy 请求体合并测试。"""
from __future__ import annotations

from core.config import LLMConfig
from core.llm_proxy import LlmProxy


class _FakeResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield "data: [DONE]"


class _FakeClient:
    def __init__(self) -> None:
        self.captured = None

    def stream(self, method, url, *, json=None):
        self.captured = json
        return _FakeResponse()


class TestPayloadMerge:
    async def test_merge_order_includes_custom(self):
        """验证 stream() payload 合并顺序（需要 mock httpx）。"""
        import pytest
        pytest.skip("TODO: 用 unittest.mock.patch 重写 httpx.AsyncClient")
