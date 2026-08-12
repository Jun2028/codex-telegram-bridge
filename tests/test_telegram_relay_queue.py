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


class TelegramRelayQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue_state = self.root / "relay-queue.state.json"
        self.confirmation_state = self.root / "relay-confirmation.state.json"
        self.log_path = self.root / "listener.jsonl"
        self.session = self.root / "rollout.jsonl"
        self.session.write_text("", encoding="utf-8")
        self.args = argparse.Namespace(
            target_pane="tele-agent:codex.0",
            relay_queue_state_path=str(self.queue_state),
            relay_confirmation_state_path=str(self.confirmation_state),
        )

    @staticmethod
    def update(message_id: int, text: str) -> dict:
        return {
            "update_id": 1000 + message_id,
            "message": {
                "message_id": message_id,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": text,
            },
        }

    def seed_pending(self, message_id: int = 10) -> None:
        telegram_inbox.write_json_object(
            self.confirmation_state,
            {
                "pending": [
                    {
                        "marker": f"[TELEGRAM USER MESSAGE message_id={message_id}",
                        "message_id": message_id,
                        "target_pane": self.args.target_pane,
                    }
                ],
                "version": 1,
            },
        )

    @staticmethod
    def session_event(event_type: str) -> str:
        return json.dumps(
            {"type": "event_msg", "payload": {"type": event_type}}
        )

    @staticmethod
    def session_user_message(text: str) -> str:
        return json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    def test_turn_is_busy_between_user_message_and_task_started_event(self) -> None:
        self.session.write_text(
            "\n".join(
                [
                    self.session_event("task_complete"),
                    self.session_user_message("accepted but not started"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertTrue(telegram_inbox.codex_session_turn_active(self.session))

    def test_turn_is_idle_after_terminal_event(self) -> None:
        self.session.write_text(
            "\n".join(
                [
                    self.session_user_message("finished turn"),
                    self.session_event("task_started"),
                    self.session_event("task_complete"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertFalse(telegram_inbox.codex_session_turn_active(self.session))

    def test_consecutive_messages_are_persisted_in_fifo_order(self) -> None:
        self.seed_pending()
        first = self.update(11, "second message")
        second = self.update(12, "third message")

        with mock.patch.object(telegram_inbox, "handle_update") as handle:
            self.assertEqual(
                telegram_inbox.dispatch_telegram_update(
                    first, self.args, {}, "token", "123", self.log_path
                ),
                "queued",
            )
            self.assertEqual(
                telegram_inbox.dispatch_telegram_update(
                    second, self.args, {}, "token", "123", self.log_path
                ),
                "queued",
            )

        handle.assert_not_called()
        tasks = telegram_inbox.telegram_relay_queue_tasks(self.queue_state)
        self.assertEqual([task["message_id"] for task in tasks], [11, 12])
        self.assertEqual(
            [task["update"]["message"]["text"] for task in tasks],
            ["second message", "third message"],
        )

    def test_unauthorized_chat_is_not_written_to_the_queue(self) -> None:
        self.seed_pending()
        update = self.update(13, "must not be persisted")
        update["message"]["chat"]["id"] = "999"

        with mock.patch.object(telegram_inbox, "handle_update") as handle:
            result = telegram_inbox.dispatch_telegram_update(
                update, self.args, {}, "token", "123", self.log_path
            )

        self.assertEqual(result, "handled")
        handle.assert_called_once()
        self.assertEqual(
            telegram_inbox.telegram_relay_queue_tasks(self.queue_state), []
        )

    def test_normal_message_is_queued_while_a_turn_is_active(self) -> None:
        update = self.update(14, "wait behind active turn")
        with (
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=(self.session, 0),
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_turn_active", return_value=True
            ),
            mock.patch.object(telegram_inbox, "handle_update") as handle,
        ):
            result = telegram_inbox.dispatch_telegram_update(
                update, self.args, {}, "token", "123", self.log_path
            )

        self.assertEqual(result, "queued")
        handle.assert_not_called()
        self.assertEqual(
            [
                task["message_id"]
                for task in telegram_inbox.telegram_relay_queue_tasks(
                    self.queue_state
                )
            ],
            [14],
        )

    def test_queue_survives_reload_and_drains_one_message_at_a_time(self) -> None:
        for message_id in (21, 22):
            telegram_inbox.enqueue_telegram_relay(
                self.queue_state,
                self.update(message_id, f"message {message_id}"),
                self.args.target_pane,
                now=float(message_id),
            )

        delivered: list[int] = []

        def fake_handle(update, *_args):
            delivered.append(update["message"]["message_id"])

        with (
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=None
            ),
            mock.patch.object(
                telegram_inbox, "handle_update", side_effect=fake_handle
            ),
        ):
            first = telegram_inbox.drain_telegram_relay_queue(
                self.args, {}, "token", "123", self.log_path
            )
            self.assertEqual(delivered, [21])
            self.assertEqual(
                [
                    task["message_id"]
                    for task in telegram_inbox.telegram_relay_queue_tasks(
                        self.queue_state
                    )
                ],
                [22],
            )
            second = telegram_inbox.drain_telegram_relay_queue(
                self.args, {}, "token", "123", self.log_path
            )

        self.assertEqual(delivered, [21, 22])
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(telegram_inbox.telegram_relay_queue_tasks(self.queue_state), [])

    def test_queue_waits_while_a_codex_turn_is_active(self) -> None:
        telegram_inbox.enqueue_telegram_relay(
            self.queue_state,
            self.update(31, "wait for idle"),
            self.args.target_pane,
        )
        with (
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=(self.session, 0),
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_turn_active", return_value=True
            ),
            mock.patch.object(telegram_inbox, "handle_update") as handle,
        ):
            delivered = telegram_inbox.drain_telegram_relay_queue(
                self.args, {}, "token", "123", self.log_path
            )

        self.assertEqual(delivered, [])
        handle.assert_not_called()
        self.assertEqual(
            [
                task["message_id"]
                for task in telegram_inbox.telegram_relay_queue_tasks(
                    self.queue_state
                )
            ],
            [31],
        )

    def test_queue_recovers_a_delivery_confirmed_before_listener_restart(self) -> None:
        telegram_inbox.enqueue_telegram_relay(
            self.queue_state,
            self.update(32, "already delivered"),
            self.args.target_pane,
        )
        marker = "[TELEGRAM USER MESSAGE message_id=32"
        self.session.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": marker}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        state = telegram_inbox.read_json_object(self.queue_state)
        state["tasks"][0].update(
            {
                "status": "delivering",
                "marker": marker,
                "delivery_session_path": str(self.session),
                "delivery_session_offset": 0,
            }
        )
        telegram_inbox.write_json_object(self.queue_state, state)

        with mock.patch.object(telegram_inbox, "handle_update") as handle:
            events = telegram_inbox.drain_telegram_relay_queue(
                self.args, {}, "token", "123", self.log_path
            )

        handle.assert_not_called()
        self.assertEqual(events[0]["event"], "telegram_relay_queue_delivery_recovered")
        self.assertEqual(
            telegram_inbox.telegram_relay_queue_tasks(self.queue_state), []
        )

    def test_interrupt_aborts_before_replacing_prompt(self) -> None:
        self.seed_pending(message_id=40)
        checkpoint = (self.session, 0)
        with (
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=True),
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=checkpoint
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_turn_active", return_value=True
            ),
            mock.patch.object(
                telegram_inbox,
                "wait_for_codex_turn_terminal",
                return_value="turn_aborted",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
            mock.patch.object(
                telegram_inbox,
                "paste_to_tmux",
                return_value="relayed to tele-agent:codex.0 (submission confirmed)",
            ) as paste,
        ):
            result, state = telegram_inbox.interrupt_codex_with_prompt(
                self.args.target_pane,
                "urgent replacement",
                submit_delay=0,
                pending_state_path=self.confirmation_state,
            )

        send_keys.assert_called_once_with(self.args.target_pane, "Escape")
        paste.assert_called_once_with(
            self.args.target_pane,
            "urgent replacement",
            press_enter=True,
            allow_shell_pane=False,
            submit_delay=0,
            pending_state_path=self.confirmation_state,
        )
        self.assertTrue(result.startswith("relayed to "))
        self.assertTrue(state["was_active"])
        self.assertEqual(state["terminal_event"], "turn_aborted")
        self.assertEqual(state["cancelled_pending_message_ids"], [40])
        self.assertEqual(
            telegram_inbox.read_json_object(self.confirmation_state)["pending"], []
        )

    def test_interrupt_timeout_does_not_paste_or_cancel_pending_input(self) -> None:
        self.seed_pending(message_id=50)
        with (
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=True),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=(self.session, 0),
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_turn_active", return_value=True
            ),
            mock.patch.object(
                telegram_inbox, "wait_for_codex_turn_terminal", return_value=None
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys"),
            mock.patch.object(telegram_inbox, "paste_to_tmux") as paste,
        ):
            with self.assertRaisesRegex(RuntimeError, "confirm the interrupt"):
                telegram_inbox.interrupt_codex_with_prompt(
                    self.args.target_pane,
                    "must not be pasted",
                    submit_delay=0,
                    pending_state_path=self.confirmation_state,
                    timeout=0,
                )

        paste.assert_not_called()
        pending = telegram_inbox.read_json_object(self.confirmation_state)["pending"]
        self.assertEqual([item["message_id"] for item in pending], [50])

    def test_interrupt_command_relays_only_its_prompt_payload(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"),
            codex_auth_state_path=str(self.root / "auth.json"),
            codex_reauth_state_path=str(self.root / "reauth.json"),
            max_log_chars=4000,
            relay_confirmation_state_path=str(self.confirmation_state),
            relay_mode="tmux-enter",
            submit_delay=0,
            target_pane=self.args.target_pane,
        )
        update = self.update(61, "/interrupt do the urgent task")
        with (
            mock.patch.object(
                telegram_inbox,
                "interrupt_codex_with_prompt",
                return_value=(
                    "relayed to tele-agent:codex.0 (submission confirmed)",
                    {
                        "cancelled_pending_message_ids": [],
                        "terminal_event": "turn_aborted",
                        "was_active": True,
                    },
                ),
            ) as interrupt,
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=None,
            ),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.log_path
            )

        relay_text = interrupt.call_args.args[1]
        self.assertIn("[TELEGRAM USER MESSAGE message_id=61", relay_text)
        self.assertIn("do the urgent task", relay_text)
        self.assertNotIn("/interrupt do the urgent task", relay_text)
        send_reply.assert_not_called()
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "interrupt")


if __name__ == "__main__":
    unittest.main()
