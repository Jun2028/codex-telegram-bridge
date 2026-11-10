"""The direct host: an agent the relay manages itself, with no tmux and no /proc.

These tests avoid every Unix-only call so the same file runs on Linux, macOS
and Windows. The Claude CLI is a deterministic fixture; Telegram is captured.
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

FAKE_CLAUDE = '''import json, sys, time, uuid
args = sys.argv[1:]
if args == ["auth", "status"]:
    print(json.dumps({"loggedIn": True})); raise SystemExit(0)
text = sys.stdin.buffer.read().decode("utf-8")
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fixture"}), flush=True)
if "WAIT" in text:
    time.sleep(60)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                  "result": "DIRECT_OK"}), flush=True)
'''


def wait_for(check, label, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out: " + label)


def install_fake_claude(directory: Path) -> str:
    """Write the fixture CLI and return a path `shutil.which` accepts on this OS."""
    script = directory / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if portable.WINDOWS:
        launcher = directory / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
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
                   process_comm="codex-claude")
        found = host.record("t:agent.0")
        self.assertEqual(found["process_comm"], "codex-claude")
        rows = wait_for(lambda: len(host.process_rows()) == 2 and host.process_rows(), "runner row")
        self.assertEqual(rows[0][2], host.PANE_PROCESS_NAME)
        self.assertEqual((rows[1][1], rows[1][2]), (found["pane_pid"], "codex-claude"))
        self.assertEqual(host.process_comm(rows[1][0]), "codex-claude")
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
                   process_comm="codex-claude")
        wait_for(lambda: marker.exists() and marker.read_text() == "2", "restart after a crash", 20)
        host.send_keys("t:agent.0", "NEW", literal=True)
        wait_for(lambda: (self.root / "got").exists(), "input after restart")
        self.assertEqual((self.root / "got").read_bytes(), b"NEW")


def scenario(root: Path) -> None:
    """Drive the listener's own handlers against a direct-host Claude agent."""
    import telegram_agent_registry as registry
    from teleagent import commands, delivery, messages, processes, submission, transport
    repo, runtime, home = root / "repo", root / "runtime", root / "rollouts"
    repo.mkdir(); runtime.mkdir()
    # The agent's working repository carries the relay scripts, as a deployment does.
    # A copy, not a link: creating links needs extra rights on Windows.
    shutil.copytree(ROOT / "scripts", repo / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "AGENTS.md").write_text("Follow only the test request.\n", encoding="utf-8")
    args = argparse.Namespace(
        repo_root=repo, session="direct-test", codex_window="codex",
        target_pane="direct-test:codex.0", submit_delay=0.2, relay_mode="tmux-enter",
        allow_shell_pane=False, bridge_ack=False, max_log_chars=4000, tmux_lines=30,
        agent_recovery_wait=3, agent_watchdog=True,
        codex_usage_state_path=str(runtime / "usage.json"),
        codex_reset_state_path=str(runtime / "reset.json"),
        codex_auth_state_path=str(runtime / "auth.json"),
        agent_lifecycle_state_path=str(runtime / "life.json"),
        relay_confirmation_state_path=str(runtime / "pending.json"),
        relay_queue_state_path=str(runtime / "queue.json"),
        agent_message_state_path=str(runtime / "messages.json"))
    routes, log, sent, counter = runtime / "routes.json", runtime / "listener.jsonl", [], [0]

    def receive(text):
        counter[0] += 1
        update = {"update_id": counter[0], "message": {
            "message_id": counter[0], "date": int(time.time()),
            "chat": {"id": "123", "type": "private"}, "from": {"id": 123}, "text": text}}
        return commands.handle_update(update, args, {}, "offline", "123", log, route_state_path=routes)

    def metadata():
        meta = registry.active_agent_for_pane(args.target_pane)
        if meta:
            meta = registry.refresh_codex_session_link(meta, target_pane=args.target_pane)
            if meta.get("codex_session_path"):
                return meta
        return None

    def records():
        meta = metadata()
        if not meta:
            return []
        lines = Path(meta["codex_session_path"]).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def last_event():
        rows = records()
        return rows[-1].get("payload", {}).get("type") if rows else None

    def reply():
        return str(sent[-1][0][2])

    with mock.patch.object(transport, "send_reply", side_effect=lambda *a, **kw: sent.append((a, kw))):
        try:
            receive("/switch claude sonnet low")
            assert "Claude Code started" in reply(), reply()
            wait_for(metadata, "session registration")
            assert processes.tmux_pane_has_claude_process(args.target_pane)
            record = receive("Reply with exactly DIRECT_OK.\nSecond line stays in one message.")
            assert "submission confirmed" in record.get("relay_result", ""), record.get("relay_result")
            users = [r["payload"]["content"][0]["text"] for r in records()
                     if r.get("type") == "response_item" and r.get("payload", {}).get("role") == "user"]
            assert len(users) == 1 and "Second line stays in one message." in users[0], users
            wait_for(lambda: last_event() == "task_complete", "first reply")
            before = len(sent)
            delivery.drain_codex_agent_messages("offline", "123", metadata(), runtime / "messages.json",
                                                None, {}, sessions_root=home, route_state_path=routes)
            assert any("DIRECT_OK" in str(item[0][2]) for item in sent[before:]), "reply was not forwarded"
            receive("/status")
            assert "idle" in reply(), reply()
            print("PASS start, multiline submit, reply forwarding, idle status", flush=True)

            receive("WAIT then reply")
            wait_for(lambda: submission.codex_session_turn_active(Path(metadata()["codex_session_path"])),
                     "active turn")
            receive("/status")
            assert "working" in reply(), reply()
            record = receive("/interrupt Reply with exactly DIRECT_OK.")
            assert record["action"] == "interrupt", record
            wait_for(lambda: last_event() == "task_complete", "replacement reply")
            assert any(r.get("payload", {}).get("type") == "turn_aborted" for r in records())
            print("PASS working status, interrupt, replacement instruction", flush=True)

            receive("/kill_agent")
            assert "Stopped" in reply(), reply()
            assert host.record(args.target_pane) is None
            receive("/switch codex")
            assert "needs a tmux host" in reply() or "codex CLI not found" in reply(), reply()
            print("PASS stop, and a clear refusal for a harness that needs tmux", flush=True)
        finally:
            host.stop(args.target_pane)


class DirectHostRelayTests(unittest.TestCase):
    def test_listener_drives_claude_without_tmux(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ)
            for name in [key for key in env if key.startswith(("TELEAGENT_", "TMUX"))]:
                env.pop(name)
            env.update(
                TELEAGENT_HOST="direct", TELEAGENT_CLAUDE_BIN=install_fake_claude(root),
                TELEAGENT_REPO=str(root / "repo"), TELEAGENT_SCRATCH=str(root / "scratch"),
                TELEAGENT_LOG_DIR=str(root / "runtime"), TELEAGENT_CODEX_HOME=str(root / "rollouts"),
                TELEAGENT_AGENT_DIR=str(root / "runtime" / "agents"), TELEAGENT_INSTANCE="main",
                TELEAGENT_PERSONALITY_FILE=str(root / "absent-personality"),
                TELEAGENT_SECRET_ENV=str(root / "absent-secrets"),
                TELEAGENT_CODEX_ACCESS_MODE="full-access", PYTHONUTF8="1",
                # Prove nothing reaches for tmux: an empty PATH entry would hide it,
                # so make any tmux call fail loudly instead.
                TMUX="")
            result = subprocess.run([sys.executable, __file__, "--scenario", str(root)], env=env,
                                    text=True, capture_output=True, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("PASS"), 3, result.stdout + result.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        scenario(Path(sys.argv[2]))
    else:
        unittest.main()
