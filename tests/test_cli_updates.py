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
             mock.patch.object(updates, "_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            self.assertEqual(updates.update("codex", Path(".")), "Codex: 0.166.0 → 0.167.1.")
        self.assertEqual(run.call_args.args[0], ["/bin/codex", "update"])
        with mock.patch.object(updates, "installed", return_value="1.0.0"), \
             mock.patch.object(updates, "binary", return_value="/bin/agy"), \
             mock.patch.object(updates, "_run", return_value=mock.Mock(
                 returncode=3, stdout="working\n", stderr="Error: no network\n")):
            self.assertEqual(updates.update("agy", Path(".")),
                             "Antigravity: its updater failed; still 1.0.0. Its last line: Error: no network")
        with mock.patch.object(updates, "installed", return_value=""), \
             mock.patch.object(updates, "binary", return_value=None):
            self.assertEqual(updates.update("claude", Path(".")), "Claude Code: not installed on this machine.")

    def test_status_lists_installed_clis_and_marks_a_newer_version(self):
        found = [("codex", "0.166.0", "0.167.1"), ("claude", "2.1.322", "2.1.322"),
                 ("dsh", "", "0.2.2-rc.1"), ("agy", "1.3.5", "")]
        self.assertEqual(updates.status_text(found).splitlines(), [
            "CLI versions", "Codex 0.166.0 (0.167.1 available)", "Claude Code 2.1.322", "Antigravity 1.3.5"])
        self.assertEqual(updates.status_text([("dsh", "", "1.0.0")]), "")

    def test_npm_update_goes_to_the_install_the_command_belongs_to(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fresh = root / "home" / "cli"
        # Nothing installed: a first install under the harness's own folder.
        self.assertEqual(updates._npm_update("npm", None, fresh)[:4], ["npm", "install", "--prefix", str(fresh)])
        # A command beside its own node_modules: that folder is the global prefix.
        beside = root / "bin"
        (beside / "node_modules" / "@deepseek-ai" / "dsh").mkdir(parents=True)
        (beside / "dsh").write_text("", encoding="utf-8")
        self.assertEqual(updates._npm_update("npm", str(beside / "dsh"), fresh)[:5],
                         ["npm", "install", "-g", "--prefix", str(beside)])
        # A project install is updated in place.
        local = root / "project" / "node_modules" / ".bin"
        local.mkdir(parents=True)
        self.assertEqual(updates._npm_update("npm", str(local / "dsh"), fresh)[:4],
                         ["npm", "install", "--prefix", str(root / "project")])


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

    def test_an_update_cut_off_by_a_restart_is_still_answered(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = updates.marker_path(str(directory / "telegram_inbox.health.json"))
        release, finished = threading.Event(), threading.Event()
        with mock.patch.object(updates, "update_many", side_effect=lambda keys, root: release.wait(10) and "done"):
            self.assertTrue(updates.run_in_background(
                ["codex"], Path("."), lambda text: finished.set(), marker=marker,
                asked={"chat_id": "123", "topic": None}))
            # While it runs, a restarted listener would find who asked.
            self.assertTrue(marker.exists())
            self.assertFalse(updates.run_in_background(["codex"], Path("."), lambda text: None))
            release.set()
            self.assertTrue(finished.wait(10))
        for _ in range(100):
            if not marker.exists():
                break
            threading.Event().wait(0.05)
        self.assertIsNone(updates.interrupted(marker))
        marker.write_text('{"chat_id": "123", "topic": 7}', encoding="utf-8")
        self.assertEqual(updates.interrupted(marker), {"chat_id": "123", "topic": 7})
        self.assertFalse(marker.exists())
        self.assertIsNone(updates.marker_path(""))

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
