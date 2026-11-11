"""Checking and updating each provider's CLI from Telegram."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from teleagent import ui, updates  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_newer_compares_numbers_and_puts_a_pre_release_before_its_release(self):
        self.assertTrue(updates.newer("0.167.1", "0.166.0"))
        self.assertTrue(updates.newer("2.1.322", "2.1.99"))
        self.assertTrue(updates.newer("0.2.2-rc.1", "0.2.0-rc.2"))
        self.assertTrue(updates.newer("0.2.2", "0.2.2-rc.1"))
        self.assertTrue(updates.newer("0.2.2-rc.10", "0.2.2-rc.2"))
        self.assertFalse(updates.newer("1.3.5", "1.3.5"))
        self.assertFalse(updates.newer("1.3.5", "1.5.5"))
        # An unknown side never claims an update.
        self.assertFalse(updates.newer("", "1.0.0"))
        self.assertFalse(updates.newer("1.0.0", ""))

    def test_installed_reads_the_version_out_of_whatever_the_cli_prints(self):
        for printed, expected in [("codex-cli 0.166.0\n", "0.166.0"), ("2.1.322 (Claude Code)\n", "2.1.322"),
                                  ("0.2.1-rc.2\n", "0.2.1-rc.2"), ("no version here", "")]:
            completed = mock.Mock(stdout=printed, stderr="")
            with mock.patch.object(updates, "binary", return_value="/bin/cli"), \
                 mock.patch.object(updates, "_run", return_value=completed):
                self.assertEqual(updates.installed("codex"), expected)
        with mock.patch.object(updates, "binary", return_value=None):
            self.assertEqual(updates.installed("codex"), "")


class ReportTests(unittest.TestCase):
    def test_report_offers_a_button_only_where_there_is_something_to_do(self):
        found = [("codex", "0.166.0", "0.167.1"), ("claude", "2.1.322", "2.1.322"),
                 ("dsh", "", "0.2.2-rc.1"), ("agy", "1.3.5", "")]
        with mock.patch.object(updates._dsh, "npm", return_value="/usr/bin/npm"):
            text, rows = updates.report(found)
        self.assertEqual(text.splitlines(), [
            "Codex: 0.166.0 → 0.167.1 available",
            "Claude Code: 2.1.322, up to date",
            "DeepSeek Harness: not installed",
            "Antigravity: 1.3.5",
        ])
        self.assertEqual(rows, [[("Update Codex", "update:codex")],
                                [("Install DeepSeek Harness", "update:dsh")],
                                [("Update Antigravity", "update:agy")],
                                [("Update all", "update:all")]])
        text, rows = updates.report([("claude", "2.1.322", "2.1.322")])
        self.assertEqual(rows, [])

    def test_update_reports_the_version_the_cli_has_afterwards(self):
        versions = iter(["0.166.0", "0.167.1"])
        with mock.patch.object(updates, "installed", side_effect=lambda key: next(versions)), \
             mock.patch.object(updates, "binary", return_value="/bin/codex"), \
             mock.patch.object(updates, "_run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(updates.update("codex", Path(".")), "Codex: 0.166.0 → 0.167.1.")
        self.assertEqual(run.call_args.args[0], ["/bin/codex", "update"])
        with mock.patch.object(updates, "installed", return_value="1.0.0"), \
             mock.patch.object(updates, "binary", return_value="/bin/agy"), \
             mock.patch.object(updates, "_run", return_value=mock.Mock(returncode=3)):
            self.assertEqual(updates.update("agy", Path(".")),
                             "Antigravity: the updater failed (exit 3); still 1.0.0.")
        with mock.patch.object(updates, "installed", return_value=""), \
             mock.patch.object(updates, "binary", return_value=None):
            self.assertEqual(updates.update("claude", Path(".")), "Claude Code: not installed on this machine.")

    def test_daily_notice_speaks_once_per_new_version(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "updates.json"
        found = [("codex", "0.166.0", "0.167.1"), ("claude", "2.1.322", "2.1.322")]
        with mock.patch.object(updates, "survey", return_value=found) as survey:
            text, rows = updates.daily_notice(path, now=1e9)
            self.assertIn("Codex: 0.166.0 → 0.167.1 available", text)
            self.assertNotIn("Claude", text)
            self.assertEqual(rows, [[("Update Codex", "update:codex")]])
            # Not asked again within a day, and not repeated for the same version later.
            self.assertIsNone(updates.daily_notice(path, now=1e9 + 1000))
            self.assertEqual(survey.call_count, 1)
            self.assertIsNone(updates.daily_notice(path, now=1e9 + updates.CHECK_INTERVAL + 1))
        newer = [("codex", "0.166.0", "0.168.0")]
        with mock.patch.object(updates, "survey", return_value=newer):
            self.assertIsNotNone(updates.daily_notice(path, now=1e9 + 3 * updates.CHECK_INTERVAL))


class UpdateButtonTests(unittest.TestCase):
    def tap(self, data, sender=123):
        return {"update_id": 9, "callback_query": {
            "id": "cb", "data": data, "from": {"id": sender},
            "message": {"message_id": 5, "chat": {"id": sender, "type": "private"}, "text": "Codex: ..."}}}

    def test_owner_tap_updates_in_the_background_and_reports(self):
        args = argparse.Namespace(repo_root=".")
        finished = threading.Event()
        with mock.patch.object(ui.transport, "telegram_api") as api, \
             mock.patch.object(ui.replies, "send", side_effect=lambda *a, **k: finished.set()) as send, \
             mock.patch.object(updates, "binary", return_value="/bin/cli"), \
             mock.patch.object(updates, "update", side_effect=lambda key, root: f"{key} done"):
            self.assertTrue(ui.handle_quick_update(self.tap("update:all"), args, "token", "123", "123", "OurBot", {}))
            self.assertTrue(finished.wait(10))
            edit = api.call_args_list[-1].args[2]
            self.assertIn("Updating Codex, Claude Code, DeepSeek Harness, Antigravity", edit["text"])
            self.assertIn("codex done\nclaude done\ndsh done\nagy done", send.call_args.args[3])
            self.assertIn("until it is restarted", send.call_args.args[3])

    def test_someone_else_cannot_update(self):
        args = argparse.Namespace(repo_root=".")
        with mock.patch.object(ui.transport, "telegram_api"), \
             mock.patch.object(ui.replies, "send") as send, \
             mock.patch.object(updates, "run_in_background") as run:
            self.assertTrue(ui.handle_quick_update(
                self.tap("update:all", sender=999), args, "token", "123", "123", "OurBot", {}))
            run.assert_not_called()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
