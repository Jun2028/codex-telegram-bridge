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

import codex_device_auth  # noqa: E402
import telegram_inbox  # noqa: E402


class TelegramAuthRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.sessions = self.root / "sessions"
        self.repo.mkdir()
        self.sessions.mkdir()
        self.session = self.sessions / "rollout.jsonl"
        self.auth_state = self.root / "auth.state.json"
        self.reauth_state = self.root / "reauth.state.json"
        self.usage_state = self.root / "usage.state.json"
        self.reset_state = self.root / "reset.state.json"
        self.lifecycle_state = self.root / "lifecycle.state.json"
        self.log_path = self.root / "listener.jsonl"
        self.meta = {
            "agent_id": "agent-test",
            "repo_root": str(self.repo),
            "codex_session_path": str(self.session),
            "codex_session_detection": "process_fd",
        }
        self._write_records(
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T00:00:00Z",
                "payload": {"cwd": str(self.repo)},
            }
        )

    def _write_records(self, *records: dict) -> None:
        with self.session.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def _auth_error(message: str | None = None, code: str = "unauthorized") -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {
                    "message": message
                    or (
                        "Your access token could not be refreshed because your refresh "
                        "token was revoked. Please log out and sign in again."
                    ),
                    "codex_error_info": code,
                },
            },
        }

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            codex_usage_state_path=str(self.usage_state),
            codex_reset_state_path=str(self.reset_state),
            codex_auth_state_path=str(self.auth_state),
            codex_reauth_state_path=str(self.reauth_state),
            agent_lifecycle_state_path=str(self.lifecycle_state),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
            tmux_lines=20,
        )

    def _main_argv(self) -> list[str]:
        return [
            "telegram_inbox.py",
            "--repo-root",
            str(self.repo),
            "--target-pane",
            "tele-agent:codex.0",
            "--process-existing",
            "--once",
            "--state-file",
            str(self.root / "offset"),
            "--log-jsonl",
            str(self.log_path),
            "--inbound-documents-dir",
            str(self.root / "documents"),
            "--agent-outbox",
            str(self.root / "outbox.jsonl"),
            "--agent-outbox-offset",
            str(self.root / "outbox.offset"),
            "--agent-message-state",
            str(self.root / "messages.state.json"),
            "--codex-usage-state",
            str(self.usage_state),
            "--codex-auth-state",
            str(self.auth_state),
            "--codex-reauth-state",
            str(self.reauth_state),
            "--codex-reset-state",
            str(self.reset_state),
            "--agent-lifecycle-state",
            str(self.lifecycle_state),
            "--relay-confirmation-state",
            str(self.root / "confirmation.state.json"),
            "--timed-message-state",
            str(self.root / "timed.state.json"),
        ]

    def test_structured_revoked_refresh_token_sets_persistent_block(self) -> None:
        self._write_records(self._auth_error())

        state = telegram_inbox.refresh_codex_auth_state(
            self.meta,
            self.auth_state,
            sessions_root=self.sessions,
            now=1_900_000_000,
        )

        self.assertTrue(state["blocked"])
        self.assertTrue(state["alert_pending"])
        self.assertEqual(state["reason"], "refresh_token_revoked")
        self.assertEqual(state["error_code"], "unauthorized")

    def test_auth_detection_ignores_prompt_text_and_unrelated_unauthorized_error(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": (
                        "Your access token could not be refreshed because your refresh "
                        "token was revoked. Please log out and sign in again."
                    ),
                },
            },
            self._auth_error("A repository request was unauthorized."),
        )

        state = telegram_inbox.refresh_codex_auth_state(
            self.meta,
            self.auth_state,
            sessions_root=self.sessions,
            now=1_900_000_000,
        )

        self.assertFalse(state.get("blocked", False))

    def test_cleared_error_is_not_rediscovered_without_a_new_event(self) -> None:
        self._write_records(self._auth_error())
        telegram_inbox.refresh_codex_auth_state(
            self.meta,
            self.auth_state,
            sessions_root=self.sessions,
            now=1_900_000_000,
        )
        telegram_inbox.clear_codex_auth_failure(self.auth_state, "test_reauth")

        state = telegram_inbox.refresh_codex_auth_state(
            self.meta,
            self.auth_state,
            sessions_root=self.sessions,
            now=1_900_000_100,
        )

        self.assertFalse(state["blocked"])
        self.assertEqual(state["clear_reason"], "test_reauth")

    def test_blocked_normal_message_gets_mechanical_reply_without_relay(self) -> None:
        telegram_inbox.write_json_object(
            self.auth_state,
            {"blocked": True, "reason": "refresh_token_revoked"},
        )
        update = {
            "update_id": 40,
            "message": {
                "message_id": 41,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "are you there?",
            },
        }
        with (
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(telegram_inbox, "paste_to_tmux") as paste,
            mock.patch.object(
                telegram_inbox, "ensure_codex_target_for_agent_message"
            ) as ensure_target,
        ):
            telegram_inbox.handle_update(
                update, self._args(), {}, "token", "123", self.log_path
            )

        paste.assert_not_called()
        ensure_target.assert_not_called()
        outgoing = send_reply.call_args.args[2]
        self.assertIn("refresh credential was revoked", outgoing)
        self.assertIn("/reauth", outgoing)
        self.assertIn("/agent_status", outgoing)
        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "auth_failure_fallback")

    def test_agent_status_includes_auth_and_recovery_diagnostics(self) -> None:
        telegram_inbox.write_json_object(
            self.auth_state,
            {
                "blocked": True,
                "reason": "refresh_token_revoked",
                "detected_ts": 1_900_000_000,
            },
        )
        telegram_inbox.write_json_object(
            self.reauth_state,
            {"phase": "failed", "error": "device auth timed out"},
        )
        update = {
            "update_id": 42,
            "message": {
                "message_id": 43,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/agent_status",
            },
        }
        with (
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=True),
            mock.patch.object(
                telegram_inbox.agent_registry, "active_agent_for_pane", return_value=None
            ),
            mock.patch.object(
                telegram_inbox, "codex_login_status_summary", return_value="Logged in using ChatGPT"
            ),
        ):
            telegram_inbox.handle_update(
                update, self._args(), {}, "token", "123", self.log_path
            )

        outgoing = send_reply.call_args.args[2]
        self.assertIn("Codex auth: REAUTH REQUIRED", outgoing)
        self.assertIn("credential storage: Logged in using ChatGPT (presence check only)", outgoing)
        self.assertIn("reauth flow: failed", outgoing)

    def test_reauth_command_starts_fixed_device_flow_without_logging_code(self) -> None:
        update = {
            "update_id": 44,
            "message": {
                "message_id": 45,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/reauth",
            },
        }
        returned = {
            "attempt_id": "attempt-1",
            "phase": "awaiting_user",
            "verification_url": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-EFGHI",
            "code_expires_ts": 1_900_000_900,
        }
        with (
            mock.patch.object(
                telegram_inbox, "start_codex_reauth", return_value=returned
            ) as start,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, self._args(), {}, "token", "123", self.log_path
            )

        start.assert_called_once()
        outgoing = send_reply.call_args.args[2]
        self.assertIn("https://auth.openai.com/codex/device", outgoing)
        self.assertIn("ABCD-EFGHI", outgoing)
        logged = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("ABCD-EFGHI", logged)
        self.assertTrue(
            telegram_inbox.read_json_object(self.auth_state).get("blocked")
        )

    def test_device_auth_worker_publishes_code_then_clears_it_on_success(self) -> None:
        attempt_id = "attempt-success"
        telegram_inbox.write_json_object(
            self.reauth_state,
            {"attempt_id": attempt_id, "phase": "starting"},
        )

        class FakeProcess:
            pid = 1234
            stdout = iter(
                [
                    "1. Open this link in your browser\n",
                    " https://auth.openai.com/codex/device\n",
                    "2. Enter this one-time code\n",
                    " ABCD-EFGHI\n",
                    "Successfully logged in\n",
                ]
            )

            @staticmethod
            def wait(timeout=None):
                return 0

            @staticmethod
            def terminate():
                return None

        with mock.patch.object(
            codex_device_auth.subprocess, "Popen", return_value=FakeProcess()
        ):
            returncode = codex_device_auth.run_device_auth(
                self.reauth_state, attempt_id, "codex"
            )

        state = codex_device_auth.read_state(self.reauth_state)
        self.assertEqual(returncode, 0)
        self.assertEqual(state["phase"], "authenticated")
        self.assertIsNone(state["user_code"])
        self.assertIsNone(state["verification_url"])

    def test_device_auth_failure_detail_redacts_ephemeral_code_and_url(self) -> None:
        detail = codex_device_auth.safe_failure_detail(
            [
                "https://auth.openai.com/codex/device\n",
                "ABCD-EFGHI\n",
                "Error logging in with device code: device auth timed out\n",
            ],
            "https://auth.openai.com/codex/device",
            "ABCD-EFGHI",
        )

        self.assertNotIn("ABCD-EFGHI", detail)
        self.assertNotIn("https://auth.openai.com", detail)
        self.assertIn("device auth timed out", detail)

    def test_listener_proactively_reports_structured_auth_failure_once(self) -> None:
        self._write_records(self._auth_error())
        with (
            mock.patch.object(sys, "argv", self._main_argv()),
            mock.patch.object(telegram_inbox, "assert_safe_local_path"),
            mock.patch.object(
                telegram_inbox,
                "load_env",
                return_value={
                    "TELEAGENT_BOT_TOKEN": "token",
                    "TELEAGENT_CHAT_ID": "123",
                },
            ),
            mock.patch.object(telegram_inbox, "get_updates", return_value=[]),
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=False),
            mock.patch.object(
                telegram_inbox, "valid_codex_session_for_agent", return_value=True
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=self.meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=self.meta,
            ),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            returncode = telegram_inbox.main()

        self.assertEqual(returncode, 0)
        auth_notices = [
            call.args[2]
            for call in send_reply.call_args_list
            if "Codex authentication failed" in call.args[2]
        ]
        self.assertEqual(len(auth_notices), 1)
        self.assertIn("/agent_status", auth_notices[0])
        self.assertFalse(
            telegram_inbox.read_json_object(self.auth_state)["alert_pending"]
        )

    def test_successful_device_login_restarts_only_agent_and_notifies(self) -> None:
        telegram_inbox.write_json_object(
            self.auth_state,
            {"blocked": True, "reason": "refresh_token_revoked", "offset": self.session.stat().st_size},
        )
        telegram_inbox.write_json_object(
            self.reauth_state,
            {
                "attempt_id": "attempt-success",
                "phase": "authenticated",
                "authenticated_ts": 1_900_000_000,
            },
        )
        new_meta = {"agent_id": "new-agent", "created_ts": 1_900_000_001}
        with (
            mock.patch.object(sys, "argv", self._main_argv()),
            mock.patch.object(telegram_inbox, "assert_safe_local_path"),
            mock.patch.object(
                telegram_inbox,
                "load_env",
                return_value={
                    "TELEAGENT_BOT_TOKEN": "token",
                    "TELEAGENT_CHAT_ID": "123",
                },
            ),
            mock.patch.object(telegram_inbox, "get_updates", return_value=[]),
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=False),
            mock.patch.object(
                telegram_inbox, "valid_codex_session_for_agent", return_value=True
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=self.meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=self.meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "current_codex_reasoning_effort",
                return_value="xhigh",
            ),
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    new_meta,
                ),
            ) as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            returncode = telegram_inbox.main()

        self.assertEqual(returncode, 0)
        start_agent.assert_called_once()
        self.assertTrue(start_agent.call_args.kwargs["restart"])
        self.assertEqual(start_agent.call_args.kwargs["reasoning_effort"], "xhigh")
        self.assertTrue(
            any(
                "sign-in succeeded and the Telegram agent was restarted" in call.args[2]
                for call in send_reply.call_args_list
            )
        )
        self.assertFalse(
            telegram_inbox.read_json_object(self.auth_state)["blocked"]
        )
        self.assertEqual(
            telegram_inbox.read_json_object(self.reauth_state)["phase"], "completed"
        )

    def test_successful_device_login_preserves_intentionally_stopped_agent(self) -> None:
        telegram_inbox.write_json_object(
            self.auth_state,
            {"blocked": True, "reason": "refresh_token_revoked"},
        )
        telegram_inbox.write_json_object(
            self.reauth_state,
            {
                "attempt_id": "attempt-stopped",
                "phase": "authenticated",
                "authenticated_ts": 1_900_000_000,
            },
        )
        telegram_inbox.write_json_object(
            self.lifecycle_state,
            {"desired": "stopped"},
        )
        with (
            mock.patch.object(sys, "argv", self._main_argv()),
            mock.patch.object(telegram_inbox, "assert_safe_local_path"),
            mock.patch.object(
                telegram_inbox,
                "load_env",
                return_value={
                    "TELEAGENT_BOT_TOKEN": "token",
                    "TELEAGENT_CHAT_ID": "123",
                },
            ),
            mock.patch.object(telegram_inbox, "get_updates", return_value=[]),
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=False),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=self.meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=self.meta,
            ),
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            returncode = telegram_inbox.main()

        self.assertEqual(returncode, 0)
        start_agent.assert_not_called()
        self.assertTrue(
            any(
                "agent remains stopped" in call.args[2]
                for call in send_reply.call_args_list
            )
        )
        reauth = telegram_inbox.read_json_object(self.reauth_state)
        self.assertEqual(reauth["phase"], "completed")
        self.assertFalse(reauth["agent_restarted"])


if __name__ == "__main__":
    unittest.main()
