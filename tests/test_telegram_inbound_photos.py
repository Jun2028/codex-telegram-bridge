"""Tests for Telegram photo handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox as inbox  # noqa: E402


class TelegramInboundPhotoTests(unittest.TestCase):
    def test_largest_telegram_photo_prefers_biggest_file(self) -> None:
        photo = inbox.largest_telegram_photo(
            {
                "photo": [
                    {"file_id": "a", "width": 100, "file_size": 1000},
                    {"file_id": "b", "width": 320, "file_size": 99999},
                    {"file_id": "c", "width": 90, "file_size": 500},
                ]
            }
        )
        self.assertEqual(photo["file_id"], "b")

    def test_largest_telegram_photo_missing(self) -> None:
        self.assertIsNone(inbox.largest_telegram_photo({}))
        self.assertIsNone(inbox.largest_telegram_photo({"photo": []}))

    def test_download_photo_requires_file_id(self) -> None:
        with self.assertRaises(ValueError):
            inbox.download_telegram_photo(
                "token", {}, Path("/tmp"), 1, max_bytes=100
            )

    def test_rejects_oversized_photo_before_network(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            inbox.download_telegram_photo(
                "token",
                {"file_id": "x", "file_size": 11 * 1024 * 1024},
                Path("/tmp"),
                1,
                max_bytes=10 * 1024 * 1024,
            )
        self.assertIn("inbound limit", str(ctx.exception))

    def test_validate_inbound_photo_accepts_jpeg_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.jpg"
            path.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg body")
            inbox.validate_inbound_photo(path, ".jpg")
            bad = Path(tmp) / "bad.jpg"
            bad.write_bytes(b"definitely not an image")
            with self.assertRaises(ValueError):
                inbox.validate_inbound_photo(bad, ".jpg")

    def test_format_inbound_photo_text_includes_caption_and_path(self) -> None:
        received = {
            "path": "/private/inbound_photos/photo_7.jpg",
            "width": 640,
            "height": 480,
            "size_bytes": 12345,
            "sha256": "a" * 64,
        }
        text = inbox.format_inbound_photo_text(received, "look at this")
        self.assertIn(received["path"], text)
        self.assertIn("look at this", text)
        self.assertIn("user-provided data", text)


if __name__ == "__main__":
    unittest.main()
