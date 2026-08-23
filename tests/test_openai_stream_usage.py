"""OpenAIStreamProxy._normalize_usage：把 OpenAI stream final chunk 的嵌套 usage
拍平，提取 prompt_tokens_details.cached_tokens 到 usage 顶层。

为什么：reasoning span 直接拿 usage dict 渲染，UI 要看 cached_tokens，所以把它
提到顶层最简单。
"""
from __future__ import annotations

from core.llm_proxy.openai_stream import _normalize_usage


def test_normalize_usage_extracts_cached_tokens():
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 800},
    }
    out = _normalize_usage(raw)
    assert out is not None
    assert out["prompt_tokens"] == 1000
    assert out["completion_tokens"] == 50
    assert out["cached_tokens"] == 800
    # nested details 不应该泄漏
    assert "prompt_tokens_details" not in out


def test_normalize_usage_missing_cached_tokens():
    raw = {"prompt_tokens": 1000, "completion_tokens": 50}
    out = _normalize_usage(raw)
    assert out is not None
    assert out["prompt_tokens"] == 1000
    assert "cached_tokens" not in out


def test_normalize_usage_empty_details():
    raw = {
        "prompt_tokens": 1000,
        "prompt_tokens_details": {},  # 字段存在但 cached_tokens 没有
    }
    out = _normalize_usage(raw)
    assert out is not None
    assert "cached_tokens" not in out


def test_normalize_usage_none_returns_none():
    assert _normalize_usage(None) is None


def test_normalize_usage_does_not_mutate_input():
    raw = {
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 800},
    }
    out = _normalize_usage(raw)
    # 原始 dict 不应该被改
    assert "prompt_tokens_details" in raw
    assert "cached_tokens" not in raw
    assert "cached_tokens" in out