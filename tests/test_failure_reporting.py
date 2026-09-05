from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from teleagent import (
    auth,
    commands,
    delivery,
    lifecycle,
    models,
    processes,
    replies,
    service,
    state,
    status,
    transport,
    ui,
)


class FailureReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            session="fixture",
            codex_window="codex",
            target_pane="fixture:codex.0",
            repo_root=self.root,
            tmux_lines=20,
            max_log_chars=1000,
            health_state_path=str(self.root / "health.json"),
            relay_queue_state_path=str(self.root / "queue.json"),
            control_reply_state_path=str(self.root / "replies.json"),
        )

    @staticmethod
    def update(text="hello"):
        return {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "chat": {"id": "123", "type": "private"},
                "from": {"id": 123},
                "text": text,
            },
        }

    def test_corrupt_state_is_preserved_instead_of_overwritten_as_empty(self):
        path = self.root / "inbox.json"
        for original in ('{"pending": [', "[]", ""):
            with self.subTest(original=original):
                path.write_text(original)
                with self.assertRaises(state.StateReadError):
                    service.Inbox(path).accept(self.update())
                self.assertEqual(path.read_text(), original)
        cursor = self.root / "cursor"
        cursor.write_text("not a cursor")
        with self.assertRaises(state.StateReadError):
            state.read_offset(cursor)
        self.assertEqual(cursor.read_text(), "not a cursor")

    def test_unreadable_existing_state_is_not_reported_as_empty(self):
        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(state.StateReadError):
                state.read_json_object(self.root / "queue.json")

    def test_a_broken_error_reporter_cannot_repeat_the_command(self):
        inbox = service.Inbox(self.root / "inbox.json")
        inbox.accept(self.update())
        dispatch = mock.Mock(side_effect=RuntimeError("action outcome unknown"))
        worker = service.RelayWorkers(
            inbox,
            dispatch,
            lambda: None,
            lambda: None,
            mock.Mock(side_effect=OSError("reporting failed")),
            Path(self.args.health_state_path),
        )
        with self.assertRaises(OSError):
            worker.control_once()
        self.assertIsNone(inbox.take())
        dispatch.assert_called_once()
        self.assertEqual(len(state.read_json_object(inbox.path)["failed"]), 1)

    def test_an_unfinished_inbox_action_is_never_taken_twice(self):
        inbox = service.Inbox(self.root / "inbox.json")
        inbox.accept(self.update())
        self.assertEqual(inbox.take()["update_id"], 1)
        with self.assertRaisesRegex(RuntimeError, "will not be executed again"):
            inbox.take()

    def test_failed_control_reply_retries_without_repeating_cancel(self):
        state.write_json_object(
            Path(self.args.relay_queue_state_path),
            {
                "tasks": [
                    {
                        "id": "q1",
                        "status": "queued",
                        "update": self.update(),
                        "queued_ts": time.time(),
                    }
                ]
            },
        )
        with mock.patch.object(ui, "cancel_queued", wraps=ui.cancel_queued) as cancel:
            self.assertTrue(
                ui.handle_quick_update(
                    self.update("/cancel all"),
                    self.args,
                    "token",
                    "123",
                    "123",
                    "OurBot",
                    {},
                )
            )
            with mock.patch.object(
                transport, "send_reply", side_effect=RuntimeError("network down")
            ):
                with self.assertRaises(replies.DeliveryError):
                    replies.drain(self.args, "token", now=10)
            saved = state.read_json_object(Path(self.args.control_reply_state_path))[
                "pending"
            ]
            self.assertEqual(len(saved), 1)
            with mock.patch.object(transport, "send_reply") as send:
                replies.drain(self.args, "token", now=100)
            self.assertIn("1", send.call_args.args[2])
            self.assertIn("Removed", send.call_args.args[2])
            cancel.assert_called_once()
        self.assertEqual(
            state.read_json_object(Path(self.args.control_reply_state_path))["pending"],
            [],
        )

    def test_a_blocked_group_reply_does_not_block_private_controls(self):
        replies.send(self.args, "token", "-99", "group result", message_thread_id=7)
        replies.send(self.args, "token", "123", "private status")

        def sender(_token, chat_id, _text, **_options):
            if chat_id == "-99":
                raise RuntimeError("bot removed from group")

        with mock.patch.object(transport, "send_reply", side_effect=sender) as send:
            with self.assertRaises(replies.DeliveryError):
                replies.drain(self.args, "token", now=10)
        self.assertEqual([call.args[1] for call in send.call_args_list], ["-99", "123"])
        pending = state.read_json_object(Path(self.args.control_reply_state_path))[
            "pending"
        ]
        self.assertEqual(
            [
                (item["chat_id"], item["options"].get("message_thread_id"))
                for item in pending
            ],
            [("-99", 7)],
        )
        with mock.patch.object(transport, "send_reply") as send:
            with self.assertRaises(replies.DeliveryError):
                replies.drain(self.args, "token", now=11)
            send.assert_not_called()
            replies.drain(self.args, "token", now=20)
            send.assert_called_once_with(
                "token", "-99", "group result", message_thread_id=7
            )

    def test_expired_callback_acknowledgement_still_delivers_refresh(self):
        update = {
            "update_id": 1,
            "callback_query": {
                "id": "old",
                "data": "relay:status",
                "from": {"id": 123},
                "message": self.update()["message"],
            },
        }
        with (
            mock.patch.object(
                transport, "telegram_api", side_effect=RuntimeError("query is too old")
            ),
            mock.patch.object(
                status, "format_system_status", return_value="current status"
            ),
        ):
            self.assertTrue(
                ui.handle_quick_update(
                    update, self.args, "token", "123", "123", "OurBot", {}
                )
            )
        with mock.patch.object(transport, "send_reply") as send:
            replies.drain(self.args, "token")
        self.assertEqual(send.call_args.args[2], "current status")

    def test_partial_control_reply_retry_does_not_repeat_completed_chunks(self):
        source = "x" * 6000
        replies.send(self.args, "token", "123", source)
        with mock.patch.object(
            transport, "send_reply", side_effect=[None, RuntimeError("offline")]
        ) as first:
            with self.assertRaises(replies.DeliveryError):
                replies.drain(self.args, "token", now=10)
        with mock.patch.object(transport, "send_reply") as retry:
            replies.drain(self.args, "token", now=20)
        self.assertEqual(
            first.call_args_list[0].args[2] + retry.call_args.args[2], source
        )
        retry.assert_called_once()

    def test_delivery_health_stays_failed_until_a_successful_check(self):
        path = Path(self.args.health_state_path)
        worker = service.RelayWorkers(
            service.Inbox(self.root / "inbox.json"),
            lambda _: None,
            lambda: None,
            lambda: None,
            mock.Mock(),
            path,
        )
        calls = 0

        def attempt():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise replies.DeliveryError("send failed")
            failed = state.read_json_object(path)
            self.assertEqual(failed["delivery_error"], "DeliveryError")
            self.assertNotIn("delivery_ok_ts", failed)
            worker.stop.set()

        worker._run("delivery", attempt, 0)
        recovered = state.read_json_object(path)
        self.assertIsNone(recovered["delivery_error"])
        self.assertIn("delivery_ok_ts", recovered)

    def test_partial_notification_line_waits_for_completion(self):
        outbox, cursor = self.root / "outbox.jsonl", self.root / "cursor"
        original = json.dumps({"text": "complete result", "phase": "final"})
        outbox.write_text(original[:-2])
        with mock.patch.object(transport, "send_reply") as send:
            delivery.drain_agent_outbox("token", "123", outbox, cursor)
            send.assert_not_called()
            self.assertEqual(state.read_offset(cursor), 0)
            outbox.write_text(original + "\n")
            delivery.drain_agent_outbox("token", "123", outbox, cursor)
            send.assert_called_once()

    def test_failed_notification_retains_cursor_and_reports_failure(self):
        outbox, cursor = self.root / "outbox.jsonl", self.root / "cursor"
        outbox.write_text(json.dumps({"text": "result", "phase": "final"}) + "\n")
        with mock.patch.object(
            transport, "send_reply", side_effect=RuntimeError("offline")
        ):
            with self.assertRaises(replies.DeliveryError):
                delivery.drain_agent_outbox("token", "123", outbox, cursor)
        self.assertEqual(state.read_offset(cursor), 0)

    def test_agent_send_failure_is_visible_and_keeps_the_unsent_result(self):
        session = self.root / "session.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "result",
                    },
                }
            )
            + "\n"
        )
        path = self.root / "messages.json"
        meta = {"agent_id": "fixture", "codex_session_path": str(session)}
        state.write_json_object(
            path,
            {
                "agent_id": "fixture",
                "session_path": str(session.resolve()),
                "offset": 0,
            },
        )
        with (
            mock.patch.object(
                delivery._sessions, "valid_codex_session_for_agent", return_value=True
            ),
            mock.patch.object(
                transport, "send_reply", side_effect=RuntimeError("offline")
            ),
        ):
            with self.assertRaises(replies.DeliveryError):
                delivery.drain_codex_agent_messages(
                    "token", "123", meta, path, None, {}
                )
        self.assertEqual(state.read_json_object(path)["offset"], 0)
        with (
            mock.patch.object(
                delivery._sessions, "valid_codex_session_for_agent", return_value=True
            ),
            mock.patch.object(transport, "send_reply") as send,
        ):
            self.assertEqual(
                delivery.drain_codex_agent_messages(
                    "token", "123", meta, path, None, {}
                ),
                1,
            )
        self.assertIn("result", send.call_args.args[2])

    def test_watchdog_failure_remains_visible_during_retry_backoff(self):
        self.args.agent_watchdog = True
        self.args.relay_mode = "tmux-enter"
        with (
            mock.patch.object(
                lifecycle, "agent_lifecycle_operation_in_progress", return_value=False
            ),
            mock.patch.object(
                processes, "registered_codex_process_running", return_value=False
            ),
            mock.patch.object(processes, "tmux_target_exists", return_value=False),
            mock.patch.object(
                lifecycle,
                "start_codex_agent",
                side_effect=RuntimeError("launch failed"),
            ) as start,
        ):
            for _ in range(2):
                with self.assertRaises(service.MaintenanceError):
                    lifecycle.maintain_managed_codex_agent(
                        self.args, self.root / "log.jsonl"
                    )
            start.assert_called_once()

    def test_start_never_claims_success_without_observing_codex(self):
        with (
            mock.patch.object(processes, "ensure_tmux_session"),
            mock.patch.object(processes, "tmux_window_exists", return_value=False),
            mock.patch.object(
                processes, "codex_home_for_model", return_value=self.root
            ),
            mock.patch.object(
                processes, "tmux_bash_shell_command", return_value="bash"
            ),
            mock.patch.object(processes, "codex_executable", return_value="codex"),
            mock.patch.object(
                processes, "tmux_pane_has_codex_process", return_value=False
            ),
            mock.patch.object(lifecycle.subprocess, "run"),
            mock.patch.object(lifecycle.time, "sleep"),
            mock.patch.object(
                lifecycle.agent_registry, "create_agent", return_value={}
            ),
            mock.patch.object(
                lifecycle.agent_registry, "shell_env_prefix", return_value=""
            ),
        ):
            with self.assertRaisesRegex(
                lifecycle.AgentStartUnconfirmed, "no Codex process"
            ):
                lifecycle.start_codex_agent(self.root, "fixture", "codex")

    def test_reset_redemption_is_preserved_when_only_restart_fails(self):
        reset_path = self.root / "reset.json"
        self.args.codex_reset_state_path = str(reset_path)
        self.args.codex_usage_state_path = str(self.root / "usage.json")
        state.write_json_object(
            reset_path,
            {
                "phase": "awaiting_confirmation",
                "expires_ts": time.time() + 300,
                "chat_id": "123",
                "sender_id": "123",
            },
        )
        with (
            mock.patch.object(
                auth, "run_codex_reset_helper", return_value=(0, "RESET_SUCCESS\n", "")
            ) as redeem,
            mock.patch.object(
                lifecycle,
                "start_codex_agent",
                side_effect=RuntimeError("restart timed out"),
            ),
        ):
            result = commands.handle_update(
                self.update("/confirm"),
                self.args,
                {},
                "token",
                "123",
                self.root / "log.jsonl",
            )
            commands.handle_update(
                self.update("/confirm"),
                self.args,
                {},
                "token",
                "123",
                self.root / "log.jsonl",
            )
            redeem.assert_called_once()
        saved = state.read_json_object(reset_path)
        self.assertTrue(saved["redeemed"])
        self.assertEqual(saved["phase"], "redeemed_followup_failed")
        self.assertEqual(result["action"], "codex_reset_followup_failed")
        texts = [
            item["text"]
            for item in state.read_json_object(
                Path(self.args.control_reply_state_path)
            )["pending"]
        ]
        self.assertTrue(
            any(
                "reset was redeemed" in text and "restart the agent" in text
                for text in texts
            )
        )
        self.assertFalse(any("failed safely" in text for text in texts))

    def test_status_distinguishes_failed_delivery_from_a_fresh_heartbeat(self):
        state.write_json_object(
            Path(self.args.health_state_path),
            {"delivery_ok_ts": time.time(), "delivery_error": "DeliveryError"},
        )
        with (
            mock.patch.object(processes, "tmux_target_exists", return_value=False),
            mock.patch.object(
                status.agent_registry, "active_agent_for_pane", return_value=None
            ),
            mock.patch.object(
                status.agent_registry,
                "codex_session_for_pane",
                return_value=(None, None),
            ),
            mock.patch.object(
                models, "current_codex_model_and_reasoning_effort", return_value=None
            ),
        ):
            rendered = status.format_system_status(
                "fixture", "fixture:codex.0", 20, self.args
            )
        self.assertIn("reply delivery: failing", rendered)


if __name__ == "__main__":
    unittest.main()
