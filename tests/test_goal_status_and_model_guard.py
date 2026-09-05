from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from teleagent import processes, status, submission


POPUP = """Approaching rate limits
Switch to gpt-5.6-luna for lower credit usage?
› 1. Switch to gpt-5.6-luna
  2. Keep current model
  3. Keep current model (never show again)
Press enter to confirm or esc to go back
"""


class GoalStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.session = self.home / "sessions" / "2026" / "09" / "05" / "rollout.jsonl"
        self.session.parent.mkdir(parents=True)
        self.session.write_text(json.dumps({
            "type": "session_meta", "payload": {"id": "bound-thread"},
        }) + "\n")
        self.db = sqlite3.connect(self.home / "goals_1.sqlite")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, status TEXT)")
        self.db.commit()

    def test_reports_every_persistent_state_for_bound_thread(self):
        self.db.execute("INSERT INTO thread_goals VALUES ('other-thread', 'blocked')")
        for value in ("active", "paused", "blocked", "usage_limited", "budget_limited", "complete"):
            with self.subTest(value=value):
                self.db.execute("INSERT OR REPLACE INTO thread_goals VALUES (?, ?)", ("bound-thread", value))
                self.db.commit()
                self.assertEqual(status.codex_session_goal_status(self.session), value)

    def test_no_goal_is_off_even_if_another_thread_has_one(self):
        self.db.execute("INSERT INTO thread_goals VALUES ('other-thread', 'active')")
        self.db.commit()
        self.assertEqual(status.codex_session_goal_status(self.session), "off")

    def test_unavailable_state_is_unknown_and_does_not_create_database(self):
        self.assertEqual(status.codex_session_goal_status(None), "unknown")
        self.assertEqual(status.codex_session_goal_status(self.home / "missing"), "unknown")
        private = self.home / "private" / "sessions" / "rollout.jsonl"
        private.parent.mkdir(parents=True)
        private.write_text(self.session.read_text())
        self.assertEqual(status.codex_session_goal_status(private), "unknown")
        self.assertFalse((private.parent.parent / "goals_1.sqlite").exists())

    def test_terminal_prose_cannot_override_persistent_state(self):
        with self.session.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {
                "type": "agent_message", "message": "Goal active /goal resume",
            }}) + "\n")
        self.assertEqual(status.codex_session_goal_status(self.session), "off")


class ModelSwitchGuardTests(unittest.TestCase):
    def test_quota_popup_receives_no_paste_or_keys(self):
        with (
            mock.patch.object(processes, "registered_codex_process_running", return_value=True),
            mock.patch.object(processes, "tmux_tail", return_value=POPUP),
            mock.patch.object(submission, "tmux_paste_text_atomic") as paste,
            mock.patch.object(submission.subprocess, "run") as run,
        ):
            result = submission.paste_to_tmux("fixture:0.0", "hello", True, False, 0)
        self.assertIn("automatic selection blocked", result)
        paste.assert_not_called()
        run.assert_not_called()

    def test_popup_appearing_after_paste_receives_no_enter(self):
        with (
            mock.patch.object(processes, "registered_codex_process_running", return_value=True),
            mock.patch.object(processes, "tmux_tail", side_effect=["› Ask Codex to do anything", POPUP]),
            mock.patch.object(submission, "tmux_paste_text_atomic"),
            mock.patch.object(submission.subprocess, "run") as run,
        ):
            result = submission.paste_to_tmux("fixture:0.0", "hello", True, False, 0)
        self.assertIn("automatic selection blocked", result)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][-1], "C-u")

    def test_recovery_enter_is_blocked_at_send_boundary(self):
        with (
            mock.patch.object(processes, "restore_tmux_socket_from_env"),
            mock.patch.object(processes, "tmux_tail", return_value=POPUP),
            mock.patch.object(processes.subprocess, "run") as run,
        ):
            with self.assertRaises(subprocess.SubprocessError):
                processes.tmux_send_keys("fixture:0.0", "Enter")
        run.assert_not_called()

    def test_ordinary_composer_and_explicit_model_picker_are_not_quota_popup(self):
        for text in ("› Ask Codex to do anything", "Select model\n› gpt-5.6-luna\nPress enter to confirm"):
            with mock.patch.object(processes, "tmux_tail", return_value=text):
                self.assertFalse(submission.codex_model_switch_prompt_visible("fixture:0.0"))


if __name__ == "__main__":
    unittest.main()
