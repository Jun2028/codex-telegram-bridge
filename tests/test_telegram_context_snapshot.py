#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telegram_inbox  # noqa: E402


def token_count_event(input_tokens: int, window: int) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": input_tokens},
                    "model_context_window": window,
                },
            },
        },
        separators=(",", ":"),
    )


class TelegramContextSnapshotTests(unittest.TestCase):
    def test_reads_last_context_usage_and_compactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(
                "\n".join(
                    [
                        token_count_event(100_000, 996_147),
                        '{"type":"compacted","payload":{"window_id":"w1"}}',
                        token_count_event(400_000, 996_147),
                        '{"type":"compacted","payload":{"window_id":"w2"}}',
                        token_count_event(780_000, 996_147),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            snapshot = telegram_inbox.codex_session_context_snapshot(path)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["context_tokens"], 780_000)
            self.assertEqual(snapshot["context_window"], 996_147)
            self.assertEqual(snapshot["compactions"], 2)

    def test_counts_context_compacted_event_messages_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(
                "\n".join(
                    [
                        '{"type":"event_msg","payload":{"type":"context_compacted"}}',
                        '{"type":"event_msg","payload":{"type":"context_compacted"}}',
                        token_count_event(200_000, 996_147),
                    ]
                ),
                encoding="utf-8",
            )
            snapshot = telegram_inbox.codex_session_context_snapshot(path)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["context_tokens"], 200_000)
            self.assertEqual(snapshot["compactions"], 2)

    def test_returns_none_for_missing_or_empty_session(self) -> None:
        self.assertIsNone(telegram_inbox.codex_session_context_snapshot(None))
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            self.assertIsNone(telegram_inbox.codex_session_context_snapshot(missing))
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("not json\n", encoding="utf-8")
            self.assertIsNone(telegram_inbox.codex_session_context_snapshot(empty))


if __name__ == "__main__":
    unittest.main()
