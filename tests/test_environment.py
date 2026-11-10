"""The shell-free environment matches what relay_paths.sh exports."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import relay
from teleagent import environment, host, portable

# Model-name constants relay_paths.sh also exports; Python keeps them in settings.py.
SHELL_ONLY = {"TELEAGENT_DEEPSEEK_FLASH_MODEL", "TELEAGENT_DEEPSEEK_PRO_MODEL",
              "TELEAGENT_DSH_FLASH_MODEL", "TELEAGENT_DSH_PRO_MODEL"}
RELAY_ENV = '''# Sample operator configuration.
export TELEAGENT_SCRATCH="${TELEAGENT_SCRATCH:-$HOME/data/tele-agent}"
export TELEAGENT_LOG_DIR="${TELEAGENT_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
export TELEAGENT_CODEX_MODEL="${TELEAGENT_CODEX_MODEL:-deepseek-flash}"
export TELEAGENT_REQUIRE_GROUP_MENTION=1   # trailing comment
# export TELEAGENT_MINI_APP_URL_FILE="/private/path/to/launch-url"
export TELEAGENT_DS_KEY_FILE='$HOME/literal/deepseek.env'
'''
NAMED_ENV = '''export TELEAGENT_CODEX_ACCESS_MODE="chat-only"
export TELEAGENT_INSTANCE_SCRATCH="$HOME/bots/lab"
'''


def teleagent_values(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if key.startswith("TELEAGENT_")}


class ExpansionTests(unittest.TestCase):
    def test_config_file_forms(self):
        env = {"HOME": "/home/operator", "SET": "value", "EMPTY": ""}
        cases = {
            '"${SET:-fallback}"': "value",
            '"${EMPTY:-fallback}"': "fallback",
            '"${MISSING:-$HOME/.codex}"': "/home/operator/.codex",
            '"${MISSING:-${EMPTY:-deep}}/x"': "deep/x",
            "$HOME/plain": "/home/operator/plain",
            "'$HOME/literal'": "$HOME/literal",
            "1   # comment": "1",
            '""': "",
        }
        for raw, expected in cases.items():
            self.assertEqual(environment.expand(raw, env), expected, raw)

    def test_invalid_access_mode_and_shared_home_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {"HOME": directory, "TELEAGENT_CODEX_ACCESS_MODE": "everything"}
            with self.assertRaisesRegex(ValueError, "full-access or chat-only"):
                environment.build(Path(directory), base)
            shared = {"HOME": directory, "TELEAGENT_CODEX_ACCESS_MODE": "chat-only",
                      "TELEAGENT_CHAT_ONLY_CODEX_HOME": directory + "/.codex"}
            with self.assertRaisesRegex(ValueError, "private TELEAGENT_CHAT_ONLY_CODEX_HOME"):
                environment.build(Path(directory), shared)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"),
                     "compares against the bash launcher, which is Linux-only")
class ShellParityTests(unittest.TestCase):
    def compare(self, instance: str, ambient: dict[str, str]) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            home, repo = Path(directory) / "userhome", Path(directory) / "repo"
            (repo / "config").mkdir(parents=True)
            home.mkdir()
            (repo / "config" / "relay.env").write_text(RELAY_ENV)
            (repo / "config" / "relay-lab.env").write_text(NAMED_ENV)
            (repo / "config" / "personality-lab.md").write_text("Lab bot.\n")
            base = {"HOME": str(home), "PATH": os.environ["PATH"], "TELEAGENT_REPO": str(repo),
                    "TELEAGENT_INSTANCE": instance, **ambient}
            shell = subprocess.run(
                ["bash", "-c", f"source {ROOT / 'scripts' / 'relay_paths.sh'} && env -0"],
                env=base, capture_output=True, check=True, timeout=30)
            exported = dict(item.split("=", 1) for item in shell.stdout.decode().split("\0") if "=" in item)
            expected = teleagent_values(exported)
            actual = teleagent_values(environment.build(repo, base))
            self.assertEqual(set(expected) - set(actual), SHELL_ONLY)
            for name in SHELL_ONLY:
                expected.pop(name)
            self.assertEqual(actual, expected)
            return {key: value.replace(directory, "<tmp>") for key, value in actual.items()}

    def test_main_bot(self):
        values = self.compare("main", {})
        self.assertEqual(values["TELEAGENT_LOG_DIR"], "<tmp>/userhome/data/tele-agent/runtime")
        self.assertEqual(values["TELEAGENT_DS_KEY_FILE"], "$HOME/literal/deepseek.env")
        self.assertEqual(values["TELEAGENT_PERSONALITY_FILE"], "<tmp>/repo/config/personality.default.md")
        self.assertNotIn("TELEAGENT_MINI_APP_URL_FILE", values)

    def test_named_bot_ignores_ambient_values_from_another_bot(self):
        values = self.compare("lab", {
            "TELEAGENT_LOG_DIR": "/elsewhere/runtime", "TELEAGENT_SECRET_ENV": "/elsewhere/secret",
            "TELEAGENT_AGENT_ID": "someone-else", "TELEAGENT_CODEX_HOME": "/elsewhere/codex"})
        self.assertEqual(values["TELEAGENT_SCRATCH"], "<tmp>/userhome/bots/lab")
        self.assertEqual(values["TELEAGENT_LOG_DIR"], "<tmp>/userhome/bots/lab/runtime")
        self.assertEqual(values["TELEAGENT_SECRET_ENV"], "<tmp>/repo/.secrets/notify-lab.env")
        self.assertEqual(values["TELEAGENT_TMUX_SESSION"], "tele-agent-lab")
        self.assertEqual(values["TELEAGENT_PERSONALITY_FILE"], "<tmp>/repo/config/personality-lab.md")
        self.assertNotIn("TELEAGENT_AGENT_ID", values)

    def test_agent_binding_survives_only_with_the_launcher_sentinel(self):
        values = self.compare("lab", {"TELEAGENT_AGENT_ID": "mine",
                                      "TELEAGENT_PRESERVE_AGENT_BINDING": "1"})
        self.assertEqual(values["TELEAGENT_AGENT_ID"], "mine")
        self.assertNotIn("TELEAGENT_PRESERVE_AGENT_BINDING", values)


class LauncherTests(unittest.TestCase):
    def test_listener_command_and_target_follow_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env = environment.build(Path(directory), {"HOME": directory, "TELEAGENT_INSTANCE": "lab",
                                                      "TELEAGENT_MAX_FINAL_MESSAGE_CHARS": "900"})
            self.assertEqual(relay.agent_target(env), "tele-agent-lab:codex.0")
            command = relay.listener_command(env)
            self.assertEqual(command[:2], [sys.executable, str(ROOT / "scripts" / "telegram_inbox.py")])
            self.assertEqual(command[command.index("--target-pane") + 1], "tele-agent-lab:codex.0")
            self.assertEqual(command[command.index("--secret-env") + 1], env["TELEAGENT_SECRET_ENV"])
            self.assertEqual(command[-2:], ["--max-final-message-chars", "900"])
            env["TELEAGENT_LOG_DIR"] = directory
            self.assertFalse(relay.relay_running(env))

    def test_long_lived_processes_use_the_console_interpreter(self):
        # Under the windowless interpreter, console programs the relay starts
        # would each open a visible terminal.
        self.assertEqual(portable.python_executable(), sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            windowless = Path(directory) / "pythonw.exe"
            windowless.write_text("")
            (Path(directory) / "python.exe").write_text("")
            with mock.patch.object(portable.sys, "executable", str(windowless)), \
                 mock.patch.object(portable, "WINDOWS", True):
                self.assertEqual(portable.python_executable(), str(Path(directory) / "python.exe"))
                self.assertEqual(relay.listener_command(
                    environment.build(Path(directory), {"HOME": directory}))[0],
                    str(Path(directory) / "python.exe"))
            with mock.patch.object(portable.sys, "executable", str(windowless)), \
                 mock.patch.object(portable, "WINDOWS", False):
                self.assertEqual(portable.python_executable(), str(windowless))

    def test_autostart_task_runs_start_at_sign_in_and_every_five_minutes(self):
        xml = relay.TASK_XML.format(user="PC\\me", command="C:\\py\\pythonw.exe",
                                    arguments='"C:\\repo\\scripts\\relay.py" --instance main start')
        for expected in ("<LogonTrigger>", "<Interval>PT5M</Interval>",
                         "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
                         "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
                         "<RunLevel>LeastPrivilege</RunLevel>", "--instance main start"):
            self.assertIn(expected, xml)
        if not portable.WINDOWS:
            with tempfile.TemporaryDirectory() as directory:
                env = environment.build(Path(directory), {"HOME": directory})
                self.assertEqual(relay.autostart(env, "on"), 2)  # Linux has its cron watchdog.

    def test_file_browser_upkeep_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            env = environment.build(Path(directory), {"HOME": directory})
            with mock.patch.object(relay.host, "start") as start, \
                 mock.patch.object(relay.host, "record") as record:
                relay.ensure_file_browser(env)  # Not enabled: nothing is touched.
                start.assert_not_called()
                record.assert_not_called()
            self.assertEqual(relay.file_browser_targets(env), ("tele-agent:files.0", "tele-agent:files-tunnel.0"))
            self.assertTrue(relay.TUNNEL_ADDRESS.fullmatch("https://quiet-river-12.trycloudflare.com"))
            self.assertFalse(relay.TUNNEL_ADDRESS.search("Requesting a tunnel on trycloudflare.com"))

    def test_host_kind_can_be_forced_and_defaults_by_platform(self):
        saved = os.environ.get("TELEAGENT_HOST")
        try:
            for value in ("direct", "tmux"):
                os.environ["TELEAGENT_HOST"] = value
                self.assertEqual(host.kind(), value)
            os.environ.pop("TELEAGENT_HOST")
            expected = "tmux" if sys.platform.startswith("linux") and shutil.which("tmux") else "direct"
            self.assertEqual(host.kind(), expected)
        finally:
            if saved is None:
                os.environ.pop("TELEAGENT_HOST", None)
            else:
                os.environ["TELEAGENT_HOST"] = saved


if __name__ == "__main__":
    unittest.main()
