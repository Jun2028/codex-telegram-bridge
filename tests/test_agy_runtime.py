"""Unit tests for the Antigravity CLI (agy) harness support."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from teleagent import agy as _agy  # noqa: E402


MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


class ModelTableTests(unittest.TestCase):
    def test_parse_models_table(self) -> None:
        models = _agy.parse_models_table(MODELS_OUTPUT)
        self.assertEqual(len(models), 6)
        self.assertEqual(models[0], ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"))
        self.assertEqual(models[-1], ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"))

    def test_resolve_g_flash_picks_newest_base_model(self) -> None:
        models = _agy.parse_models_table(
            MODELS_OUTPUT
            + "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
            + "gemini-4.0-flash-high\tGemini 4.0 Flash (High)\n"
        )
        self.assertEqual(_agy.resolve_model_id("g-flash", models), "gemini-4.0-flash")

    def test_resolve_g_flash_strips_level_suffix(self) -> None:
        models = _agy.parse_models_table(MODELS_OUTPUT)
        self.assertEqual(_agy.resolve_model_id("g-flash", models), "gemini-3.8-flash")

    def test_resolve_argon_uses_listed_model(self) -> None:
        models = _agy.parse_models_table(
            MODELS_OUTPUT + "gemini-4-argon\tGemini 4 Argon\n"
        )
        self.assertEqual(_agy.resolve_model_id("argon", models), "gemini-4-argon")

    def test_resolve_argon_unavailable_on_this_account(self) -> None:
        models = _agy.parse_models_table(MODELS_OUTPUT)
        with self.assertRaisesRegex(_agy.AgyModelUnavailable, "argon"):
            _agy.resolve_model_id("argon", models)

    def test_resolve_short_aliases_for_other_families(self) -> None:
        models = _agy.parse_models_table(MODELS_OUTPUT)
        self.assertEqual(
            _agy.resolve_model_id("sonnet", models), "claude-sonnet-4-6"
        )
        self.assertEqual(
            _agy.resolve_model_id("opus", models), "claude-opus-4-6-thinking"
        )
        # Base names carry no thinking level; --effort or the server default
        # chooses it.
        self.assertEqual(_agy.resolve_model_id("oss", models), "gpt-oss-120b")

    def test_explicit_model_id_passes_through(self) -> None:
        models = _agy.parse_models_table(MODELS_OUTPUT)
        self.assertEqual(
            _agy.resolve_model_id("gemini-3.8-flash-low", models),
            "gemini-3.8-flash-low",
        )


class TurnCommandTests(unittest.TestCase):
    def test_build_turn_command(self) -> None:
        command = _agy.build_turn_command(
            prompt="hello",
            model="gemini-3.8-flash",
            agy_bin="/opt/agy",
            effort="medium",
            conversation_id="abc-123",
        )
        self.assertEqual(command[0], "/opt/agy")
        self.assertIn("stream-json", command)
        self.assertIn("--conversation", command)
        self.assertIn("abc-123", command)
        self.assertIn("--effort", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_build_turn_command_without_optional_flags(self) -> None:
        command = _agy.build_turn_command(
            prompt="hello", model="gemini-3.8-flash", agy_bin="agy",
            skip_permissions=False,
        )
        self.assertNotIn("--conversation", command)
        self.assertNotIn("--effort", command)
        self.assertNotIn("--dangerously-skip-permissions", command)


class StreamEventTests(unittest.TestCase):
    def _fold(self, lines: list[str]) -> _agy.AgyTurnResult:
        result = _agy.AgyTurnResult()
        deltas: list[str] = []
        for event in _agy.iter_stream_events(lines):
            _agy.apply_stream_event(result, event, deltas)
        if not result.text and deltas:
            result.text = "".join(deltas).strip()
        if not result.status:
            result.status = "SUCCESS"
        return result

    def test_stream_accumulates_final_response(self) -> None:
        lines = [
            '{"event":"init","conversation_id":"c-1","init":{"model":"gemini-3.8-flash"}}',
            '{"event":"step_update","step_update":{"state":"ACTIVE","step_type":"agent_response","text_delta":"Hel"}}',
            '{"event":"step_update","step_update":{"state":"DONE","step_type":"agent_response","text_delta":"lo"}}',
            '{"event":"result","result":{"conversation_id":"c-1","status":"SUCCESS","response":"Hello\\n"}}',
        ]
        result = self._fold(lines)
        self.assertEqual(result.conversation_id, "c-1")
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.text, "Hello\n")

    def test_stream_falls_back_to_text_deltas(self) -> None:
        lines = [
            '{"event":"init","conversation_id":"c-2"}',
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"partial answer"}}',
        ]
        result = self._fold(lines)
        self.assertEqual(result.conversation_id, "c-2")
        self.assertEqual(result.text, "partial answer")

    def test_stream_ignores_non_json_noise(self) -> None:
        events = list(_agy.iter_stream_events(["diagnostic line", "", '{"event":"init"}']))
        self.assertEqual(events, [{"event": "init"}])


if __name__ == "__main__":
    unittest.main()
