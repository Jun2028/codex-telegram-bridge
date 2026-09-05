from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from teleagent import queue_history, routing, state, transport, ui


class QueueHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            relay_queue_state_path=str(self.root / "queue.json"),
            ingress_state_path=str(self.root / "inbox.json"),
            relay_confirmation_state_path=str(self.root / "confirmations.json"),
            reply_route_state_path=str(self.root / "routes.json"),
        )

    def archived(self, identifier=21, route="u21"):
        return {
            "message_id": identifier,
            "created_ts": 1786632844,
            "stalled_reason": "stale_pending_cleared",
            "relay_text": f"[TELEGRAM USER MESSAGE message_id={identifier} route_id={route} from user] do the task",
            "session_path": str(self.root / f"session-{identifier}.jsonl"),
        }

    def save(self, *items):
        path = Path(self.args.relay_confirmation_state_path)
        state.write_json_object(path, {"pending": [], "failed": list(items)})
        return path

    def test_archived_checks_are_not_presented_as_current_failures(self):
        item = self.archived()
        self.save(item)
        current = ui.queue_text(self.args, "123", False, None)
        self.assertIn("Waiting: 0 · incoming: 0", current)
        self.assertIn("History: 1 archived", current)
        self.assertNotIn("failed:", current)
        self.assertNotIn("#21", current)
        history = ui.queue_text(self.args, "123", False, None, history=True)
        self.assertIn("13 Aug 22:54 · #21", history)
        self.assertIn("confirmation timed out", history)
        self.assertIn("session log unavailable", history)

    def test_late_receipt_is_detected_without_mutating_or_replaying_anything(self):
        item = self.archived()
        path = self.save(item)
        original = path.read_bytes()
        session = Path(item["session_path"])
        session.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": item["relay_text"]},
                }
            )
            + "\n"
        )
        with mock.patch.object(transport, "send_reply") as send:
            result = ui.queue_text(self.args, "123", False, None, history=True)
        self.assertIn("received by agent", result)
        self.assertNotIn("receipt unconfirmed", result)
        self.assertEqual(path.read_bytes(), original)
        send.assert_not_called()

    def test_quotes_and_different_payloads_do_not_prove_receipt(self):
        item = self.archived()
        path = Path(item["session_path"])
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": item["relay_text"]}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": item["relay_text"].replace(
                        "do the task", "different task"
                    ),
                },
            },
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertEqual(queue_history.receipt_status(item), "receipt unconfirmed")
        with path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"text": item["relay_text"]}],
                        },
                    }
                )
                + "\n"
            )
        self.assertIn("received by agent", queue_history.receipt_status(item))

    def test_history_keeps_group_and_topic_visibility(self):
        private, group, other_topic = (
            self.archived(21),
            self.archived(22, "u22"),
            self.archived(23, "u23"),
        )
        self.save(private, group, other_topic)
        path = Path(self.args.reply_route_state_path)
        routing.set_reply_route_chat_id(path, "123", is_group=False, route_id="u21")
        routing.set_reply_route_chat_id(
            path, "-99", is_group=True, route_id="u22", message_thread_id=7
        )
        routing.set_reply_route_chat_id(
            path, "-99", is_group=True, route_id="u23", message_thread_id=8
        )
        result = ui.queue_text(self.args, "-99", True, 7, history=True)
        self.assertIn("#22", result)
        self.assertNotIn("#21", result)
        self.assertNotIn("#23", result)

    def test_history_button_returns_a_dated_view_to_the_same_topic(self):
        item = self.archived()
        self.save(item)
        routing.set_reply_route_chat_id(
            Path(self.args.reply_route_state_path),
            "-99",
            is_group=True,
            route_id="u21",
            message_thread_id=7,
        )
        update = {
            "update_id": 1,
            "callback_query": {
                "id": "callback",
                "data": "relay:history",
                "from": {"id": 123},
                "message": {
                    "message_id": 99,
                    "chat": {"id": "-99", "type": "supergroup"},
                    "message_thread_id": 7,
                },
            },
        }
        with (
            mock.patch.object(transport, "telegram_api"),
            mock.patch.object(transport, "send_reply") as send,
        ):
            self.assertTrue(
                ui.handle_quick_update(
                    update, self.args, "token", "123", "123", "OurBot", {}
                )
            )
        self.assertEqual(send.call_args.args[1], "-99")
        self.assertIn("13 Aug 22:54", send.call_args.args[2])
        self.assertEqual(send.call_args.kwargs["message_thread_id"], 7)


if __name__ == "__main__":
    unittest.main()
