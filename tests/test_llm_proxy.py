"""OpenAIStreamProxy 请求体合并测试。"""
from __future__ import annotations

from core.config import LLMConfig
from core.llm_proxy import OpenAIStreamProxy


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
        cfg = LLMConfig(
            api_base="https://example.com/v1",
            api_key="sk-test",
            model="main-model",
            options={"temperature": 0.7, "max_tokens": 100},
            extra_params={"reasoning_split": True},
            custom={"top_k": 5},
        )
        proxy = OpenAIStreamProxy(cfg)
        client = _FakeClient()
        proxy._client = client

        chunks = []
        async for chunk in proxy.stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
            options={"temperature": 0.1},
        ):
            chunks.append(chunk)

        payload = client.captured
        assert payload is not None
        assert payload["model"] == "main-model"
        assert payload["stream"] is True
        assert payload["tools"] == [{"type": "function"}]
        assert payload["reasoning_split"] is True
        assert payload["top_k"] == 5
        assert payload["max_tokens"] == 100
        # 调用方 options 最后覆盖
        assert payload["temperature"] == 0.1
        assert chunks == []
