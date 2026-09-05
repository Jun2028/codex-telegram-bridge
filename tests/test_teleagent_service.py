from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from teleagent import (
    commands,
    events,
    messages,
    queue,
    routing,
    service,
    state,
    status,
    transport,
    ui,
)


class RelayServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @staticmethod
    def update(identifier=1, chat="123", text="hello", topic=None):
        message = {
            "message_id": identifier,
            "chat": {"id": chat, "type": "private" if chat == "123" else "supergroup"},
            "from": {"id": 123},
            "text": text,
        }
        if topic is not None:
            message["message_thread_id"] = topic
        return {"update_id": identifier, "message": message}

    def args(self):
        return argparse.Namespace(
            session="test",
            target_pane="test:codex.0",
            tmux_lines=20,
            relay_queue_state_path=str(self.root / "queue.json"),
            ingress_state_path=str(self.root / "ingress.json"),
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"),
            codex_auth_state_path=str(self.root / "auth.json"),
            max_log_chars=4000,
        )

    def test_inbox_survives_restart_without_repeating_an_uncertain_action(self):
        inbox = service.Inbox(self.root / "inbox.json")
        self.assertTrue(inbox.accept(self.update(1)))
        self.assertFalse(inbox.accept(self.update(1)))
        self.assertEqual(inbox.take()["update_id"], 1)
        restarted = service.Inbox(inbox.path)
        self.assertEqual(restarted.recover(), 1)
        self.assertIsNone(restarted.take())
        self.assertFalse(restarted.accept(self.update(1)))
        self.assertEqual(len(state.read_json_object(inbox.path)["failed"]), 1)

    def test_inbox_keeps_accepted_but_unstarted_work_in_order(self):
        inbox = service.Inbox(self.root / "inbox.json")
        for identifier in (1, 2, 3):
            inbox.accept(self.update(identifier))
        inbox.recover()
        for identifier in (1, 2, 3):
            self.assertEqual(inbox.take()["update_id"], identifier)
            inbox.finish(identifier)
        self.assertIsNone(inbox.take())

    def test_slow_control_does_not_block_outbound_delivery(self):
        inbox = service.Inbox(self.root / "inbox.json")
        inbox.accept(self.update())
        busy, release, sent = threading.Event(), threading.Event(), threading.Event()

        def control(_update):
            busy.set()
            release.wait(3)

        workers = service.RelayWorkers(
            inbox,
            control,
            lambda: None,
            sent.set,
            lambda *args: None,
            self.root / "health.json",
        )
        workers.start()
        try:
            self.assertTrue(busy.wait(1))
            self.assertTrue(sent.wait(1))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            workers.close()

    def test_concurrent_state_mutations_are_not_lost(self):
        path = self.root / "counter.json"

        @state.serialized
        def increment(path):
            value = state.read_json_object(path)
            value["count"] = value.get("count", 0) + 1
            state.write_json_object(path, value)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(increment, [path] * 40))
        self.assertEqual(state.read_json_object(path)["count"], 40)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_exact_mentions_captions_replies_and_other_bot_commands(self):
        self.assertTrue(
            routing.message_mentions_bot({"caption": "😀 @OurBot read this"}, "OurBot")
        )
        self.assertFalse(
            routing.message_mentions_bot({"text": "@OurBot_backup hello"}, "OurBot")
        )
        self.assertTrue(
            routing.message_mentions_bot(
                {"reply_to_message": {"from": {"is_bot": True, "username": "OurBot"}}},
                "OurBot",
            )
        )
        self.assertFalse(
            routing.message_mentions_bot(
                {
                    "reply_to_message": {
                        "from": {"is_bot": True, "username": "OtherBot"}
                    }
                },
                "OurBot",
            )
        )
        update = self.update(chat="-99", text="/kill_agent@OtherBot @OurBot")
        self.assertIsNone(
            routing.resolve_source_chat(update, "123", "123", "OurBot", "token", {})[0]
        )

    def test_group_identity_is_discovered_from_the_existing_private_setup(self):
        with mock.patch.object(
            transport, "telegram_api", return_value={"id": 999, "username": "OurBot"}
        ) as api:
            owner, username = routing.resolve_bot_identity("token", "123")
        self.assertEqual((owner, username), ("123", "OurBot"))
        api.assert_called_once_with("token", "getMe", timeout=10)
        self.assertEqual(
            routing.resolve_source_chat(
                self.update(chat="-99", text="/status@OurBot"),
                "123",
                owner,
                username,
                "token",
                {},
            ),
            ("-99", True, True),
        )

    def test_explicit_bot_identity_does_not_require_a_profile_query(self):
        with mock.patch.object(transport, "telegram_api") as api:
            self.assertEqual(
                routing.resolve_bot_identity("token", "123", "456", "@NamedBot"),
                ("456", "NamedBot"),
            )
        api.assert_not_called()

    def test_unknown_and_cross_chat_routes_cannot_send_to_a_group(self):
        routes = {
            "private": {"chat_id": "123"},
            "group": {"chat_id": "-99", "is_group": True, "message_thread_id": 7},
        }
        turn = events.TurnDelivery("123")
        turn.begin("first")
        turn.bind("private", routes.get)
        turn.bind("group", routes.get)
        self.assertEqual(turn.chat_id, "123")
        self.assertTrue(turn.conflicted)
        restored = events.TurnDelivery.restore(turn.snapshot(), "123")
        restored.bind("group", routes.get)
        self.assertEqual(restored.chat_id, "123")
        restored.begin("second")
        restored.bind("group", routes.get)
        self.assertEqual((restored.chat_id, restored.topic_id), ("-99", 7))

    def test_route_destinations_are_immutable(self):
        path = self.root / "routes.json"
        routing.set_reply_route_chat_id(path, "123", is_group=False, route_id="u1")
        with self.assertRaises(ValueError):
            routing.set_reply_route_chat_id(path, "-99", is_group=True, route_id="u1")

    def test_only_the_bound_chat_and_topic_can_steer_the_existing_turn(self):
        from teleagent import processes, submission

        args = self.args()
        args.agent_message_state_path = str(self.root / "delivery.json")
        session = self.root / "session.jsonl"
        session.touch()
        state.write_json_object(
            Path(args.agent_message_state_path),
            {
                "route_locked": True,
                "active_chat_id": "-99",
                "active_topic_id": 7,
                "session_path": str(session.resolve()),
            },
        )
        with (
            mock.patch.object(
                submission, "codex_session_checkpoint", return_value=(session, 0)
            ),
            mock.patch.object(
                submission, "codex_session_turn_active", return_value=True
            ),
            mock.patch.object(processes, "codex_goal_active", return_value=False),
        ):
            self.assertTrue(
                queue.same_chat_can_steer(args, self.update(chat="-99", topic=7))
            )
            self.assertFalse(queue.same_chat_can_steer(args, self.update(chat="123")))
            self.assertFalse(
                queue.same_chat_can_steer(args, self.update(chat="-99", topic=8))
            )

    def test_queue_preserves_receipt_errors_and_hides_them_from_other_chats(self):
        args = self.args()
        args.relay_confirmation_state_path = str(self.root / "confirmations.json")
        args.reply_route_state_path = str(self.root / "routes.json")
        routing.set_reply_route_chat_id(
            Path(args.reply_route_state_path), "123", is_group=False, route_id="u1"
        )
        state.write_json_object(
            Path(args.relay_confirmation_state_path),
            {
                "pending": [
                    {
                        "message_id": 1,
                        "relay_text": "[TELEGRAM USER MESSAGE message_id=1 route_id=u1 from user] private",
                    }
                ],
                "failed": [
                    {
                        "message_id": 2,
                        "created_ts": 1786632844,
                        "last_error": "receipt write permission denied",
                        "relay_text": "[TELEGRAM USER MESSAGE message_id=2 route_id=u1 from user] private",
                    }
                ],
            },
        )
        private = ui.queue_text(args, "123", False, None)
        self.assertIn("Delivery check waiting: 1", private)
        self.assertIn("13 Aug 22:54 · #2: receipt write permission denied", private)
        group = ui.queue_text(args, "-99", True, 7)
        self.assertNotIn("Delivery check waiting:", group)
        self.assertNotIn("Delivery problems:", group)
        self.assertNotIn("permission denied", group)
        state.write_json_object(Path(args.relay_confirmation_state_path), {})
        self.assertEqual(
            ui.queue_text(args, "123", False, None),
            "Waiting: 0 · incoming: 0\nNo messages waiting for the agent.",
        )

    def test_route_markers_inside_quoted_text_are_not_authoritative(self):
        self.assertIsNone(
            messages.telegram_route_id_from_text(
                "quote:\n[TELEGRAM USER MESSAGE message_id=2 route_id=u2 from user]"
            )
        )
        body = "```python\nfor x in range(2):\n    print(x)\n```"
        self.assertIn(
            body,
            messages.format_agent_message(
                self.update()["message"], body, route_id="u1"
            ),
        )

    def test_group_queue_and_cancel_cannot_expose_or_remove_private_work(self):
        args = self.args()
        path = Path(args.relay_queue_state_path)
        queue.enqueue_telegram_relay(
            path, self.update(1, text="PRIVATE SECRET"), args.target_pane
        )
        queue.enqueue_telegram_relay(
            path, self.update(2, "-99", "group task", 7), args.target_pane
        )
        queue.enqueue_telegram_relay(
            path, self.update(3, "-99", "other topic", 8), args.target_pane
        )
        text = ui.queue_text(args, "-99", True, 7)
        self.assertIn("group task", text)
        self.assertNotIn("PRIVATE SECRET", text)
        self.assertNotIn("other topic", text)
        ui.cancel_queued(args, "all", "-99", True, 7)
        self.assertEqual(
            [item["message_id"] for item in queue.telegram_relay_queue_tasks(path)],
            [1, 3],
        )

    def test_group_owner_controls_and_unauthorized_callbacks(self):
        args = self.args()
        with (
            mock.patch.object(
                status, "format_system_status", return_value="idle"
            ) as formatter,
            mock.patch.object(transport, "send_reply") as send,
        ):
            self.assertTrue(
                ui.handle_quick_update(
                    self.update(1, "-99", "/status@OurBot", 7),
                    args,
                    "token",
                    "123",
                    "123",
                    "OurBot",
                    {},
                )
            )
            self.assertEqual(send.call_args.args[1], "-99")
            self.assertEqual(send.call_args.kwargs["message_thread_id"], 7)
            self.assertEqual(formatter.call_args.kwargs["audience_chat_id"], "-99")
        callback = {
            "update_id": 5,
            "callback_query": {
                "id": "query",
                "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": "relay:status",
            },
        }
        with (
            mock.patch.object(transport, "telegram_api"),
            mock.patch.object(transport, "send_reply") as send,
        ):
            ui.handle_quick_update(callback, args, "token", "123", "123", "OurBot", {})
        send.assert_not_called()

    def test_group_auth_failure_never_exposes_private_login_instructions(self):
        args = self.args()
        state.write_json_object(
            Path(args.codex_auth_state_path),
            {"blocked": True, "reason": "refresh_token_revoked"},
        )
        with (
            mock.patch.object(
                commands._auth,
                "active_codex_auth_failure",
                return_value={"blocked": True},
            ),
            mock.patch.object(
                commands._auth,
                "format_auth_failure_fallback",
                return_value="PRIVATE LOGIN CODE",
            ) as private,
            mock.patch.object(transport, "send_reply") as send,
        ):
            commands.handle_update(
                self.update(1, "-99", "@OurBot hello"),
                args,
                {},
                "token",
                "123",
                self.root / "inbox.jsonl",
                owner_user_id="123",
                bot_username="OurBot",
            )
        private.assert_not_called()
        self.assertNotIn("PRIVATE LOGIN CODE", send.call_args.args[2])

    def test_unicode_long_replies_keep_every_character_and_destination(self):
        text = "Result\n" + "😀 a line\n" * 700
        parts = transport.split_reply(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(
            all(len(part.encode("utf-16-le")) // 2 <= 3800 for part in parts)
        )
        with mock.patch.object(transport, "telegram_api", return_value={}) as api:
            transport.send_reply(
                "token", "-99", text, message_thread_id=7, reply_to_message_id=11
            )
        self.assertGreater(api.call_count, 1)
        for call in api.call_args_list:
            self.assertEqual(call.args[2]["chat_id"], "-99")
            self.assertEqual(call.args[2]["message_thread_id"], 7)

    def test_submission_confirmation_ignores_assistant_quotes_and_tool_output(self):
        from teleagent import submission

        path = self.root / "session.jsonl"
        marker = "[TELEGRAM USER MESSAGE message_id=42"
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": marker}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": marker},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "quoted: " + marker},
            },
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertFalse(
            submission.wait_for_codex_submission((path, 0), marker, timeout=0)
        )
        with path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": marker + "] actual",
                        },
                    }
                )
                + "\n"
            )
        self.assertTrue(
            submission.wait_for_codex_submission((path, 0), marker, timeout=0)
        )

    def test_report_notification_is_compact_and_does_not_mine_log_lines(self):
        from notify import telegram_safe_message

        result = telegram_safe_message(
            "Run completed",
            "status: completed\nexit_status: 0\nruntime: 2m\nhost: fixture\nfinished_at: 12:00\npbs_jobid: \nsummary:\n- [INFO] judge step finished\n- Process exited successfully.",
        )
        self.assertIn("Completed · 2m · fixture", result)
        self.assertIn("Finished: 12:00", result)
        self.assertNotIn("judge step", result)
        self.assertNotIn("pbs_jobid", result)


if __name__ == "__main__":
    unittest.main()
