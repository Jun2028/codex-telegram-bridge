from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402
from teleagent import transport as _relay_transport


class TelegramReplyRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.session = self.root / "rollout.jsonl"
        self.session.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-17T00:00:00Z",
                    "payload": {"cwd": str(self.repo)},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.routes = self.root / "reply-route.state.json"
        self.messages = self.root / "agent-messages.state.json"
        self.meta = {
            "agent_id": "agent-test",
            "repo_root": str(self.repo),
            "codex_session_path": str(self.session),
            "codex_session_detection": "process_fd",
            "created_ts": 0,
        }

    def write_records(self, *records: dict) -> None:
        with self.session.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def user(text: str) -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    @staticmethod
    def assistant(message_id: str, phase: str, text: str) -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": message_id,
                "phase": phase,
                "content": [{"type": "output_text", "text": text}],
            },
        }

    def test_route_marker_is_unique_to_the_update(self) -> None:
        message = {
            "message_id": 7,
            "from": {"id": 10, "username": "tester"},
            "chat": {"id": "private", "type": "private"},
            "text": "hello",
        }
        formatted = telegram_inbox.format_agent_message(
            message,
            "hello",
            route_id=telegram_inbox.telegram_update_route_id({"update_id": 99}),
        )

        self.assertIn("route_id=u99", formatted)
        self.assertEqual(
            telegram_inbox.telegram_route_id_from_text(formatted),
            "u99",
        )

    def test_cross_chat_input_in_the_same_turn_quarantines_remaining_output(
        self,
    ) -> None:
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "private-chat", is_group=False, route_id="u1"
        )
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "group-chat", is_group=True, route_id="u2"
        )
        self.write_records(
            self.user(
                "[TELEGRAM USER MESSAGE message_id=1 route_id=u1 from user] private"
            ),
            self.assistant("a1", "commentary", "private progress"),
            self.user(
                "[TELEGRAM USER MESSAGE message_id=2 route_id=u2 from user] group"
            ),
            self.assistant("a2", "final_answer", "group answer"),
        )

        with mock.patch.object(_relay_transport, "send_reply") as send_reply:
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
            )

        self.assertEqual(sent, 2)
        self.assertEqual(
            [call.args[1] for call in send_reply.call_args_list],
            ["private-chat", "owner-private"],
        )

    def test_missing_route_fails_closed_to_owner_private_chat(self) -> None:
        self.write_records(
            self.user(
                "[TELEGRAM USER MESSAGE message_id=3 route_id=expired from user] group"
            ),
            self.assistant("a3", "final_answer", "do not leak"),
        )

        with mock.patch.object(_relay_transport, "send_reply") as send_reply:
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
                is_group_route=True,
            )

        self.assertEqual(sent, 1)
        self.assertEqual(send_reply.call_args.args[1], "owner-private")
        self.assertTrue(send_reply.call_args.args[2].endswith(" ∎"))

    def test_real_turn_boundaries_allow_a_new_group_request_and_deduplicate_schemas(
        self,
    ):
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "owner-private", is_group=False, route_id="u21"
        )
        telegram_inbox.set_reply_route_chat_id(
            self.routes,
            "group-chat",
            is_group=True,
            route_id="u22",
            message_thread_id=7,
            source_message_id=22,
        )
        self.write_records(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "first"},
            },
            self.user(
                "[TELEGRAM USER MESSAGE message_id=21 route_id=u21 from user] private"
            ),
            {"type": "turn_context", "payload": {"turn_id": "first"}},
            self.assistant("a21", "final_answer", "private result"),
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "private result",
                    "phase": "final_answer",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "first"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "second"},
            },
            self.user(
                "[TELEGRAM USER MESSAGE message_id=22 route_id=u22 from user] group"
            ),
            {"type": "turn_context", "payload": {"turn_id": "second"}},
            self.assistant("a22", "final_answer", "group result"),
        )
        with mock.patch.object(_relay_transport, "send_reply") as send:
            telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
            )
        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            ["owner-private", "group-chat"],
        )
        self.assertEqual(send.call_args.kwargs["message_thread_id"], 7)
        self.assertEqual(send.call_args.kwargs["reply_to_message_id"], 22)

    def test_restart_between_private_progress_and_group_input_keeps_quarantine(self):
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "owner-private", is_group=False, route_id="u31"
        )
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "group-chat", is_group=True, route_id="u32"
        )
        self.write_records(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "one"},
            },
            self.user(
                "[TELEGRAM USER MESSAGE message_id=31 route_id=u31 from user] private"
            ),
            self.assistant("a31", "commentary", "private progress"),
        )
        with mock.patch.object(_relay_transport, "send_reply"):
            telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
            )
        self.write_records(
            self.user(
                "[TELEGRAM USER MESSAGE message_id=32 route_id=u32 from user] group"
            ),
            self.assistant("a32", "final_answer", "private material"),
        )
        with mock.patch.object(_relay_transport, "send_reply") as send:
            telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
            )
        self.assertEqual(send.call_args.args[1], "owner-private")

    def test_newer_group_route_does_not_overwrite_prior_private_reply(self) -> None:
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "private-chat", is_group=False, route_id="u10"
        )
        telegram_inbox.set_reply_route_chat_id(
            self.routes, "group-chat", is_group=True, route_id="u11"
        )
        self.write_records(
            self.user(
                "[TELEGRAM USER MESSAGE message_id=10 route_id=u10 from user] private"
            ),
            self.assistant("a10", "final_answer", "private answer"),
        )

        with mock.patch.object(_relay_transport, "send_reply") as send_reply:
            telegram_inbox.drain_codex_agent_messages(
                "token",
                "owner-private",
                self.meta,
                self.messages,
                None,
                {},
                sessions_root=self.root,
                route_state_path=self.routes,
            )

        self.assertEqual(send_reply.call_args.args[1], "private-chat")


if __name__ == "__main__":
    unittest.main()
