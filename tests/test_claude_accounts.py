"""Native account commands and the private, detached OAuth code handoff."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from teleagent import claude, claude_auth as auth, commands, queue, state

STATE = "s" * 32
CODE = "c" * 32 + "#" + STATE
URL = ("https://claude.com/cai/oauth/authorize?response_type=code&state=" + STATE
       + "&code_challenge=challenge&code_challenge_method=S256")


class ClaudeAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(repo_root=ROOT, session="unused", target_pane="unused:0",
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"), max_log_chars=4000,
            tmux_lines=20, relay_queue_state_path=str(self.root / "queue.json"))
        self.path = auth.path_for(self.args)
        self.binary = self.root / "claude"
        self.binary.write_text("#!/usr/bin/env python3\n" + f'''
import json,sys
from pathlib import Path
marker=Path({str(self.root / 'signed-in')!r})
if sys.argv[1:]==['auth','login']:
    print({URL!r},flush=True)
    if input().strip()!={CODE!r}:raise SystemExit(1)
    marker.write_text('signed in')
elif sys.argv[1:]==['auth','status']:
    print(json.dumps(dict(loggedIn=marker.exists())))
    raise SystemExit(0 if marker.exists() else 1)
elif sys.argv[1:]==['auth','logout']:
    marker.unlink(missing_ok=True)
else:
    # Print mode never runs the interactive /usage command; limits arrive as an event.
    assert sys.stdin.read()!='/usage'
    assert '--no-session-persistence' in sys.argv and '--tools' in sys.argv
    print(json.dumps(dict(type='rate_limit_event',rate_limit_info=dict(status='allowed',
        unifiedWindows=dict(five_hour=dict(utilization=.17,resetsAt=1900000000,status='allowed'),
                            seven_day=dict(utilization=.4,resetsAt=1900500000,status='allowed'))))))
    print(json.dumps(dict(type='result',subtype='success',is_error=False,result='account@example.com')))
''')
        self.binary.chmod(0o755)
        env = mock.patch.dict(os.environ, TELEAGENT_CLAUDE_BIN=str(self.binary))
        env.start(); self.addCleanup(env.stop)
        sender = mock.patch.object(auth.replies, "send")
        self.send = sender.start(); self.addCleanup(sender.stop)
        self.addCleanup(auth.cancel_login, self.path)

    def update(self, text, chat="123", sender="123", number=1):
        return {"update_id": number, "message": {"message_id": number, "date": int(time.time()),
            "text": text, "chat": {"id": chat, "type": "private"}, "from": {"id": sender}}}

    def wait_phase(self, phase):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = state.read_json_object(self.path)
            if data.get("phase") == phase:
                return data
            time.sleep(.02)
        self.fail("Login did not reach " + phase)

    def command(self, text, native=True):
        with mock.patch.object(commands._processes, "tmux_pane_has_claude_process", return_value=native):
            return commands.handle_update(self.update(text), self.args, {}, "offline", "123",
                                          self.root / "listener.jsonl")

    def test_detached_login_private_handoff_and_native_account_commands(self):
        self.command("/claude_auth login", native=False)
        waiting = self.wait_phase("awaiting_code")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        auth.notify_progress(self.args, "offline", "123")
        self.assertIn(URL, self.send.call_args.args[-1])
        # Wrong owner, group and OAuth state cannot supply a credential.
        for update in [self.update(CODE, sender="999"), self.update(CODE, chat="-123"),
                       self.update("x" * 32 + "#" + "wrong" * 8)]:
            self.assertTrue(auth.consume_code_update(update, self.args, "offline", "123"))
            self.assertEqual(state.read_json_object(self.path)["phase"], "awaiting_code")
            self.assertNotIn(CODE, json.dumps(update))
        good = self.update("/claude_code " + CODE, number=4)
        # Bypass agent/FIFO even while the agent would otherwise be busy.
        with mock.patch.object(queue._commands, "handle_update") as normal:
            self.assertEqual(queue.dispatch_telegram_update(good, self.args, {}, "offline", "123",
                                                           self.root / "listener.jsonl"), "handled")
            normal.assert_not_called()
        self.wait_phase("authenticated")
        self.assertFalse(auth.code_path(self.path, waiting["attempt_id"]).exists())
        self.assertNotIn(CODE, self.path.read_text())
        self.assertNotIn(CODE, json.dumps(good))
        self.assertFalse((self.root / "queue.json").exists())
        auth.notify_progress(self.args, "offline", "123")
        self.assertEqual(self.send.call_args.args[-1], "Claude sign-in complete.")
        self.command("/claude_auth status")
        self.assertEqual(self.send.call_args.args[-1], "Claude Code signed in")
        self.command("/usage")
        self.assertIn("5-hour window: 17% used, resets", self.send.call_args.args[-1])
        self.assertIn("7-day window: 40% used", self.send.call_args.args[-1])
        # Only the limit event is reported, never the model's own reply.
        self.assertNotIn("account@example.com", self.send.call_args.args[-1])
        self.command("/claude_usage", native=False)
        self.assertIn("17%", self.send.call_args.args[-1])
        self.command("/claude_auth logout")
        self.assertIn("signed out", self.send.call_args.args[-1])
        self.assertIn("sign-in required", claude.authentication_status())
        self.assertNotIn(CODE, (self.root / "listener.jsonl").read_text())

    def test_reauth_cancel_expiry_and_late_code(self):
        self.command("/reauth")
        self.wait_phase("awaiting_code")
        self.command("/claude_auth cancel")
        self.assertEqual(state.read_json_object(self.path)["phase"], "cancelled")
        late = self.update(CODE)
        self.assertTrue(auth.consume_code_update(late, self.args, "offline", "123"))
        self.assertNotIn(CODE, json.dumps(late))
        attempt = "timeout-fixture"
        state.write_json_object(self.path, dict(attempt_id=attempt, phase="starting"))
        result = subprocess.run([sys.executable, "-c",
            "import sys;sys.path.insert(0,'scripts');from claude_auth_worker import run;"
            "from pathlib import Path;raise SystemExit(run(Path(sys.argv[1]),sys.argv[2],sys.argv[3],.1))",
            str(self.path), attempt, str(self.binary)], cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state.read_json_object(self.path)["phase"], "failed")

    def test_quoted_codes_and_unsafe_urls(self):
        update = self.update("hello")
        update["message"]["reply_to_message"] = {"text": CODE}
        self.assertFalse(auth.consume_code_update(update, self.args, "offline", "123"))
        self.assertNotIn(CODE, json.dumps(update))
        for url in [URL.replace("claude.com", "evil.example"), URL + "&access_token=secret",
                    URL.replace("https:", "http:")]:
            self.assertFalse(auth.validated_url(url))
        with mock.patch.object(claude.subprocess, "run", return_value=subprocess.CompletedProcess([], 1,
                 stdout='{"result":"secret credential in error","is_error":true}', stderr="secret")):
            with self.assertRaisesRegex(RuntimeError, "no cached"):
                claude.usage()
        # A turn that reports no limit window is a failure, not an empty report.
        with mock.patch.object(claude.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                 stdout='{"type":"result","subtype":"success","result":"OK"}\n', stderr="")):
            with self.assertRaisesRegex(RuntimeError, "no cached"):
                claude.usage()

    def test_usage_reads_fables_window_and_falls_back_to_haiku(self):
        windows = dict(five_hour=dict(utilization=.01, resetsAt=1900000000, status='allowed'),
                       seven_day=dict(utilization=.47, resetsAt=1900500000, status='allowed'),
                       seven_day_overage_included=dict(utilization=.92, resetsAt=1900500000,
                                                       status='allowed'))
        fable = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(
            type='rate_limit_event', rate_limit_info=dict(unifiedWindows=windows))) + "\n", stderr="")
        with mock.patch.object(claude.subprocess, "run", return_value=fable) as run:
            report = claude.usage()
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--model") + 1], "fable")
        self.assertIn("Fable 7-day window: 92% used", report)
        self.assertIn("7-day window: 47% used", report)
        unavailable = subprocess.CompletedProcess([], 1, stdout="", stderr="no access")
        haiku = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(
            type='rate_limit_event', rate_limit_info=dict(unifiedWindows=dict(
                five_hour=windows["five_hour"])))) + "\n", stderr="")
        with mock.patch.object(claude.subprocess, "run", side_effect=[unavailable, haiku]) as run:
            self.assertIn("5-hour window: 1% used", claude.usage())
        self.assertEqual([c.args[0][c.args[0].index("--model") + 1] for c in run.call_args_list],
                         ["fable", "haiku"])

    def test_rate_limit_report_formats_only_known_safe_fields(self):
        report = claude.format_rate_limits({"rateLimitType": "five_hour", "resetsAt": 1900000000,
            "status": "allowed_warning", "utilization": .91, "isUsingOverage": True,
            "orgId": "org-private"})
        self.assertIn("5-hour window: 91% used, resets", report)
        self.assertIn("(allowed warning)", report)
        self.assertIn("Extra usage is being consumed.", report)
        self.assertNotIn("org-private", report)
        with self.assertRaises(ValueError):
            claude.format_rate_limits({"status": "allowed"})
