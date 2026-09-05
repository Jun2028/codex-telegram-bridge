from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402
from teleagent import lifecycle as _relay_lifecycle
from teleagent import processes as _relay_processes
from teleagent import submission as _relay_submission


class TelegramInboxRecordIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_path = self.root / "listener.jsonl"

    def test_group_mentions_are_required_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(telegram_inbox.require_group_mention())

            base = {
                "message": {
                    "chat": {"id": "-123", "type": "supergroup"},
                    "from": {"id": "42"},
                    "text": "hello",
                }
            }
            self.assertEqual(
                telegram_inbox.resolve_source_chat(
                    base, "private", "42", "fixturebot", "token", {}
                ),
                (None, True, True),
            )

            mentioned = json.loads(json.dumps(base))
            mentioned["message"]["text"] = "@fixturebot hello"
            self.assertEqual(
                telegram_inbox.resolve_source_chat(
                    mentioned, "private", "42", "fixturebot", "token", {}
                ),
                ("-123", True, True),
            )

    def test_codex_agent_message_parses_response_item_assistant_text(self) -> None:
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "Hi! 你好 👋"},
                ],
            },
        }
        self.assertEqual(
            telegram_inbox.codex_agent_message(record),
            ("Hi! 你好 👋", "final_answer"),
        )

    def test_codex_agent_message_ignores_non_assistant_response_items(self) -> None:
        record = {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        }
        self.assertIsNone(telegram_inbox.codex_agent_message(record))

    def test_per_chat_paths_are_private_and_group_scoped(self) -> None:
        with mock.patch.dict(
            os.environ,
            {telegram_inbox.PER_CHAT_INBOX_RECORDS_ENV: "1"},
        ):
            private = telegram_inbox.per_chat_inbox_record_path(
                self.log_path,
                "123456789",
                "private",
            )
            group = telegram_inbox.per_chat_inbox_record_path(
                self.log_path,
                "-100123456789",
                "supergroup",
            )
        self.assertEqual(
            private,
            self.root / "listener.private.123456789.jsonl",
        )
        self.assertEqual(
            group,
            self.root / "listener.group.-100123456789.jsonl",
        )
        self.assertIsNone(
            telegram_inbox.per_chat_inbox_record_path(
                self.log_path,
                "123456789",
                "private",
            )
        )

    def test_backfill_copies_historical_records_per_chat_without_duplicates(self) -> None:
        records = [
            {
                "ts": 1,
                "update_id": 10,
                "message_id": 100,
                "chat_id": "123456789",
                "chat_type": "private",
                "action": "agent",
                "text": "private hello",
            },
            {
                "ts": 2,
                "update_id": 11,
                "message_id": 200,
                "chat_id": "-100123456789",
                "chat_type": "supergroup",
                "action": "agent",
                "text": "group hello",
            },
        ]
        self.log_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {telegram_inbox.PER_CHAT_INBOX_RECORDS_ENV: "1"},
        ):
            telegram_inbox.backfill_per_chat_inbox_records(self.log_path)
            private_path = self.root / "listener.private.123456789.jsonl"
            group_path = self.root / "listener.group.-100123456789.jsonl"
            self.assertEqual(
                len(private_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            self.assertEqual(
                len(group_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            telegram_inbox.backfill_per_chat_inbox_records(self.log_path)
            self.assertEqual(
                len(private_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            self.assertEqual(
                len(group_path.read_text(encoding="utf-8").splitlines()),
                1,
            )

    def test_people_memory_is_keyed_by_sender_and_reused_across_chats(self) -> None:
        state_path = self.root / "people.json"
        alice_private = {
            "message_id": 1,
            "date": 100,
            "chat": {"id": "10", "type": "private"},
            "from": {"id": 111, "username": "alice", "first_name": "Alice"},
        }
        alice_group = {
            "message_id": 2,
            "date": 200,
            "chat": {"id": "-20", "type": "supergroup", "title": "Friends"},
            "from": {"id": 111, "username": "alice", "first_name": "Alice"},
        }
        bob_group = {
            "message_id": 3,
            "date": 300,
            "chat": {"id": "-20", "type": "supergroup", "title": "Friends"},
            "from": {"id": 222, "username": "bob", "first_name": "Bob"},
        }
        with mock.patch.dict(os.environ, {telegram_inbox.PEOPLE_MEMORY_ENV: "1"}):
            self.assertTrue(
                telegram_inbox.remember_telegram_person(
                    state_path, alice_private, "I play the cello."
                )
            )
            telegram_inbox.remember_telegram_person(
                state_path, alice_group, "My favorite food is hotpot."
            )
            telegram_inbox.remember_telegram_person(
                state_path, bob_group, "I prefer jazz."
            )
            alice_context = telegram_inbox.telegram_person_context(
                state_path, alice_group
            )
            bob_context = telegram_inbox.telegram_person_context(state_path, bob_group)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(set(state["people"]), {"111", "222"})
        self.assertEqual(set(state["people"]["111"]["chats"]), {"10", "-20"})
        self.assertIn("I play the cello.", alice_context)
        self.assertIn("My favorite food is hotpot.", alice_context)
        self.assertNotIn("I prefer jazz.", alice_context)
        self.assertIn("I prefer jazz.", bob_context)
        self.assertNotIn("cello", bob_context)

    def test_successful_relay_injects_prior_person_context_and_updates_record(self) -> None:
        state_path = self.root / "people.json"
        args = argparse.Namespace(
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"),
            people_memory_state_path=str(state_path),
            repo_root=self.root,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
            relay_mode="tmux-enter",
            allow_shell_pane=False,
            submit_delay=0,
            bridge_ack=False,
        )

        def update(message_id: int, text: str) -> dict[str, object]:
            return {
                "update_id": message_id,
                "message": {
                    "message_id": message_id,
                    "date": 1_900_000_000 + message_id,
                    "chat": {"id": "123", "type": "private"},
                    "from": {
                        "id": 456,
                        "username": "tester",
                        "first_name": "Test",
                    },
                    "text": text,
                },
            }

        with (
            mock.patch.dict(os.environ, {telegram_inbox.PEOPLE_MEMORY_ENV: "1"}),
            mock.patch.object(
                _relay_lifecycle, "ensure_codex_target_for_agent_message", return_value=None
            ),
            mock.patch.object(_relay_processes, "codex_target_ready", return_value=False),
            mock.patch.object(
                _relay_submission, "paste_to_tmux", return_value="relayed to tele-agent:codex.0"
            ) as paste,
            mock.patch.object(
                telegram_inbox.agent_registry, "active_agent_for_pane", return_value=None
            ),
        ):
            telegram_inbox.handle_update(
                update(1, "I collect vinyl."), args, {}, "token", "123", self.log_path
            )
            telegram_inbox.handle_update(
                update(2, "Any recommendations?"), args, {}, "token", "123", self.log_path
            )

        first_relay = paste.call_args_list[0].args[1]
        second_relay = paste.call_args_list[1].args[1]
        self.assertNotIn("KNOWN TELEGRAM PERSON CONTEXT", first_relay)
        self.assertIn("KNOWN TELEGRAM PERSON CONTEXT", second_relay)
        self.assertIn("I collect vinyl.", second_relay)
        observations = json.loads(state_path.read_text(encoding="utf-8"))["people"]["456"]["observations"]
        self.assertEqual(
            [item["text"] for item in observations],
            ["I collect vinyl.", "Any recommendations?"],
        )


if __name__ == "__main__":
    unittest.main()
