"""multimodal_understand._debug_server_path 单元测试（#53 attachment-local-path）。

`_debug_server_path(url)` 把 `http://host:port/debug/attachments/<fname>` 形式的 URL
反推成本地 `attachments_root/attachments/<fname>`，让 tool 在收到同进程 debug server
URL 时直接 open() 读 bytes 跳过 requests.get（避免 ReadTimeout）。

测试 8 个 case（happy / edge / security）：
- 真存在 → 返回 Path
- 文件不存在 → None
- 非 debug server URL → None
- path traversal（../etc/passwd）→ None（防 escape attachments_root）
- 空 filename → None
- 隐藏文件（.xxx）→ None
- attachments_root=None → 全部 None（back-compat：旧代码无注入不破）
- query string / fragment 仍能匹配
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.loop.tools.multimodal_understand import MultimodalUnderstandTool


class TestDebugServerPath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "attachments").mkdir(parents=True)
        (self.root / "attachments" / "abc123.png").write_bytes(b"fake-png")
        self.tool = MultimodalUnderstandTool(attachments_root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_existing_file_returns_path(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/abc123.png")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name, "abc123.png")
        self.assertTrue(p.is_file())

    def test_missing_file_returns_none(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/missing.png")
        self.assertIsNone(p)

    def test_external_url_returns_none(self) -> None:
        p = self.tool._debug_server_path("https://i.imgur.com/abc.png")
        self.assertIsNone(p)

    def test_path_traversal_returns_none(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/../etc/passwd")
        self.assertIsNone(p)

    def test_empty_filename_returns_none(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/")
        self.assertIsNone(p)

    def test_dotfile_returns_none(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/.secret")
        self.assertIsNone(p)

    def test_none_root_back_compat(self) -> None:
        tool = MultimodalUnderstandTool()
        p = tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/abc123.png")
        self.assertIsNone(p)

    def test_query_and_fragment_stripped(self) -> None:
        p = self.tool._debug_server_path("http://127.0.0.1:8768/debug/attachments/abc123.png?v=1#frag")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name, "abc123.png")


if __name__ == "__main__":
    unittest.main()
