from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from teleagent import replies, status, ui
import setup_telegram_notify as setup


class MiniAppControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url_file = Path(self.temp.name) / "launch-url"
        self.url_file.write_text("https://files.example/?key=private-test-key")
        self.env = {"TELEAGENT_MINI_APP_URL_FILE": str(self.url_file)}
        self.args = argparse.Namespace(session="test", target_pane="test:codex.0", tmux_lines=20)

    def request(self, command="/status", chat="123", sender=123, group=False):
        update = {"message": {
            "message_id": 1,
            "chat": {"id": chat, "type": "supergroup" if group else "private"},
            "from": {"id": sender},
            "text": command + "@OurBot",
        }}
        with (
            mock.patch.object(replies, "send") as send,
            mock.patch.object(status, "format_system_status", return_value="Idle"),
        ):
            self.assertTrue(ui.handle_quick_update(
                update, self.args, "token", "123", "123", "OurBot", {}, env=self.env
            ))
        return send

    def test_files_coexists_with_all_existing_controls(self):
        for command in ("/status", "/help", "/start", "/files"):
            with self.subTest(command=command):
                send = self.request(command)
                rows = send.call_args.kwargs["reply_markup"]["inline_keyboard"]
                self.assertEqual(rows[:-1], ui.BUTTONS["inline_keyboard"])
                self.assertEqual(rows[-1], [{"text": "Files", "web_app": {
                    "url": self.url_file.read_text()}}])
        self.assertEqual(ui.command_menu(self.env)[:-1], ui.command_menu())
        self.assertEqual(ui.command_menu(self.env)[-1]["command"], "files")

    def test_private_then_group_never_reuses_private_url(self):
        self.request()
        for command in ("/status", "/help", "/files"):
            with self.subTest(command=command):
                send = self.request(command, chat="-99", group=True)
                self.assertEqual(send.call_args.kwargs["reply_markup"], ui.BUTTONS)
                self.assertNotIn("private-test-key", str(send.call_args))
        self.assertEqual(len(ui.BUTTONS["inline_keyboard"]), 1)

    def test_other_private_chat_gets_no_response(self):
        self.request("/files", chat="999", sender=999).assert_not_called()

    def test_changed_url_is_used_without_restarting(self):
        self.request("/files")
        self.url_file.write_text("https://new-files.example/?key=new-test-key")
        rows = self.request("/files").call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[-1][0]["web_app"]["url"], self.url_file.read_text())

    def test_missing_or_invalid_url_keeps_normal_controls(self):
        for value in ("", "http://files.example/", "javascript:alert(1)",
                      "https://files.example/\nunsafe", "https://user:pass@files.example/",
                      "https://[broken", None):
            with self.subTest(value=value):
                if value is None:
                    self.url_file.unlink()
                else:
                    self.url_file.write_text(value)
                self.assertEqual(ui.command_menu(self.env), ui.command_menu())
                send = self.request()
                self.assertEqual(send.call_args.kwargs["reply_markup"], ui.BUTTONS)
                self.assertIn("unavailable", self.request("/files").call_args.args[3])

    def test_connector_restores_commands_and_updates_existing_launch(self):
        repo = Path(self.temp.name)
        secret_dir = repo / ".secrets"
        secret_dir.mkdir()
        (secret_dir / "notify.env").write_text("TELEAGENT_BOT_TOKEN=test-token\nTELEAGENT_CHAT_ID=123\n")
        runtime = repo / ".runtime"
        runtime.mkdir()
        (runtime / "tunnel.log").write_text("https://new-address.trycloudflare.com")
        (runtime / "access-key").write_text("a" * 64)
        binding = hashlib.sha256(b"test-token:123").hexdigest()[:16]
        (runtime / f"launch-message-{binding}.id").write_text("7")
        module_path = Path(__file__).resolve().parents[1] / "examples/telegram-file-browser/connect-menu.py"
        spec = importlib.util.spec_from_file_location("connect_mini_app", module_path)
        connector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(connector)
        with mock.patch.object(setup, "telegram_api") as api:
            connector.connect(repo, repo)
        calls = {call.args[1]: call.args[2] for call in api.call_args_list}
        self.assertEqual(json.loads(calls["setChatMenuButton"]["menu_button"]), {"type": "commands"})
        commands = json.loads(calls["setMyCommands"]["commands"])
        self.assertEqual(commands[:-1], ui.command_menu())
        self.assertEqual(commands[-1]["command"], "files")
        rows = json.loads(calls["editMessageReplyMarkup"]["reply_markup"])["inline_keyboard"]
        self.assertEqual(rows[0][0]["web_app"]["url"], (runtime / "launch-url").read_text())
        self.assertEqual(calls["editMessageReplyMarkup"]["message_id"], "7")
        self.assertNotIn("sendMessage", calls)

        # A named bot must use its selected credentials, never the main bot's.
        (secret_dir / "notify-beta.env").write_text("TELEAGENT_BOT_TOKEN=beta-token\nTELEAGENT_CHAT_ID=456\n")
        with mock.patch.object(setup, "telegram_api", return_value={"message_id": 8}) as api:
            connector.connect(repo, repo, Path(".secrets/notify-beta.env"), "https://files.example.com")
        self.assertTrue(all(call.args[0] == "beta-token" for call in api.call_args_list))
        for call in api.call_args_list:
            if "chat_id" in call.args[2]:
                self.assertEqual(call.args[2]["chat_id"], "456")
        self.assertNotIn("editMessageReplyMarkup", [call.args[1] for call in api.call_args_list])
        with mock.patch.object(setup, "telegram_api") as api:
            with self.assertRaises(SystemExit):
                connector.connect(repo, repo, origin="http://files.example.com")
        api.assert_not_called()
        with (
            mock.patch.dict(os.environ, {"TELEAGENT_BOT_TOKEN": "ambient-other-bot", "TELEAGENT_CHAT_ID": "789"}),
            mock.patch.object(setup, "telegram_api") as api,
        ):
            with self.assertRaises(SystemExit):
                connector.connect(repo, repo, Path(".secrets/missing-bot.env"), "https://files.example.com")
        api.assert_not_called()

    def test_named_bot_must_opt_in_separately(self):
        repo = Path(self.temp.name)
        (repo / "config").mkdir()
        (repo / "config/relay.env").write_text('export TELEAGENT_MINI_APP_URL_FILE="/private/main-url"\n')
        script = Path(__file__).resolve().parents[1] / "scripts/relay_paths.sh"
        env = {k: v for k, v in os.environ.items() if not k.startswith("TELEAGENT_")}
        env.update(TELEAGENT_REPO=str(repo), TELEAGENT_INSTANCE="beta")
        command = ['bash', '-c', 'source "$1"; printf "%s" "${TELEAGENT_MINI_APP_URL_FILE:-}"', 'test', str(script)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "")
        (repo / "config/relay-beta.env").write_text('export TELEAGENT_MINI_APP_URL_FILE="/private/beta-url"\n')
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "/private/beta-url")


if __name__ == "__main__":
    unittest.main()
