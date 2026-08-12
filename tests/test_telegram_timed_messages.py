from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402


class TelegramTimedMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "timed.state.json"
        self.usage_state = self.root / "usage.state.json"
        self.auth_state = self.root / "auth.state.json"
        self.reauth_state = self.root / "reauth.state.json"
        self.confirmation_state = self.root / "confirmation.state.json"
        self.log_path = self.root / "listener.jsonl"
        self.session = self.root / "rollout.jsonl"
        self.session.write_text("", encoding="utf-8")
        self.message = {
            "message_id": 4197,
            "date": 1_785_116_720,
            "chat": {"id": "123"},
            "from": {"id": 456, "username": "tester", "first_name": "Test"},
            "text": "/timed 0.5 check the goal agent now",
            "reply_to_message": {
                "message_id": 4196,
                "date": 1_785_116_700,
                "from": {"id": 999, "first_name": "Relay Bot"},
                "text": "Previous context remains available.",
            },
        }
        self.args = argparse.Namespace(
            allow_shell_pane=False,
            codex_usage_state_path=str(self.usage_state),
            codex_auth_state_path=str(self.auth_state),
            relay_confirmation_state_path=str(self.confirmation_state),
            submit_delay=0,
            target_pane="tele-agent:codex.0",
        )

    def _schedule(self, *, now: float = 1000.0) -> dict:
        task, created = telegram_inbox.schedule_timed_message(
            self.state,
            self.message,
            "check the goal agent now",
            0.5,
            now=now,
        )
        self.assertTrue(created)
        return task

    def _state_task(self) -> dict:
        return telegram_inbox.read_json_object(self.state)["tasks"][0]

    def _message(self, message_id: int, text: str, *, chat_id: str = "123") -> dict:
        value = json.loads(json.dumps(self.message))
        value["message_id"] = message_id
        value["chat"]["id"] = chat_id
        value["text"] = text
        return value

    def _handle_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            codex_usage_state_path=str(self.usage_state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            codex_auth_state_path=str(self.auth_state),
            codex_reauth_state_path=str(self.reauth_state),
            timed_message_state_path=str(self.state),
            max_log_chars=4000,
        )

    def test_parse_timed_payload_accepts_integer_and_float_hours(self) -> None:
        self.assertEqual(
            telegram_inbox.parse_timed_payload("2 do that thing"),
            (2.0, "do that thing"),
        )
        self.assertEqual(
            telegram_inbox.parse_timed_payload("0.25 check again"),
            (0.25, "check again"),
        )

    def test_parse_timed_payload_rejects_invalid_values(self) -> None:
        for payload in ("", "2", "zero message", "0 message", "-1 message", "nan message"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    telegram_inbox.parse_timed_payload(payload)

    def test_schedule_is_persistent_and_idempotent_per_telegram_message(self) -> None:
        task = self._schedule()
        self.assertEqual(task["due_ts"], 2800.0)
        self.assertEqual(task["message"]["text"], "check the goal agent now")
        self.assertEqual(
            task["message"]["reply_to_message"]["text"],
            "Previous context remains available.",
        )
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

        duplicate, created = telegram_inbox.schedule_timed_message(
            self.state,
            self.message,
            "different text cannot duplicate the same update",
            4.0,
            now=1100.0,
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["due_ts"], 2800.0)
        self.assertEqual(len(telegram_inbox.read_json_object(self.state)["tasks"]), 1)

    def test_handle_update_schedules_decimal_hours_and_replies_with_due_time(self) -> None:
        args = self._handle_args()
        update = {"update_id": 88, "message": self.message}
        with (
            mock.patch.object(telegram_inbox.time, "time", return_value=1000.0),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update,
                args,
                {},
                "token",
                "123",
                self.log_path,
            )

        self.assertEqual(self._state_task()["hours"], 0.5)
        reply = send_reply.call_args.args[2]
        self.assertIn("0.5 hours", reply)
        self.assertIn("> check the goal agent now", reply)
        self.assertIn(
            "<blockquote>check the goal agent now</blockquote>",
            telegram_inbox.render_telegram_html(reply),
        )
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "timed_message_scheduled")

    def test_list_orders_by_due_time_and_renders_telegram_safe_entries(self) -> None:
        self._schedule(now=1000.0)
        early_message = self._message(4200, "check *<the goal agent>* & report")
        telegram_inbox.schedule_timed_message(
            self.state,
            early_message,
            "check *<the goal agent>* & report",
            0.1,
            now=1000.0,
        )

        tasks = telegram_inbox.timed_messages_for_chat(self.state, "123")
        self.assertEqual([task["id"] for task in tasks], ["123:4200", "123:4197"])
        chunks = telegram_inbox.format_timed_message_list(tasks)
        self.assertEqual(len(chunks), 1)
        self.assertIn("**Active timed messages (2)**", chunks[0])
        self.assertIn("**1. Pending**", chunks[0])
        self.assertNotIn("Telegram source", chunks[0])
        self.assertIn("/timed remove N", chunks[0])

        rendered = telegram_inbox.render_telegram_html(chunks[0])
        self.assertIn("<b>Active timed messages (2)</b>", rendered)
        self.assertIn("<b>1. Pending</b>", rendered)
        self.assertIn("<blockquote>", rendered)
        self.assertIn("&lt;the goal agent&gt;", rendered)
        self.assertNotIn("<the goal agent>", rendered)

    def test_long_list_is_chunked_without_dropping_numbers(self) -> None:
        for offset in range(9):
            message = self._message(
                4300 + offset,
                f"task {offset} " + ("x" * 600),
            )
            telegram_inbox.schedule_timed_message(
                self.state,
                message,
                str(message["text"]),
                1.0 + offset,
                now=1000.0,
            )

        chunks = telegram_inbox.format_timed_message_list(
            telegram_inbox.timed_messages_for_chat(self.state, "123")
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= telegram_inbox.TIMED_MESSAGE_LIST_CHUNK_CHARS for chunk in chunks))
        joined = "\n".join(chunks)
        for number in range(1, 10):
            self.assertEqual(joined.count(f"**{number}. Pending**"), 1)

    def test_remove_single_uses_the_number_from_due_time_order(self) -> None:
        self._schedule(now=1000.0)
        early_message = self._message(4200, "earlier task")
        telegram_inbox.schedule_timed_message(
            self.state,
            early_message,
            "earlier task",
            0.1,
            now=1000.0,
        )

        removed = telegram_inbox.remove_timed_messages(
            self.state,
            "123",
            1,
            now=1200.0,
        )
        self.assertEqual([task["id"] for task in removed], ["123:4200"])
        self.assertEqual(
            [task["id"] for task in telegram_inbox.timed_messages_for_chat(self.state, "123")],
            ["123:4197"],
        )
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_completed_messages_are_hidden_and_do_not_consume_numbers(self) -> None:
        completed = self._schedule(now=1000.0)
        state = telegram_inbox.read_json_object(self.state)
        state["tasks"][0]["status"] = "delivered"
        telegram_inbox.write_timed_message_state(self.state, state)

        pending_message = self._message(4200, "still pending")
        telegram_inbox.schedule_timed_message(
            self.state,
            pending_message,
            "still pending",
            1.0,
            now=1000.0,
        )
        listed = telegram_inbox.timed_messages_for_chat(self.state, "123")
        self.assertEqual([task["id"] for task in listed], ["123:4200"])
        self.assertNotIn(completed, listed)
        self.assertIn("**1. Pending**", telegram_inbox.format_timed_message_list(listed)[0])

        removed = telegram_inbox.remove_timed_messages(
            self.state,
            "123",
            1,
            now=1200.0,
        )
        self.assertEqual([task["id"] for task in removed], ["123:4200"])
        persisted = telegram_inbox.read_json_object(self.state)["tasks"]
        self.assertEqual([task["id"] for task in persisted], ["123:4197"])
        self.assertEqual(persisted[0]["status"], "delivered")
        self.assertEqual(telegram_inbox.timed_messages_for_chat(self.state, "123"), [])

    def test_remove_all_is_scoped_to_the_current_chat(self) -> None:
        self._schedule()
        same_chat = self._message(4200, "same chat")
        other_chat = self._message(4201, "other chat", chat_id="999")
        for scheduled_message in (same_chat, other_chat):
            telegram_inbox.schedule_timed_message(
                self.state,
                scheduled_message,
                str(scheduled_message["text"]),
                1.0,
                now=1000.0,
            )

        removed = telegram_inbox.remove_timed_messages(
            self.state,
            "123",
            now=1200.0,
        )
        self.assertEqual({task["id"] for task in removed}, {"123:4197", "123:4200"})
        remaining = telegram_inbox.read_json_object(self.state)["tasks"]
        self.assertEqual([task["id"] for task in remaining], ["999:4201"])

    def test_handle_update_lists_and_removes_a_numbered_message(self) -> None:
        self._schedule()
        args = self._handle_args()
        list_message = self._message(4202, "/timed list")
        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            telegram_inbox.handle_update(
                {"update_id": 89, "message": list_message},
                args,
                {},
                "token",
                "123",
                self.log_path,
            )
        listed_reply = "\n".join(call.args[2] for call in send_reply.call_args_list)
        self.assertIn("**1. Pending**", listed_reply)
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "timed_message_listed")
        self.assertEqual(record["timed_message_count"], 1)

        self.log_path.unlink()
        remove_message = self._message(4203, "/timed remove 1")
        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            telegram_inbox.handle_update(
                {"update_id": 90, "message": remove_message},
                args,
                {},
                "token",
                "123",
                self.log_path,
            )
        self.assertIn("**Timed message 1 removed**", send_reply.call_args.args[2])
        self.assertIn("> check the goal agent now", send_reply.call_args.args[2])
        self.assertEqual(telegram_inbox.timed_messages_for_chat(self.state, "123"), [])
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "timed_message_removed")
        self.assertEqual(record["timed_message_removed_count"], 1)

    def test_handle_update_remove_without_number_clears_all(self) -> None:
        self._schedule()
        second = self._message(4200, "second task")
        telegram_inbox.schedule_timed_message(
            self.state,
            second,
            "second task",
            1.0,
            now=1000.0,
        )
        args = self._handle_args()
        remove_message = self._message(4204, "/timed remove")
        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            telegram_inbox.handle_update(
                {"update_id": 91, "message": remove_message},
                args,
                {},
                "token",
                "123",
                self.log_path,
            )
        self.assertIn("Removed 2 timed messages", send_reply.call_args.args[2])
        self.assertEqual(telegram_inbox.timed_messages_for_chat(self.state, "123"), [])
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "timed_messages_removed_all")

    def test_remove_rejects_invalid_or_out_of_range_number(self) -> None:
        self._schedule()
        for payload in ("remove 0", "remove nope", "remove 2", "remove 1 extra"):
            with self.subTest(payload=payload):
                number: int | None = None
                try:
                    number = telegram_inbox.parse_timed_remove_number(payload)
                    telegram_inbox.remove_timed_messages(self.state, "123", number)
                except ValueError:
                    pass
                else:
                    self.fail(f"{payload!r} should have failed")

    def test_due_message_uses_confirmed_normal_relay_and_only_delivers_once(self) -> None:
        self._schedule()
        checkpoint = (self.session, 0)
        delivery_order: list[str] = []
        relay_texts: list[str] = []

        def confirmed_relay(_pane: str, text: str, **_kwargs) -> str:
            delivery_order.append("relay")
            relay_texts.append(text)
            return (
                "relayed to tele-agent:codex.0 "
                "(pane command: codex; submission confirmed)"
            )

        def visible_echo(*_args, **_kwargs) -> dict:
            delivery_order.append("echo")
            return {"message_id": 5001}

        with (
            mock.patch.object(
                telegram_inbox,
                "ensure_codex_target_for_agent_message",
                return_value=None,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=checkpoint,
            ),
            mock.patch.object(
                telegram_inbox,
                "paste_to_tmux",
                side_effect=confirmed_relay,
            ),
            mock.patch.object(
                telegram_inbox,
                "send_reply",
                side_effect=visible_echo,
            ) as send_reply,
            mock.patch.object(telegram_inbox.time, "time", return_value=2800.0),
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2900.0,
            )

        self.assertEqual(delivery_order, ["echo", "relay"])
        send_reply.assert_called_once()
        self.assertEqual(send_reply.call_args.kwargs["reply_to_message_id"], 4197)
        self.assertIn("**Timed message fired**", send_reply.call_args.args[2])
        self.assertIn("> check the goal agent now", send_reply.call_args.args[2])
        self.assertIn("[TELEGRAM USER MESSAGE message_id=4197", relay_texts[0])
        self.assertIn("@tester | Test | 456", relay_texts[0])
        self.assertIn("Previous context remains available.", relay_texts[0])
        task = self._state_task()
        self.assertEqual(task["status"], "delivered")
        self.assertEqual(task["visible_echo_message_id"], 5001)

    def test_due_message_waits_for_reauth_without_touching_codex(self) -> None:
        self._schedule()
        telegram_inbox.write_json_object(
            self.auth_state,
            {"blocked": True, "reason": "refresh_token_revoked"},
        )
        with (
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(
                telegram_inbox, "ensure_codex_target_for_agent_message"
            ) as ensure_target,
            mock.patch.object(telegram_inbox, "paste_to_tmux") as paste,
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )

        ensure_target.assert_not_called()
        paste.assert_not_called()
        self.assertIn("needs sign-in", send_reply.call_args.args[2])
        task = self._state_task()
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["next_attempt_ts"], 2815.0)
        self.assertTrue(task["auth_failure_notice_sent"])

    def test_pending_confirmation_survives_and_finishes_without_resubmission(self) -> None:
        self._schedule()
        checkpoint = (self.session, 0)
        with (
            mock.patch.object(
                telegram_inbox,
                "ensure_codex_target_for_agent_message",
                return_value=None,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=checkpoint,
            ),
            mock.patch.object(
                telegram_inbox,
                "paste_to_tmux",
                return_value=(
                    "relayed to tele-agent:codex.0 "
                    "(pane command: codex; submission pending confirmation)"
                ),
            ) as relay,
            mock.patch.object(
                telegram_inbox,
                "send_reply",
                return_value={"message_id": 5002},
            ) as send_reply,
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )
            self.assertEqual(self._state_task()["status"], "submitted")

            marker = self._state_task()["marker"]
            with self.session.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": marker + " from @tester | 456] check the goal agent now",
                            },
                        }
                    )
                    + "\n"
                )
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2810.0,
            )

        relay.assert_called_once()
        send_reply.assert_called_once()
        self.assertEqual(self._state_task()["status"], "delivered")

    def test_restart_recovery_confirms_interrupted_delivery_from_jsonl(self) -> None:
        self._schedule()
        state = telegram_inbox.read_json_object(self.state)
        task = state["tasks"][0]
        marker = "[TELEGRAM USER MESSAGE message_id=4197"
        task.update(
            {
                "status": "delivering",
                "attempt_started_ts": 2790.0,
                "marker": marker,
                "session_path": str(self.session),
                "session_offset": 0,
            }
        )
        telegram_inbox.write_json_object(self.state, state)
        with self.session.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": marker + " from @tester | 456] check the goal agent now",
                        },
                    }
                )
                + "\n"
            )

        with (
            mock.patch.object(telegram_inbox, "paste_to_tmux") as relay,
            mock.patch.object(
                telegram_inbox,
                "send_reply",
                return_value={"message_id": 5003},
            ) as send_reply,
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )

        relay.assert_not_called()
        send_reply.assert_called_once()
        self.assertEqual(self._state_task()["status"], "delivered")

    def test_restart_recovery_retries_unconfirmed_delivery_after_grace(self) -> None:
        self._schedule()
        state = telegram_inbox.read_json_object(self.state)
        task = state["tasks"][0]
        task.update(
            {
                "status": "delivering",
                "attempt_started_ts": 2700.0,
                "marker": "[TELEGRAM USER MESSAGE message_id=4197",
                "session_path": str(self.session),
                "session_offset": 0,
            }
        )
        telegram_inbox.write_timed_message_state(self.state, state)
        checkpoint = (self.session, 0)
        with (
            mock.patch.object(
                telegram_inbox,
                "ensure_codex_target_for_agent_message",
                return_value=None,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=checkpoint,
            ),
            mock.patch.object(
                telegram_inbox,
                "paste_to_tmux",
                return_value=(
                    "relayed to tele-agent:codex.0 "
                    "(pane command: codex; submission confirmed)"
                ),
            ) as relay,
            mock.patch.object(
                telegram_inbox,
                "send_reply",
                return_value={"message_id": 5004},
            ) as send_reply,
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )

        relay.assert_called_once()
        send_reply.assert_called_once()
        self.assertEqual(self._state_task()["status"], "delivered")

    def test_visible_echo_failure_prevents_codex_delivery_and_retries(self) -> None:
        self._schedule()
        with (
            mock.patch.object(
                telegram_inbox,
                "ensure_codex_target_for_agent_message",
                return_value=None,
            ),
            mock.patch.object(
                telegram_inbox,
                "send_reply",
                side_effect=telegram_inbox.TransientTelegramError("offline"),
            ),
            mock.patch.object(telegram_inbox, "paste_to_tmux") as relay,
        ):
            telegram_inbox.process_due_timed_messages(
                self.args,
                {},
                "token",
                "123",
                self.state,
                self.log_path,
                now=2800.0,
            )

        relay.assert_not_called()
        task = self._state_task()
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["next_attempt_ts"], 2815.0)
        self.assertIn("visible timed-message echo failed", task["last_error"])


if __name__ == "__main__":
    unittest.main()
