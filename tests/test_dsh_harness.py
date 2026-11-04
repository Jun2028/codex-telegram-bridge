"""Tests for the native DeepSeek Harness integration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
EXECUTABLE_TEST_PARENT = Path(
    os.environ.get("TELEAGENT_TEST_TMPDIR", str(Path.home() / ".ta-test"))
)

import dsh_agent_runner  # noqa: E402
import telegram_agent_registry  # noqa: E402
import telegram_inbox  # noqa: E402
from teleagent import dsh as _dsh  # noqa: E402
from teleagent import lifecycle as _lifecycle  # noqa: E402
from teleagent import processes as _processes  # noqa: E402
from teleagent import submission as _submission  # noqa: E402
from teleagent import transport as _transport  # noqa: E402


class DshPayloadTests(unittest.TestCase):
    def test_defaults_to_flash_high(self) -> None:
        self.assertEqual(
            _dsh.parse_launch_payload(""),
            (_dsh.DSH_FLASH_AGENT_MODEL, "max"),
        )

    def test_accepts_model_and_effort(self) -> None:
        self.assertEqual(
            _dsh.parse_launch_payload("pro max"),
            (_dsh.DSH_PRO_AGENT_MODEL, "max"),
        )
        self.assertEqual(
            _dsh.parse_launch_payload("flash low"),
            (_dsh.DSH_FLASH_AGENT_MODEL, "low"),
        )

    def test_rejects_prompt_text_as_third_argument(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most"):
            _dsh.parse_launch_payload("flash max do work")

    def test_command_uses_headless_json_and_stdin(self) -> None:
        command = _dsh.build_turn_command(
            dsh_bin="/opt/dsh",
            overlay_path=Path("/tmp/model.yml"),
            session_id="session-test",
        )
        self.assertEqual(
            command[:5],
            ["/opt/dsh", "--profile", "headless", "--patch", "/tmp/model.yml"],
        )
        self.assertIn("--json", command)
        self.assertEqual(command[-3:], ["--session-id", "session-test", "-"])

    def test_overlay_selects_native_model_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            _dsh.write_model_overlay(path, _dsh.DSH_PRO_AGENT_MODEL, "max")
            text = path.read_text(encoding="utf-8")
        self.assertIn('provider: deepseek-official', text)
        self.assertIn('model: "deepseek-v4-pro"', text)
        self.assertIn('reasoningEffort: "max"', text)
        self.assertNotIn("API_KEY", text)


class DshProtocolTests(unittest.TestCase):
    def test_run_turn_parses_session_and_lossless_final(self) -> None:
        EXECUTABLE_TEST_PARENT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=EXECUTABLE_TEST_PARENT) as tmp:
            root = Path(tmp)
            executable = root / "fake-dsh"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "prompt = sys.stdin.read()\n"
                "print(json.dumps({'type': 'session', 'sessionId': 'session-test'}))\n"
                "print(json.dumps({'type': 'tool_call', 'tool': 'read'}))\n"
                "print(json.dumps({'type': 'final', 'text': 'answer:' + prompt}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            overlay = root / "model.yml"
            overlay.write_text("[]\n", encoding="utf-8")
            result, returncode, stderr = _dsh.run_turn(
                prompt="hello",
                dsh_bin=str(executable),
                overlay_path=overlay,
                env=dict(os.environ),
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(result.session_id, "session-test")
        self.assertEqual(result.text, "answer:hello")

    def test_launcher_rejects_chat_only_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "repo"
            (fixture / "config").mkdir(parents=True)
            (fixture / "config" / "relay.env").write_text("", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "TELEAGENT_REPO": str(fixture),
                    "TELEAGENT_INSTANCE": "main",
                    "TELEAGENT_SCRATCH": str(Path(tmp) / "scratch"),
                    "TELEAGENT_CODEX_HOME": str(Path(tmp) / "codex-home"),
                    "TELEAGENT_CODEX_MODEL": "dsh-flash",
                    "TELEAGENT_CODEX_ACCESS_MODE": "chat-only",
                }
            )
            result = subprocess.run(
                [str(ROOT / "scripts" / "start_codex_agent.sh"), "--restart"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires full-access", result.stderr)


class DshSessionTests(unittest.TestCase):
    def test_session_log_avoids_dsh_flat_file_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "sessions" / "dsh" / "rollout-dsh-old.jsonl"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}\n", encoding="utf-8")

            log = dsh_agent_runner.SessionLog(root, Path("/repo"))
            path = log.path
            moved = path.parent / legacy.name
            log.close()

            self.assertEqual(path.parent, legacy.parent / "telegram")
            self.assertTrue(moved.is_file())
            self.assertFalse(legacy.exists())

    def test_records_confirm_submission_and_turn_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = dsh_agent_runner.SessionLog(root, Path("/repo"))
            marker = "[TELEGRAM USER MESSAGE message_id=42"
            log.turn_context("deepseek-flash", "high")
            log.turn_started(marker + " route_id=u1] do work")

            self.assertTrue(_submission.codex_session_turn_active(log.path))
            self.assertTrue(
                _submission.wait_for_codex_submission(
                    (log.path, 0), marker, timeout=0
                )
            )

            log.agent_message("done", phase="final_answer")
            log.turn_finished()
            self.assertFalse(_submission.codex_session_turn_active(log.path))
            records = [
                json.loads(line)
                for line in log.path.read_text(encoding="utf-8").splitlines()
            ]
            log.close()
        self.assertEqual(records[-2]["payload"]["message"], "done")
        self.assertEqual(records[-1]["payload"]["type"], "task_complete")

    def test_registry_recognizes_dsh_runner(self) -> None:
        rows = [(10, 1, "bash"), (11, 10, "codex-dsh"), (12, 11, "node")]
        self.assertEqual(telegram_agent_registry.closest_codex_pids(10, rows), [11])


class DshCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        active = mock.patch.object(
            telegram_inbox.agent_registry,
            "active_agent_for_pane",
            return_value=None,
        )
        active.start()
        self.addCleanup(active.stop)
        pane = mock.patch.object(
            _processes, "tmux_tail", return_value="DeepSeek Harness ready"
        )
        pane.start()
        self.addCleanup(pane.stop)

    def _args(self) -> argparse.Namespace:
        state = self.root / "usage.json"
        return argparse.Namespace(
            codex_usage_state_path=str(state),
            codex_reset_state_path=str(self.root / "reset.json"),
            agent_lifecycle_state_path=str(self.root / "lifecycle.json"),
            repo_root=self.root,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )

    def test_dsh_command_restarts_with_native_harness(self) -> None:
        update = {
            "update_id": 100,
            "message": {
                "message_id": 101,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/dsh pro max",
            },
        }
        with (
            mock.patch.object(
                _lifecycle,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "started",
                    {"agent_id": "dsh-agent"},
                ),
            ) as start_agent,
            mock.patch.object(_transport, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update,
                self._args(),
                {},
                "token",
                "123",
                self.root / "listener.jsonl",
            )

        call = start_agent.call_args.kwargs
        self.assertTrue(call["restart"])
        self.assertEqual(call["model"], _dsh.DSH_PRO_AGENT_MODEL)
        self.assertEqual(call["reasoning_effort"], "max")
        reply = send_reply.call_args.args[2]
        self.assertIn("DeepSeek Harness started", reply)
        self.assertIn("model=deepseek-v4-pro", reply)
        self.assertIn("fresh session", reply)


if __name__ == "__main__":
    unittest.main()
