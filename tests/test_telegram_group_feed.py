"""Tests for observe-only Telegram group feeds."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import group_feed  # noqa: E402
import group_feed_listener as listener  # noqa: E402


def _group_update(sender_id: str, text: str = "hello group") -> dict:
    return {
        "update_id": 11,
        "message": {
            "message_id": 22,
            "date": 1755200000,
            "from": {"id": sender_id, "username": "alice"},
            "chat": {"id": -100123456, "title": "agents", "type": "group"},
            "text": text,
        },
    }


class TelegramGroupFeedTests(unittest.TestCase):
    def test_archive_marks_owner_messages_as_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            update = _group_update("7")
            record = listener.archive_message(
                feed,
                update,
                update["message"],
                {},
                owner_id="9",
                max_log_chars=4000,
            )
            self.assertFalse(record["is_owner"])
            self.assertFalse(record["priority"])
            self.assertEqual(record["chat_id"], "-100123456")
            self.assertIn("hello group", record["text_full"])
            lines = feed.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_archive_prioritizes_configured_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            update = _group_update("9")
            record = listener.archive_message(
                feed,
                update,
                update["message"],
                {},
                owner_id="9",
                max_log_chars=4000,
            )
            self.assertTrue(record["is_owner"])
            self.assertTrue(record["priority"])

    def test_process_update_archives_configured_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            log = Path(tmp) / "listener.jsonl"
            args = listener.parse_args(
                [
                    "--feed",
                    str(feed),
                    "--offset-file",
                    str(Path(tmp) / "offset"),
                    "--chat-id",
                    "-100123456",
                    "--owner-id",
                    "9",
                ]
            )
            args.max_log_chars = 4000
            record = listener.process_update(
                _group_update("7"),
                args,
                {},
                log,
            )
            self.assertIsNotNone(record)
            self.assertEqual(record["text_full"], "hello group")

    def test_process_update_skips_other_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            args = listener.parse_args(
                [
                    "--feed",
                    str(feed),
                    "--offset-file",
                    str(Path(tmp) / "offset"),
                    "--chat-id",
                    "-100123456",
                    "--owner-id",
                    "9",
                ]
            )
            update = _group_update("7")
            update["message"]["chat"]["id"] = -100999999
            self.assertIsNone(
                listener.process_update(
                    update,
                    args,
                    {},
                    None,
                )
            )
            self.assertFalse(feed.is_file())

    def test_reader_shows_archived_owner_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            update = _group_update("9")
            listener.archive_message(
                feed,
                update,
                update["message"],
                {},
                owner_id="9",
                max_log_chars=4000,
            )
            records = group_feed.filter_feed(
                group_feed.load_feed(feed),
                owner_only=True,
                since_ts=None,
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["text_full"], "hello group")


class GroupFeedReaderTests(unittest.TestCase):
    def _write_feed(self, path: Path, records: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_filter_feed_owner_only(self) -> None:
        records = [
            {"ts": 100, "is_owner": False, "sender": "bot-a", "text": "x"},
            {"ts": 200, "is_owner": True, "sender": "owner", "text": "y"},
        ]
        kept = group_feed.filter_feed(records, owner_only=True, since_ts=None)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["is_owner"])

    def test_format_entry_marks_owner(self) -> None:
        record = {
            "telegram_date": 1755200000,
            "sender": "@juan",
            "is_owner": True,
            "edited": False,
            "text": "important",
        }
        self.assertIn("[OWNER]", group_feed.format_entry(record))
        self.assertIn("important", group_feed.format_entry(record))

    def test_reader_shows_only_recent_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.jsonl"
            self._write_feed(
                feed,
                [
                    {"ts": 100, "is_owner": False, "sender": "a", "text": "1"},
                    {"ts": 200, "is_owner": False, "sender": "b", "text": "2"},
                    {"ts": 300, "is_owner": False, "sender": "c", "text": "3"},
                ],
            )
            records = group_feed.load_feed(feed)
            self.assertEqual(len(records), 3)
            self.assertEqual(
                [r["text"] for r in records[-2:]], ["2", "3"]
            )


if __name__ == "__main__":
    unittest.main()
