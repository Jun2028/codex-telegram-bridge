"""The harness registry and command table stay the single source of truth."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import telegram_agent_registry as registry
import telegram_inbox
from teleagent import (claude, commands, harnesses, lifecycle, processes, provider_commands,
                       settings, transport, ui)

SCRIPTS = ROOT / "scripts"
# Launch models as the listener passes them to the shell launcher.
SAMPLE_MODELS = ["cc-sonnet", "cc-opus", "cc-haiku", "dsh-flash", "dsh-pro", "g-flash",
                 "argon", "sonnet", "opus", "oss", "gemini-3-flash", "claude-opus-4",
                 "gpt-oss-120b", "gpt-6.1-sol", "gpt-6-astra", "deepseek-flash", "deepseek-v4-pro"]


class HarnessRegistryTests(unittest.TestCase):
    def test_every_copy_of_the_process_names_matches_the_registry(self):
        self.assertEqual(set(settings.CODEX_COMMANDS), set(harnesses.PROCESS_COMMANDS))
        self.assertEqual(set(registry.CODEX_PROCESS_COMMANDS), set(harnesses.PROCESS_COMMANDS))
        self.assertEqual(len(harnesses.PROCESS_COMMANDS), len(harnesses.ALL))
        # Shell cannot import the registry, so its literal lists are checked here.
        found = 0
        for name in ("start_codex_agent.sh", "ensure_telegram_relay.sh"):
            for listed in re.findall(r"codex(?:\|codex-[a-z]+)+", (SCRIPTS / name).read_text()):
                found += 1
                self.assertEqual(set(listed.split("|")), set(harnesses.PROCESS_COMMANDS), name)
        self.assertGreaterEqual(found, 3)

    def test_shell_and_python_classify_launch_models_identically(self):
        script = "source scripts/relay_paths.sh >/dev/null 2>&1\n" + "\n".join(
            f"if tele_agent_is_claude_model {m}; then echo claude; "
            f"elif tele_agent_is_dsh_model {m}; then echo dsh; "
            f"elif tele_agent_is_agy_model {m}; then echo agy; else echo codex; fi"
            for m in SAMPLE_MODELS)
        with tempfile.TemporaryDirectory() as scratch:
            env = dict(os.environ, TELEAGENT_REPO=str(ROOT), TELEAGENT_INSTANCE="main",
                       TELEAGENT_SCRATCH=scratch, TELEAGENT_LOG_DIR=scratch)
            result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, text=True,
                                    capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(),
                         [harnesses.for_model(m).key for m in SAMPLE_MODELS])

    def test_model_ownership_and_capabilities(self):
        self.assertIs(harnesses.for_model("cc-opus"), harnesses.CLAUDE)
        self.assertIs(harnesses.for_model("opus"), harnesses.AGY)
        self.assertIs(harnesses.for_model("dsh-flash"), harnesses.DSH)
        # Codex-backed DeepSeek provider models still run in the Codex harness.
        self.assertIs(harnesses.for_model("deepseek-flash"), harnesses.CODEX)
        self.assertEqual([h.key for h in harnesses.ALL if h.uses_codex_auth], ["codex"])
        self.assertEqual([h.key for h in harnesses.ALL if h.live_codex_controls], ["codex"])
        for harness in harnesses.ALL:
            self.assertIs(harnesses.for_process_comm(harness.process_comm), harness)
        self.assertIsNone(harnesses.for_process_comm("bash"))
        self.assertIn("TELEAGENT_CLAUDE_BIN=", lifecycle.build_codex_agent_command(
            ROOT, "/bin/claude", "", model="cc-sonnet", reasoning_effort="high"))

    def test_pane_harness_is_read_from_the_live_process_name(self):
        with tempfile.TemporaryDirectory() as directory:
            def comm(pid):
                return {11: "bash", 12: "codex-claude"}[pid]
            real_read = Path.read_text
            def read(path, *a, **kw):
                match = re.fullmatch(r"/proc/(\d+)/comm", str(path))
                return comm(int(match.group(1))) + "\n" if match else real_read(path, *a, **kw)
            with mock.patch.object(processes.agent_registry, "tmux_pane_pid", return_value=10), \
                 mock.patch.object(processes.agent_registry, "descendants", return_value=[11, 12]), \
                 mock.patch.object(Path, "read_text", read):
                self.assertIs(processes.tmux_pane_harness("x:0.0"), harnesses.CLAUDE)
                self.assertTrue(processes.tmux_pane_has_claude_process("x:0.0"))
                self.assertFalse(processes.tmux_pane_has_dsh_process("x:0.0"))
            with mock.patch.object(processes.agent_registry, "tmux_pane_pid", return_value=None):
                self.assertIsNone(processes.tmux_pane_harness(directory))


class CommandTableTests(unittest.TestCase):
    HANDLER_FILES = ("commands.py", "ui.py", "settings.py", "queue.py", "claude_auth.py",
                     "provider_commands.py")

    def handled(self) -> set[str]:
        names: set[str] = set()
        for name in self.HANDLER_FILES:
            source = (SCRIPTS / "teleagent" / name).read_text()
            names.update(re.findall(r"""["']/([a-z_]+)["']""", source))
        return names

    def test_handlers_and_command_table_list_the_same_commands(self):
        self.assertEqual(self.handled(), set(ui.COMMAND_NAMES))
        self.assertEqual(len(ui.COMMANDS), len(ui.COMMAND_NAMES))

    def test_menu_help_and_suggestions_come_from_the_table(self):
        menu = [entry["command"] for entry in ui.command_menu()]
        self.assertEqual(menu, [c.name for c in ui.COMMANDS if c.menu])
        listing = ui.all_commands_text()
        for command in ui.COMMANDS:
            if command.same_as:
                self.assertIn(f"/{command.name} = {command.same_as}", listing)
                self.assertFalse(command.menu, command.name)
            else:
                self.assertIn(f"/{command.name} {command.args}".rstrip() + " — ", listing)
        # The menu offers one spelling per action, never a provider-named one.
        self.assertFalse({"claude", "dsh", "claude_usage", "claude_auth", "reauth"} & set(menu))
        self.assertLessEqual({"switch", "usage", "auth"}, set(menu))
        for provider in provider_commands.PROVIDERS:
            self.assertIn(provider, ui.PROVIDERS.split("|"))
            self.assertIn(provider + " — ", ui.models_text())
        self.assertEqual(ui.help_reply("ALL"), listing)
        self.assertEqual(ui.help_reply(""), ui.help_text())
        self.assertIn("/help all", ui.help_text())
        self.assertIn("Did you mean /restart_agent?", ui.unknown_command_text("/restart_agnet"))
        self.assertNotIn("Did you mean", ui.unknown_command_text("/zzzzzz"))


class LaunchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def launch(self, text: str):
        args = argparse.Namespace(
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"),
            agent_lifecycle_state_path=str(self.root / "life.json"),
            repo_root=self.root, session="fixture", codex_window="codex",
            max_log_chars=4000, target_pane="fixture:codex.0")
        update = {"update_id": 1, "message": {"message_id": 2, "date": 1900000000,
                  "chat": {"id": "123"}, "from": {"id": 123}, "text": text}}
        with mock.patch.object(registry, "active_agent_for_pane", return_value=None), \
             mock.patch.object(processes, "tmux_tail", return_value=""), \
             mock.patch.object(processes, "tmux_pane_has_claude_process", return_value=False), \
             mock.patch.object(commands._auth, "active_codex_auth_failure",
                               return_value={"reason": "refresh_token_revoked"}), \
             mock.patch.object(commands._auth, "format_auth_failure_fallback",
                               return_value="Codex sign-in required."), \
             mock.patch.object(lifecycle, "managed_codex_agent_present", return_value=True), \
             mock.patch.object(claude, "ensure_available", return_value="/bin/claude"), \
             mock.patch.object(lifecycle, "start_codex_agent",
                               return_value=("fixture:codex.0", "started", None)) as start, \
             mock.patch.object(transport, "send_reply") as send:
            telegram_inbox.handle_update(update, args, {}, "token", "123",
                                         self.root / "listener.jsonl")
        return start, [call.args[2] for call in send.call_args_list]

    def test_codex_sign_in_failure_blocks_only_codex_launches(self):
        start, replies = self.launch("/restart_agent")
        start.assert_not_called()
        self.assertEqual(replies, ["Codex sign-in required."])
        start, replies = self.launch("/restart_agent cc-opus max")
        self.assertEqual(start.call_args.kwargs["model"], "cc-opus")
        self.assertIn("Restarting Claude Code agent", replies[0])
        self.assertNotIn("Codex agent", replies[0])

    def test_switch_is_one_command_for_every_provider(self):
        for text in ("/switch claude opus max", "/claude opus max"):
            start, replies = self.launch(text)
            self.assertEqual(start.call_args.kwargs["model"], "cc-opus")
            self.assertEqual(start.call_args.kwargs["reasoning_effort"], "max")
            self.assertIn("Claude Code started", replies[-1])
        # Codex needs its own sign-in; other providers do not.
        start, replies = self.launch("/switch codex")
        start.assert_not_called()
        self.assertIn("/auth codex login", replies[-1])
        start, replies = self.launch("/switch")
        start.assert_not_called()
        self.assertIn(provider_commands.SWITCH_HELP, replies[-1])

    def test_auth_and_usage_name_a_provider_or_use_the_running_one(self):
        with mock.patch.object(commands._auth, "codex_login_status_summary",
                               return_value="Logged in using ChatGPT"):
            _start, replies = self.launch("/auth")
            self.assertEqual(replies, ["Codex sign-in: Logged in using ChatGPT"])
            _start, replies = self.launch("/auth codex logout")
            self.assertIn("only status and login", replies[0])
        with mock.patch.object(claude, "usage", return_value="Claude usage, checked live:"):
            _start, replies = self.launch("/usage claude")
        self.assertEqual(replies, ["Claude usage, checked live:"])
        _start, replies = self.launch("/usage dsh")
        self.assertIn("no account controls", replies[0])
        _start, replies = self.launch("/auth claude everything")
        self.assertEqual(replies, [provider_commands.AUTH_HELP])


class ProviderCommandTests(unittest.TestCase):
    def test_account_commands_resolve_to_one_provider(self):
        resolve = provider_commands.resolve_account_command
        codex, claude_code, dsh = harnesses.CODEX, harnesses.CLAUDE, harnesses.DSH
        self.assertEqual(resolve("/usage", "", claude_code), ("/claude_usage", ""))
        self.assertEqual(resolve("/usage", "codex", claude_code), ("/codex_usage", ""))
        self.assertEqual(resolve("/usage", "", dsh), ("/codex_usage", ""))
        self.assertEqual(resolve("/auth", "", codex), ("/codex_auth", "status"))
        self.assertEqual(resolve("/auth", "login", claude_code), ("/claude_auth", "login"))
        self.assertEqual(resolve("/auth", "Claude Logout", codex), ("/claude_auth", "logout"))
        self.assertEqual(resolve("/reauth", "", claude_code), ("/claude_auth", "login"))
        self.assertEqual(resolve("/reauth", "", codex), ("/codex_auth", "login"))
        self.assertEqual(resolve("/status", "x", codex), ("/status", "x"))
        for command, payload in [("/usage", "agy"), ("/usage", "claude now"),
                                 ("/auth", "sideways"), ("/auth", "claude login now")]:
            with self.assertRaises(ValueError):
                resolve(command, payload, codex)

    def test_switch_parses_every_provider_the_same_way(self):
        parse = provider_commands.parse_switch
        self.assertEqual(parse("/switch", "claude opus max"),
                         (harnesses.CLAUDE, "cc-opus", "max", "opus"))
        self.assertEqual(parse("/claude", ""), (harnesses.CLAUDE, "cc-sonnet", "high", "sonnet"))
        self.assertEqual(parse("/switch", "dsh pro")[:3], (harnesses.DSH, "dsh-pro", "max"))
        self.assertEqual(parse("/dsh", "pro"), parse("/switch", "dsh pro"))
        self.assertEqual(parse("/switch", "agy g-flash")[:2], (harnesses.AGY, "g-flash"))
        harness, model, _effort, native = parse("/switch", "codex")
        self.assertEqual((harness, model, native),
                         (harnesses.CODEX, settings.DEFAULT_CODEX_AGENT_MODEL,
                          settings.DEFAULT_CODEX_AGENT_MODEL))
        self.assertIs(parse("/switch", "CODEX astra xhigh")[0], harnesses.CODEX)
        for payload in ["", "gemini", "agy", "codex opus", "codex cc-opus", "agy astra",
                        "claude opus max extra"]:
            with self.assertRaises(ValueError):
                parse("/switch", payload)


if __name__ == "__main__":
    unittest.main()
