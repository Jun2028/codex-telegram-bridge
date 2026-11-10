"""The direct host: an agent the relay manages itself, with no tmux and no /proc.

These tests avoid every Unix-only call so the same file runs on Linux, macOS
and Windows. Each provider CLI is a deterministic fixture and Telegram is
captured; `tests/live_providers.py` runs the same flow against the real CLIs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from teleagent import direct_pane, host, portable

# One fixture stands in for every provider CLI; its first argument names which.
FAKE_CLI = '''import json, sys, time, uuid
name, args = sys.argv[1], sys.argv[2:]
def emit(value):
    print(json.dumps(value), flush=True)
def wait(text):
    if "WAIT" in text:
        time.sleep(60)
if name == "claude":
    if args == ["auth", "status"]:
        emit({"loggedIn": True}); raise SystemExit(0)
    text = sys.stdin.buffer.read().decode("utf-8")
    sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fixture"})
    wait(text)
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "DIRECT_OK"})
elif name == "codex":
    assert args[0] == "exec" and args[-1] == "-" and "--json" in args, args
    text = sys.stdin.buffer.read().decode("utf-8")
    sid = args[2] if args[1] == "resume" else str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": sid})
    emit({"type": "item.completed", "item": {"type": "error", "message": "configuration notice"}})
    emit({"type": "turn.started"})
    wait(text)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "working on it"}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "DIRECT_OK"}})
    emit({"type": "turn.completed", "usage": {}})
elif name == "agy":
    text = args[args.index("-p") + 1]
    cid = args[args.index("--conversation") + 1] if "--conversation" in args else str(uuid.uuid4())
    emit({"event": "init", "conversation_id": cid})
    wait(text)
    emit({"event": "result", "result": {"status": "SUCCESS", "response": "DIRECT_OK", "conversation_id": cid}})
elif name == "dsh":
    if args == ["--version"]:
        print("0.0.0-fixture"); raise SystemExit(0)
    assert "--json" in args and args[-1] == "-", args
    text = sys.stdin.buffer.read().decode("utf-8")
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else str(uuid.uuid4())
    emit({"type": "session", "sessionId": sid})
    wait(text)
    emit({"type": "final", "text": "DIRECT_OK"})
'''
# How each provider is started from Telegram, and the name its harness reports.
PROVIDERS = {
    "claude": ("/switch claude sonnet low", "ta-claude"),
    "codex": ("/switch codex luna low", "ta-codex"),
    "agy": ("/switch agy gemini-fixture", "ta-agy"),
    "dsh": ("/switch dsh flash low", "ta-dsh"),
}


def wait_for(check, label, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out: " + label)


def install_fake_cli(directory: Path, name: str) -> str:
    """Write a fixture CLI named like the real one; return a path `shutil.which` accepts."""
    script = directory / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if portable.WINDOWS:
        launcher = directory / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" {name} %*\r\n', encoding="utf-8")
    else:
        launcher = directory / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {name} "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    return str(launcher)


class PortableTests(unittest.TestCase):
    def test_lock_excludes_a_second_holder_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.lock"
            first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                self.assertTrue(portable.lock_file(first, blocking=False))
                self.assertFalse(portable.lock_file(second, blocking=False))
                portable.unlock_file(first)
                self.assertTrue(portable.lock_file(second, blocking=False))
                portable.unlock_file(second)
            finally:
                os.close(first)
                os.close(second)

    def test_pid_alive(self):
        self.assertTrue(portable.pid_alive(os.getpid()))
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        self.assertFalse(portable.pid_alive(finished.pid))
        self.assertFalse(portable.pid_alive(0))

    def test_keystrokes_become_the_bytes_a_terminal_sends(self):
        self.assertEqual(direct_pane.event_bytes({"paste": "a\nb"}), b"\x1b[200~a\nb\x1b[201~")
        self.assertEqual(direct_pane.event_bytes({"keys": ["C-u", "Enter"]}), b"\x15\n")
        self.assertEqual(direct_pane.event_bytes({"keys": ["Escape"]}), b"\x1b")
        self.assertEqual(direct_pane.event_bytes({"unknown": 1}), b"")


class DirectPaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, TELEAGENT_HOST="direct", TELEAGENT_LOG_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(host.stop, "t:agent.0")

    def test_pane_runs_feeds_reports_and_stops_a_runner(self):
        self.assertTrue(host.is_direct())
        self.assertIsNone(host.record("t:agent.0"))
        received = self.root / "received.bin"
        echo = ("import sys\nprint('runner ready', flush=True)\n"
                f"out = open({str(received)!r}, 'ab', buffering=0)\n"
                "while True:\n    b = sys.stdin.buffer.read(1)\n    if not b: break\n    out.write(b)\n")
        host.start("t:agent.0", [sys.executable, "-c", echo], cwd=self.root, env={},
                   process_comm="ta-claude")
        found = host.record("t:agent.0")
        self.assertEqual(found["process_comm"], "ta-claude")
        rows = wait_for(lambda: len(host.process_rows()) == 2 and host.process_rows(), "runner row")
        self.assertEqual(rows[0][2], host.PANE_PROCESS_NAME)
        self.assertEqual((rows[1][1], rows[1][2]), (found["pane_pid"], "ta-claude"))
        self.assertEqual(host.process_comm(rows[1][0]), "ta-claude")
        self.assertTrue(host.process_start(rows[1][0]))
        wait_for(lambda: "runner ready" in host.tail("t:agent.0"), "runner output")
        host.send_keys("t:agent.0", "C-u")
        host.paste("t:agent.0", "héllo\nworld")
        host.send_keys("t:agent.0", "Enter")
        expected = b"\x15\x1b[200~" + "héllo\nworld".encode() + b"\x1b[201~\n"
        wait_for(lambda: received.exists() and received.read_bytes() == expected, "input bytes")
        with self.assertRaises(subprocess.SubprocessError):
            host.send_keys("t:agent.0", "F5")
        # A second pane cannot take over a live target.
        self.assertEqual(direct_pane.run(host.pane_dir("t:agent.0"), "t:agent.0", "x",
                                         str(self.root), [sys.executable, "-c", "pass"]), 3)
        self.assertTrue(host.stop("t:agent.0"))
        self.assertIsNone(host.record("t:agent.0"))
        self.assertEqual(host.process_rows(), [])
        self.assertFalse(host.stop("t:agent.0"))
        with self.assertRaises(subprocess.SubprocessError):
            host.paste("t:agent.0", "late")

    def test_crashed_runner_is_restarted_and_old_input_never_replays(self):
        marker = self.root / "starts"
        crash_once = (f"import sys, pathlib\np = pathlib.Path({str(marker)!r})\n"
                      "n = int(p.read_text()) + 1 if p.exists() else 1\np.write_text(str(n))\n"
                      "if n == 1: raise SystemExit(7)\n"
                      "data = sys.stdin.buffer.read(3)\n"
                      f"pathlib.Path({str(self.root / 'got')!r}).write_bytes(data)\n")
        directory = host.pane_dir("t:agent.0")
        directory.mkdir(parents=True)
        (directory / "input.jsonl").write_text(json.dumps({"text": "OLD"}) + "\n")
        host.start("t:agent.0", [sys.executable, "-c", crash_once], cwd=self.root, env={},
                   process_comm="ta-claude")
        wait_for(lambda: marker.exists() and marker.read_text() == "2", "restart after a crash", 20)
        host.send_keys("t:agent.0", "NEW", literal=True)
        wait_for(lambda: (self.root / "got").exists(), "input after restart")
        self.assertEqual((self.root / "got").read_bytes(), b"NEW")


class Relay:
    """The listener's own handlers, with Telegram captured, for one bot runtime."""

    def __init__(self, root: Path) -> None:
        import telegram_agent_registry as registry
        from teleagent import commands, delivery, transport
        self.registry, self.commands, self.delivery = registry, commands, delivery
        self.repo, self.runtime, self.home = root / "repo", root / "runtime", root / "rollouts"
        self.repo.mkdir(exist_ok=True)
        self.runtime.mkdir(exist_ok=True)
        # The agent's working repository carries the relay scripts, as a deployment
        # does. A copy, not a link: creating links needs extra rights on Windows.
        shutil.copytree(ROOT / "scripts", self.repo / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # Model catalogues the launchers read; never the operator's own settings.
        shutil.copytree(ROOT / "config" / "deepseek", self.repo / "config" / "deepseek")
        (self.repo / "AGENTS.md").write_text("Follow only the test request.\n", encoding="utf-8")
        state = lambda name: str(self.runtime / name)  # noqa: E731
        self.args = argparse.Namespace(
            repo_root=self.repo, session="direct-test", codex_window="codex",
            target_pane="direct-test:codex.0", submit_delay=0.2, relay_mode="tmux-enter",
            allow_shell_pane=False, bridge_ack=False, max_log_chars=4000, tmux_lines=30,
            agent_recovery_wait=3, agent_watchdog=True,
            codex_usage_state_path=state("usage.json"), codex_reset_state_path=state("reset.json"),
            codex_auth_state_path=state("auth.json"), agent_lifecycle_state_path=state("life.json"),
            relay_confirmation_state_path=state("pending.json"),
            relay_queue_state_path=state("queue.json"), agent_message_state_path=state("messages.json"))
        self.sent: list[str] = []
        self.count = 0
        self.patch = mock.patch.object(
            transport, "send_reply", side_effect=lambda *a, **kw: self.sent.append(str(a[2])))

    def receive(self, text: str) -> dict:
        self.count += 1
        update = {"update_id": self.count, "message": {
            "message_id": self.count, "date": int(time.time()),
            "chat": {"id": "123", "type": "private"}, "from": {"id": 123}, "text": text}}
        return self.commands.handle_update(update, self.args, {}, "offline", "123",
                                           self.runtime / "listener.jsonl",
                                           route_state_path=self.runtime / "routes.json") or {}

    def metadata(self):
        meta = self.registry.active_agent_for_pane(self.args.target_pane)
        if meta:
            meta = self.registry.refresh_codex_session_link(meta, target_pane=self.args.target_pane)
            if meta.get("codex_session_path"):
                return meta
        return None

    def records(self) -> list[dict]:
        meta = self.metadata()
        if not meta:
            return []
        lines = Path(meta["codex_session_path"]).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def last_event(self):
        rows = self.records()
        return rows[-1].get("payload", {}).get("type") if rows else None

    def forwarded(self) -> list[str]:
        """Deliver new agent messages as the listener would; return what was sent."""
        before = len(self.sent)
        self.delivery.drain_codex_agent_messages(
            "offline", "123", self.metadata(), self.runtime / "messages.json", None, {},
            sessions_root=self.home, route_state_path=self.runtime / "routes.json")
        return self.sent[before:]


def scenario(root: Path, provider: str) -> None:
    """Drive one provider's direct-host agent through the listener's own handlers."""
    from teleagent import processes, submission
    launch, process_name = PROVIDERS[provider]
    relay = Relay(root)
    with relay.patch:
        try:
            relay.receive(launch)
            assert "started in" in relay.sent[-1], relay.sent[-1]
            wait_for(relay.metadata, "session registration")
            assert processes.tmux_pane_harness(relay.args.target_pane).process_comm == process_name
            record = relay.receive("Reply with exactly DIRECT_OK.\nSecond line stays in one message.")
            assert "submission confirmed" in record.get("relay_result", ""), record.get("relay_result")
            users = [r["payload"]["content"][0]["text"] for r in relay.records()
                     if r.get("type") == "response_item" and r.get("payload", {}).get("role") == "user"]
            assert len(users) == 1 and "Second line stays in one message." in users[0], users
            wait_for(lambda: relay.last_event() == "task_complete", "first reply")
            sent = relay.forwarded()
            assert any("DIRECT_OK" in text for text in sent), sent
            assert not any("configuration notice" in text for text in sent), sent
            relay.receive("/status")
            assert "idle" in relay.sent[-1], relay.sent[-1]
            print(f"PASS {provider}: start, multiline submit, reply forwarding, idle status", flush=True)

            relay.receive("WAIT then reply")
            wait_for(lambda: submission.codex_session_turn_active(
                Path(relay.metadata()["codex_session_path"])), "active turn")
            relay.receive("/status")
            assert "working" in relay.sent[-1], relay.sent[-1]
            record = relay.receive("/interrupt Reply with exactly DIRECT_OK.")
            assert record["action"] == "interrupt", record
            wait_for(lambda: relay.last_event() == "task_complete", "replacement reply")
            assert any(r.get("payload", {}).get("type") == "turn_aborted" for r in relay.records())
            print(f"PASS {provider}: working status, interrupt, replacement instruction", flush=True)

            relay.receive("/model sol")
            assert "not confirmed" in relay.sent[-1], relay.sent[-1]
            relay.receive("/kill_agent")
            assert "Stopped" in relay.sent[-1], relay.sent[-1]
            assert host.record(relay.args.target_pane) is None
            print(f"PASS {provider}: live Codex controls refused, agent stopped", flush=True)
        finally:
            host.stop(relay.args.target_pane)


def relay_environment(root: Path, cli_directory: Path | None) -> dict[str, str]:
    """An isolated bot runtime on the direct host; fixture CLIs when a directory is given."""
    env = dict(os.environ)
    for name in [key for key in env if key.startswith(("TELEAGENT_", "TMUX"))]:
        env.pop(name)
    key_file = root / "deepseek.env"
    env.update(
        TELEAGENT_HOST="direct", TELEAGENT_REPO=str(root / "repo"),
        TELEAGENT_SCRATCH=str(root / "scratch"), TELEAGENT_LOG_DIR=str(root / "runtime"),
        TELEAGENT_CODEX_HOME=str(root / "rollouts"), TELEAGENT_DSH_HOME=str(root / "dsh-home"),
        TELEAGENT_AGENT_DIR=str(root / "runtime" / "agents"), TELEAGENT_INSTANCE="main",
        TELEAGENT_PERSONALITY_FILE=str(root / "absent-personality"),
        TELEAGENT_SECRET_ENV=str(root / "absent-secrets"),
        TELEAGENT_CODEX_ACCESS_MODE="full-access", PYTHONUTF8="1")
    if cli_directory is not None:
        key_file.write_text("DEEPSEEK_API_KEY=fixture-key\n", encoding="utf-8")
        launchers = {name: install_fake_cli(cli_directory, name) for name in PROVIDERS}
        env.update(TELEAGENT_CLAUDE_BIN=launchers["claude"], TELEAGENT_AGY_BIN=launchers["agy"],
                   TELEAGENT_DSH_BIN=launchers["dsh"], TELEAGENT_DS_KEY_FILE=str(key_file),
                   PATH=str(cli_directory) + os.pathsep + env.get("PATH", ""))
    return env


class DirectHostRelayTests(unittest.TestCase):
    """Every provider runs through the same listener, host and input protocol."""

    def run_provider(self, provider: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cli").mkdir()
            result = subprocess.run(
                [sys.executable, __file__, "--scenario", str(root), provider],
                env=relay_environment(root, root / "cli"), text=True, capture_output=True,
                timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count(f"PASS {provider}:"), 3,
                             result.stdout + result.stderr)

    def test_claude_code(self):
        self.run_provider("claude")

    def test_codex_without_its_terminal_interface(self):
        self.run_provider("codex")

    def test_antigravity(self):
        self.run_provider("agy")

    def test_deepseek_harness(self):
        self.run_provider("dsh")


@unittest.skipIf(portable.WINDOWS or not shutil.which("tmux") or not shutil.which("bash"),
                 "needs tmux and bash")
class TmuxHostRelayTests(unittest.TestCase):
    """The same scenario on a tmux host, through the bash launcher and supervisor.

    Each provider gets a private tmux server: a tmux server keeps the
    environment it was started with, and the user's own server is never touched.
    Codex is absent because its terminal interface cannot be a stand-in;
    `tests/live_providers.py --host tmux codex` covers it with the real CLI.
    """

    def run_provider(self, provider: str) -> None:
        # A short path: tmux socket paths have a small length limit.
        with tempfile.TemporaryDirectory(prefix="ta-", dir="/tmp") as directory:
            root = Path(directory)
            (root / "cli").mkdir()
            (root / "tmux").mkdir(mode=0o700)
            env = relay_environment(root, root / "cli")
            env.update(TELEAGENT_HOST="tmux", TELEAGENT_TMUX_SESSION="direct-test",
                       TMUX_TMPDIR=str(root / "tmux"))
            try:
                result = subprocess.run(
                    [sys.executable, __file__, "--scenario", str(root), provider],
                    env=env, text=True, capture_output=True, timeout=240)
            finally:
                # Only the private server under this TMUX_TMPDIR can be reached.
                subprocess.run(["tmux", "kill-server"], env=env, check=False, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count(f"PASS {provider}:"), 3,
                             result.stdout + result.stderr)

    def test_claude_code(self):
        self.run_provider("claude")

    def test_antigravity(self):
        self.run_provider("agy")

    def test_deepseek_harness(self):
        self.run_provider("dsh")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        scenario(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
