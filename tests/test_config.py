"""Config.from_dict 测试，重点覆盖可选独立压缩模型。"""
from __future__ import annotations

from core.config import Config


class TestCompressionLlm:
    def test_omitted_returns_none(self):
        cfg = Config.from_dict({
            "llm": {
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-main",
                "model": "gpt-4",
            },
        })
        assert cfg.compression_llm is None

    def test_partial_config_falls_back_to_main_llm(self):
        cfg = Config.from_dict({
            "llm": {
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-main",
                "model": "gpt-4",
                "custom": {"top_k": 10},
                "compression": {
                    "model": "gpt-4o-mini",
                },
            },
        })
        assert cfg.compression_llm is not None
        assert cfg.compression_llm.model == "gpt-4o-mini"
        assert cfg.compression_llm.api_base == "https://api.openai.com/v1"
        assert cfg.compression_llm.api_key == "sk-main"
        assert cfg.compression_llm.timeout == 60
        assert cfg.compression_llm.custom == {"top_k": 10}

    def test_compression_options_override(self):
        cfg = Config.from_dict({
            "llm": {
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-main",
                "model": "gpt-4",
                "options": {"temperature": 0.7},
                "custom": {"top_k": 10},
                "compression": {
                    "model": "gpt-4o-mini",
                    "options": {"temperature": 0.2},
                    "custom": {"top_k": 5},
                },
            },
        })
        assert cfg.compression_llm.options == {"temperature": 0.2}
        assert cfg.llm.options == {"temperature": 0.7}
        assert cfg.compression_llm.custom == {"top_k": 5}
        assert cfg.llm.custom == {"top_k": 10}
