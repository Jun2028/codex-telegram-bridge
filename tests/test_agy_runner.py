"""Tests for the agy harness runner, session records and model wiring."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agy_agent_runner  # noqa: E402
import telegram_agent_registry  # noqa: E402
import telegram_inbox  # noqa: E402
from teleagent import delivery as _delivery  # noqa: E402
from teleagent import models as _models  # noqa: E402
from teleagent import processes as _processes  # noqa: E402
from teleagent import sessions as _sessions  # noqa: E402
from teleagent import settings as _settings  # noqa: E402


class MessageAssemblerTests(unittest.TestCase):
    def test_burst_submits_once_idle(self) -> None:
        assembler = agy_agent_runner.MessageAssembler()
        assembler.feed("line one")
        assembler.feed("line two")
        self.assertEqual(assembler.take(), "line one\nline two")

    def test_embedded_blank_line_is_message_content(self) -> None:
        assembler = agy_agent_runner.MessageAssembler()
        assembler.feed("para one")
        assembler.feed("")
        assembler.feed("para two")
        self.assertEqual(assembler.take(), "para one\n\npara two")

    def test_take_returns_none_without_input(self) -> None:
        assembler = agy_agent_runner.MessageAssembler()
        self.assertIsNone(assembler.take())


class SessionLogTests(unittest.TestCase):
    def test_records_are_forwarded_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = agy_agent_runner.SessionLog(Path(tmp), Path("/repo"))
            log.turn_context("gemini-3.8-flash", "high")
            log.agent_message("hello from agy", phase="final_answer")
            records = [
                json.loads(line)
                for line in log.path.read_text(encoding="utf-8").splitlines()
            ]
            meta = _sessions.codex_session_metadata(log.path)
            forwarded = [_delivery.codex_agent_message(record) for record in records]
            current = _models.codex_session_model_and_reasoning_effort(log.path)
            log.close()

        self.assertEqual(meta.get("cwd"), "/repo")
        self.assertIn(("hello from agy", "final_answer"), forwarded)
        self.assertEqual(current, ("gemini-3.8-flash", "high"))


class AgyModelValidationTests(unittest.TestCase):
    def test_aliases_and_ids_are_accepted(self) -> None:
        for name in (
            "g-flash",
            "argon",
            "sonnet",
            "opus",
            "oss",
            "gemini-3.8-flash",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b",
        ):
            self.assertTrue(_settings.is_agy_model(name), name)
            self.assertEqual(telegram_inbox.normalize_codex_agent_model(name), name)

    def test_launch_payload_defaults_to_model_level(self) -> None:
        model, effort, explicit = telegram_inbox.parse_agent_launch_payload("g-flash")
        self.assertEqual((model, effort, explicit), ("g-flash", "", True))

    def test_launch_payload_accepts_explicit_agy_effort(self) -> None:
        model, effort, _ = telegram_inbox.parse_agent_launch_payload("g-flash high")
        self.assertEqual((model, effort), ("g-flash", "high"))

    def test_launch_payload_rejects_unsupported_agy_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "agy reasoning"):
            telegram_inbox.parse_agent_launch_payload("g-flash minimal")

    def test_registry_treats_codex_agy_as_process_layer(self) -> None:
        rows = [(10, 1, "bash"), (11, 10, "codex-agy"), (12, 11, "python3")]
        self.assertEqual(telegram_agent_registry.closest_codex_pids(10, rows), [11])


class AgyLiveSwitchGuidanceTests(unittest.TestCase):
    def test_live_switch_to_agy_model_directs_to_restart(self) -> None:
        with (
            mock.patch.object(_processes, "tmux_target_exists", return_value=True),
            mock.patch.object(
                _processes, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(_processes, "tmux_pane_has_agy_process", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "restart_agent"):
                telegram_inbox.set_codex_model("tele-agent:codex.0", "g-flash")

    def test_live_switch_inside_agy_pane_is_blocked(self) -> None:
        with (
            mock.patch.object(_processes, "tmux_target_exists", return_value=True),
            mock.patch.object(
                _processes, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(_processes, "tmux_pane_has_agy_process", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "agy"):
                telegram_inbox.set_codex_model("tele-agent:codex.0", "astra")


if __name__ == "__main__":
    unittest.main()
