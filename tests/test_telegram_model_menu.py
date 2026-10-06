"""Regression tests for live model switching against newer Codex CLI menus.

Codex 0.160 renders model menus with capitalized display names
(``GPT-6.1-Sol``) while older CLIs used lowercase slugs (``gpt-6.1-sol``).
The relay must accept both.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402
from teleagent import models as _relay_models  # noqa: E402
from teleagent import processes as _relay_processes  # noqa: E402


DISPLAY_NAME_MENU = (
    "  Select Model and Effort\n"
    "  Access legacy models by running codex -m <model_name> or in your config.toml\n"
    "\n"
    "  1. GPT-6.1-Sol (default)  Latest workhorse model for coding and everyday work.\n"
    "› 2. GPT-6-Astra (current)  Frontier intelligence for the most demanding work.\n"
    "  3. GPT-6-Sol              Previous generation workhorse model.\n"
    "  4. GPT-6-Luna             Fast and affordable model for easier tasks.\n"
    "  5. GPT-5.6-Sol            Older generation workhorse model.\n"
)

SLUG_MENU = (
    "  Select Model and Effort\n"
    "› 1. gpt-5.6-sol (current)\n"
    "  2. gpt-6-astra\n"
)

LEGACY_MENU_WITHOUT_SOL_61 = (
    "  Select Model and Effort\n"
    "› 1. gpt-6-astra (current)\n"
    "  2. gpt-5.6-sol\n"
    "  3. gpt-5.6-luna\n"
)


class TelegramModelMenuTests(unittest.TestCase):
    def _selector_text(self, menu: str):
        def selector_text(_target: str, expected: str, timeout: float = 5.0) -> str:
            if expected == "Select Model and Effort":
                return menu
            return expected

        return selector_text

    def test_display_name_menu_switches_latest_to_sol_61(self) -> None:
        with (
            mock.patch.object(_relay_processes, "tmux_target_exists", return_value=True),
            mock.patch.object(
                _relay_processes, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(_relay_processes, "tmux_send_keys") as send_keys,
            mock.patch.object(
                _relay_processes,
                "wait_for_tmux_text",
                side_effect=self._selector_text(DISPLAY_NAME_MENU),
            ) as wait_text,
            mock.patch.object(
                _relay_models,
                "current_codex_model_and_reasoning_effort",
                return_value=("gpt-6.1-sol", "high"),
            ),
        ):
            selected = telegram_inbox.set_codex_model("tele-agent:codex.0", "latest")

        self.assertEqual(selected, ("gpt-6.1-sol", "high"))
        self.assertEqual(
            send_keys.call_args_list.count(mock.call("tele-agent:codex.0", "Down")),
            2,  # the reasoning menu moves from Low to High
        )
        self.assertIn(
            mock.call("tele-agent:codex.0", "Select Reasoning Level for"),
            wait_text.call_args_list,
        )

    def test_slug_menu_still_switches_astra(self) -> None:
        with (
            mock.patch.object(_relay_processes, "tmux_target_exists", return_value=True),
            mock.patch.object(
                _relay_processes, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(_relay_processes, "tmux_send_keys") as send_keys,
            mock.patch.object(
                _relay_processes,
                "wait_for_tmux_text",
                side_effect=self._selector_text(SLUG_MENU),
            ),
            mock.patch.object(
                _relay_models,
                "current_codex_model_and_reasoning_effort",
                return_value=("gpt-6-astra", "medium"),
            ),
        ):
            selected = telegram_inbox.set_codex_model("tele-agent:codex.0", "astra")

        self.assertEqual(selected, ("gpt-6-astra", "medium"))
        # one Down for model entry 2, one Down for reasoning Medium
        self.assertEqual(
            send_keys.call_args_list.count(mock.call("tele-agent:codex.0", "Down")),
            2,
        )

    def test_menu_without_requested_model_still_fails(self) -> None:
        with (
            mock.patch.object(_relay_processes, "tmux_target_exists", return_value=True),
            mock.patch.object(
                _relay_processes, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(_relay_processes, "tmux_send_keys"),
            mock.patch.object(
                _relay_processes,
                "wait_for_tmux_text",
                side_effect=self._selector_text(LEGACY_MENU_WITHOUT_SOL_61),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "does not offer gpt-6.1-sol"
            ):
                telegram_inbox.set_codex_model("tele-agent:codex.0", "latest")

    def test_status_line_reads_capitalized_display_name(self) -> None:
        with mock.patch.object(
            _relay_processes,
            "tmux_tail",
            return_value=(
                "  GPT-6-Astra max · /nfs/home/svu/test · Goal paused\n"
            ),
        ):
            current = telegram_inbox.current_codex_model_and_reasoning_effort(
                "tele-agent:codex.0"
            )

        self.assertEqual(current, ("gpt-6-astra", "max"))

    def test_status_line_reads_deepseek_display_name(self) -> None:
        with mock.patch.object(
            _relay_processes,
            "tmux_tail",
            return_value="  DeepSeek-Flash max · ~/tele-agent\n",
        ):
            current = telegram_inbox.current_codex_model_and_reasoning_effort(
                "ds-goal:0.0"
            )

        self.assertEqual(current, ("deepseek-flash", "max"))


if __name__ == "__main__":
    unittest.main()
