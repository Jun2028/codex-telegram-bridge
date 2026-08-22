"""Tests for the ChatGPT-app image generation driver (fail-fast parts)."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "app_image_gen.py"
sys.path.insert(0, str(ROOT / "scripts"))

import app_image_gen  # noqa: E402


class AppImageGenUnitTests(unittest.TestCase):
    def test_pick_page_prefers_main_chat_surface(self) -> None:
        targets = [
            {"type": "page", "url": "app://-/index.html?initialRoute=%2Favatar-overlay", "id": "a"},
            {"type": "page", "url": "app://-/index.html", "id": "b"},
            {"type": "webview", "url": "https://chatgpt.com/", "id": "c"},
        ]
        self.assertEqual(app_image_gen.pick_page(targets)["id"], "b")

    def test_pick_page_fails_fast_without_pages(self) -> None:
        with self.assertRaises(app_image_gen.AppImageError) as ctx:
            app_image_gen.pick_page([])
        self.assertEqual(ctx.exception.code, "app_page_not_found")

    def test_frame_encoding_lengths(self) -> None:
        for size in (0, 125, 126, 65535, 65536):
            frame = app_image_gen.WS._encode_frame(b"x" * size)
            self.assertEqual(frame[0] & 0x0F, 1)  # text opcode
            self.assertEqual(frame[0] & 0x80, 0x80)  # FIN

    def test_closed_debug_port_fails_fast(self) -> None:
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--cdp", "http://127.0.0.1:1",
             "--timeout", "2", "a test prompt"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("app_not_running", proc.stderr)


if __name__ == "__main__":
    unittest.main()
