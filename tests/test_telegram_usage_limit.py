from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402


class TelegramUsageLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        telegram_inbox.REGISTERED_CODEX_PID_CACHE.clear()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.sessions = self.root / "sessions"
        self.repo.mkdir()
        self.sessions.mkdir()
        self.session = self.sessions / "rollout.jsonl"
        self.state = self.root / "usage.state.json"
        self.meta = {
            "agent_id": "agent-test",
            "repo_root": str(self.repo),
            "codex_session_path": str(self.session),
            "codex_session_detection": "process_fd",
        }
        self._write_records(
            {
                "type": "session_meta",
                "timestamp": "2026-07-17T00:00:00Z",
                "payload": {"cwd": str(self.repo)},
            }
        )

    def _write_records(self, *records: dict) -> None:
        with self.session.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def _rate_event(used: float, reached_type=None, resets_at: int = 2_000_000_000) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {"used_percent": 40.0, "resets_at": resets_at - 100},
                    "secondary": {"used_percent": used, "resets_at": resets_at},
                    "rate_limit_reached_type": reached_type,
                },
            },
        }

    def _refresh(self, now: int = 1_900_000_000) -> dict:
        return telegram_inbox.refresh_codex_usage_state(
            self.meta,
            self.state,
            sessions_root=self.sessions,
            now=now,
            tail_bytes=1024 * 1024,
        )

    def test_safe_local_path_honors_configured_fragments(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TELEAGENT_BLOCKED_PATH_FRAGMENTS": "/shared:/group-storage"},
        ):
            with self.assertRaises(SystemExit):
                telegram_inbox.assert_safe_local_path(
                    "/shared/relay/secret.env"
                )
            telegram_inbox.assert_safe_local_path(
                "/home/operator/relay/secret.env"
            )

    def test_codex_target_ready_treats_tmux_timeout_as_transient(self) -> None:
        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=False,
            ),
            mock.patch.object(
                telegram_inbox,
                "tmux_target_exists",
                side_effect=subprocess.TimeoutExpired(["tmux"], 5),
            ),
        ):
            self.assertFalse(
                telegram_inbox.codex_target_ready("tele-agent:codex.0")
            )

    def test_registered_codex_process_uses_session_fd_without_tmux(self) -> None:
        meta = {"codex_session_path": str(self.session)}
        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "ps_rows",
                return_value=[(321, 1, "codex")],
            ) as ps_rows,
            mock.patch.object(
                telegram_inbox,
                "process_has_codex_session",
                return_value=True,
            ),
        ):
            self.assertTrue(
                telegram_inbox.registered_codex_process_running(
                    "tele-agent:codex.0"
                )
            )
            self.assertTrue(
                telegram_inbox.registered_codex_process_running(
                    "tele-agent:codex.0"
                )
            )
        ps_rows.assert_called_once()

    def test_tmux_relay_uses_registered_codex_and_long_mutation_timeout(self) -> None:
        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists") as target_probe,
            mock.patch.object(telegram_inbox.subprocess, "run") as run,
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                "hello",
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0,
            )

        target_probe.assert_not_called()
        self.assertEqual(run.call_count, 4)
        self.assertTrue(
            all(
                call.kwargs["timeout"]
                == telegram_inbox.TMUX_MUTATION_TIMEOUT_SECONDS
                for call in run.call_args_list
            )
        )
        load_call, paste_call = run.call_args_list[1:3]
        self.assertEqual(load_call.args[0][0:2], ["tmux", "load-buffer"])
        self.assertEqual(load_call.kwargs["input"], "hello")
        self.assertTrue(load_call.kwargs["text"])
        self.assertEqual(paste_call.args[0][0:2], ["tmux", "paste-buffer"])
        self.assertIn("-p", paste_call.args[0])
        self.assertIn("pane command: codex", result)

    def test_ensure_target_accepts_registered_codex_without_tmux_probe(self) -> None:
        args = argparse.Namespace(
            relay_mode="tmux-enter",
            target_pane="tele-agent:codex.0",
        )
        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists") as target_probe,
        ):
            result = telegram_inbox.ensure_codex_target_for_agent_message(args, {})

        self.assertIsNone(result)
        target_probe.assert_not_called()

    def test_watchdog_does_not_restart_agent_after_tmux_timeout(self) -> None:
        args = argparse.Namespace(
            agent_watchdog=True,
            relay_mode="tmux-enter",
            target_pane="tele-agent:codex.0",
            session="tele-agent",
            codex_window="codex",
        )
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=False,
            ),
            mock.patch.object(
                telegram_inbox,
                "tmux_target_exists",
                side_effect=subprocess.TimeoutExpired(["tmux"], 5),
            ),
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
        ):
            result = telegram_inbox.maintain_managed_codex_agent(args, log_path)

        self.assertIsNone(result)
        start_agent.assert_not_called()
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["event"], "codex_watchdog_tmux_probe_deferred")
        self.assertEqual(record["error_type"], "TimeoutExpired")

    def test_format_agent_message_includes_full_replied_text(self) -> None:
        message = {
            "message_id": 22,
            "date": 1_900_000_100,
            "from": {"id": 456, "username": "tester"},
            "text": "Please do that",
            "reply_to_message": {
                "message_id": 21,
                "date": 1_900_000_000,
                "from": {"id": 789, "first_name": "Relay Bot"},
                "text": "First line of the original message.\nSecond line stays intact.",
            },
        }

        formatted = telegram_inbox.format_agent_message(message, message["text"])

        self.assertIn("[TELEGRAM USER MESSAGE message_id=22", formatted)
        self.assertIn("[REPLIED-TO TELEGRAM MESSAGE message_id=21", formatted)
        self.assertIn("Relay Bot | 789", formatted)
        self.assertIn(
            "First line of the original message.\nSecond line stays intact.",
            formatted,
        )
        self.assertIn("[/REPLIED-TO TELEGRAM MESSAGE]", formatted)

    def test_format_agent_message_uses_replied_caption(self) -> None:
        message = {
            "message_id": 24,
            "from": {"id": 456},
            "text": "This one",
            "reply_to_message": {
                "message_id": 23,
                "from": {"id": 789},
                "caption": "Complete media caption",
            },
        }

        formatted = telegram_inbox.format_agent_message(message, message["text"])

        self.assertIn("Complete media caption", formatted)

    def test_parse_agent_launch_payload_accepts_optional_reasoning_only(self) -> None:
        model, reasoning, explicit = telegram_inbox.parse_agent_launch_payload("max")

        self.assertEqual(model, telegram_inbox.DEFAULT_CODEX_AGENT_MODEL)
        self.assertEqual(reasoning, "max")
        self.assertTrue(explicit)

    def test_parse_agent_launch_payload_accepts_explicit_spark(self) -> None:
        model, reasoning, explicit = telegram_inbox.parse_agent_launch_payload(
            "spark xhigh"
        )

        self.assertEqual(model, "gpt-5.3-codex-spark")
        self.assertEqual(reasoning, "xhigh")
        self.assertTrue(explicit)

    def test_parse_live_model_payload_accepts_ds_flash(self) -> None:
        model, reasoning = telegram_inbox.parse_live_model_payload("ds-flash")

        self.assertEqual(model, "deepseek-v4-flash")
        self.assertEqual(reasoning, "max")

    def test_parse_live_model_payload_accepts_full_deepseek_flash(self) -> None:
        model, reasoning = telegram_inbox.parse_live_model_payload(
            "deepseek-v4-flash max"
        )

        self.assertEqual(model, "deepseek-v4-flash")
        self.assertEqual(reasoning, "max")

    def test_parse_live_model_payload_rejects_non_max_ds_flash(self) -> None:
        with self.assertRaisesRegex(ValueError, "DeepSeek Flash reasoning"):
            telegram_inbox.parse_live_model_payload("ds-flash high")

    def test_parse_agent_launch_payload_accepts_ds_flash_with_max(self) -> None:
        model, reasoning, explicit = telegram_inbox.parse_agent_launch_payload(
            "ds-flash"
        )

        self.assertEqual(model, "deepseek-v4-flash")
        self.assertEqual(reasoning, "max")
        self.assertTrue(explicit)

    def test_parse_agent_launch_payload_rejects_non_max_ds_flash(self) -> None:
        with self.assertRaisesRegex(ValueError, "DeepSeek Flash reasoning"):
            telegram_inbox.parse_agent_launch_payload("ds-flash high")

    def test_current_codex_model_detects_deepseek_flash(self) -> None:
        with mock.patch.object(
            telegram_inbox,
            "tmux_tail",
            return_value=(
                "Current: deepseek-v4-flash max\n"
            ),
        ):
            current = telegram_inbox.current_codex_model_and_reasoning_effort(
                "ds-goal:0.0"
            )

        self.assertEqual(current, ("deepseek-v4-flash", "max"))

    def test_parse_agent_launch_payload_keeps_latest_as_default(self) -> None:
        model, reasoning, explicit = telegram_inbox.parse_agent_launch_payload("latest")

        self.assertEqual(model, telegram_inbox.DEFAULT_CODEX_AGENT_MODEL)
        self.assertEqual(reasoning, telegram_inbox.DEFAULT_CODEX_AGENT_REASONING_EFFORT)
        self.assertTrue(explicit)

    def test_parse_agent_launch_payload_rejects_unsupported_spark_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "Spark reasoning"):
            telegram_inbox.parse_agent_launch_payload("spark max")

    def test_parse_agent_launch_payload_uses_defaults_when_empty(self) -> None:
        model, reasoning, explicit = telegram_inbox.parse_agent_launch_payload("")

        self.assertEqual(model, telegram_inbox.DEFAULT_CODEX_AGENT_MODEL)
        self.assertEqual(reasoning, telegram_inbox.DEFAULT_CODEX_AGENT_REASONING_EFFORT)
        self.assertFalse(explicit)

    def test_parse_agent_launch_payload_rejects_prompts(self) -> None:
        for payload in (
            "continue",
            "max -- continue carefully",
            "--prompt continue",
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                telegram_inbox.parse_agent_launch_payload(payload)

    def test_top_level_functions_do_not_shadow_module_callables(self) -> None:
        tree = ast.parse(
            (ROOT / "scripts" / "telegram_inbox.py").read_text(encoding="utf-8")
        )
        module_callables = {
            node.name
            for node in tree.body
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
        }
        collisions = {}
        for function in (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            local_assignments = {
                node.id
                for node in ast.walk(function)
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, (ast.Store, ast.Del))
            }
            overlap = (module_callables & local_assignments) - {function.name}
            if overlap:
                collisions[function.name] = sorted(overlap)

        self.assertEqual(collisions, {})

    def test_status_command_calls_system_status_formatter(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
            tmux_lines=20,
        )
        update = {
            "update_id": 25,
            "message": {
                "message_id": 26,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/status",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "format_system_status",
                return_value="system status",
            ) as formatter,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", log_path
            )

        formatter.assert_called_once_with(
            "tele-agent",
            "tele-agent:codex.0",
            20,
            args=args,
            auth_failure=None,
        )
        self.assertEqual(send_reply.call_args.args[2], "system status")
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "status")

    def test_format_system_status_is_compact_and_shows_model_runtime(self) -> None:
        meta = {
            "agent_id": "agent-test",
            "created_ts": time.time() - 125,
            "codex_session_path": None,
        }
        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "codex_target_ready", return_value=True),
            mock.patch.object(
                telegram_inbox,
                "current_codex_model_and_reasoning_effort",
                return_value=("deepseek-v4-flash", "max"),
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "codex_session_for_pane",
                return_value=(None, "mocked"),
            ),
        ):
            text = telegram_inbox.format_system_status(
                "ds-goal", "ds-goal:0.0", 20
            )

        self.assertIn("model: deepseek-v4-flash / max", text)
        self.assertIn("uptime: 2m", text)
        self.assertIn("state: running", text)
        self.assertIn("auth: ok", text)
        self.assertNotIn("qstat", text)
        self.assertNotIn("tmux tail", text)

    def test_format_uptime_renders_compact_duration(self) -> None:
        self.assertEqual(telegram_inbox.format_uptime(42), "42s")
        self.assertEqual(telegram_inbox.format_uptime(125), "2m")
        self.assertEqual(telegram_inbox.format_uptime(3725), "1h 2m")
        self.assertEqual(telegram_inbox.format_uptime(90061), "1d 1h 1m")

    def test_help_defines_lifecycle_and_contains_no_prompt_syntax(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 27,
            "message": {
                "message_id": 28,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/help",
            },
        }
        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.root / "help.jsonl"
            )

        help_text = send_reply.call_args.args[2]
        self.assertIn(
            "/start_agent [MODEL] [LEVEL] — start only when stopped", help_text
        )
        self.assertIn("/kill_agent — stop the agent", help_text)
        self.assertIn(
            "/restart_agent [MODEL] [LEVEL] — replace a running agent with a "
            "fresh chat (context is not preserved)",
            help_text,
        )
        self.assertIn("/model latest|spark|ds-flash [LEVEL]", help_text)
        self.assertIn("MODEL defaults to latest (gpt-5.6-sol)", help_text)
        self.assertIn("ds-flash selects deepseek-v4-flash", help_text)
        self.assertIn("restart_agent ds-flash", help_text)
        self.assertIn("never accept prompts", help_text)
        self.assertNotIn("-- PROMPT", help_text)

    def test_restart_agent_passes_launch_overrides(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 30,
            "message": {
                "message_id": 31,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/restart_agent max",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    {"agent_id": "new-agent"},
                ),
            ) as start_agent,
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)

        start_agent.assert_called_once()
        call = start_agent.call_args.kwargs
        self.assertTrue(call["restart"])
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertEqual(call["reasoning_effort"], "max")
        self.assertNotIn("prompt", call)
        self.assertIn("reasoning=max", send_reply.call_args_list[0].args[2])

    def test_restart_agent_accepts_explicit_spark_without_changing_default(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 31,
            "message": {
                "message_id": 32,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/restart_agent spark",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    {"agent_id": "spark-agent"},
                ),
            ) as start_agent,
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "send_reply"),
        ):
            telegram_inbox.handle_update(
                update,
                args,
                {},
                "token",
                "123",
                self.root / "spark-restart.jsonl",
            )

        call = start_agent.call_args.kwargs
        self.assertEqual(call["model"], "gpt-5.3-codex-spark")
        self.assertEqual(call["reasoning_effort"], "high")
        self.assertEqual(
            telegram_inbox.DEFAULT_CODEX_AGENT_MODEL,
            "gpt-5.6-sol",
        )

    def test_restart_agent_accepts_ds_flash_with_max(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 32,
            "message": {
                "message_id": 33,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/restart_agent ds-flash",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    {"agent_id": "ds-flash-agent"},
                ),
            ) as start_agent,
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "send_reply"),
        ):
            telegram_inbox.handle_update(
                update,
                args,
                {},
                "token",
                "123",
                self.root / "ds-flash-restart.jsonl",
            )

        call = start_agent.call_args.kwargs
        self.assertEqual(call["model"], "deepseek-v4-flash")
        self.assertEqual(call["reasoning_effort"], "max")
        self.assertTrue(call["restart"])

    def test_kill_agent_persists_stopped_state_and_stops_only_agent(self) -> None:
        lifecycle = self.root / "lifecycle.state.json"
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            agent_lifecycle_state_path=str(lifecycle),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 32,
            "message": {
                "message_id": 33,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/kill_agent",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "stop_codex_agent",
                return_value=("Stopped the Codex agent.", {"agent_id": "old-agent"}),
            ) as stop_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.root / "kill.jsonl"
            )

        stop_agent.assert_called_once_with("tele-agent", "codex")
        self.assertEqual(
            telegram_inbox.read_json_object(lifecycle)["desired"], "stopped"
        )
        self.assertIn("listener remains online", send_reply.call_args.args[2])

    def test_stopped_state_blocks_message_autostart_and_watchdog(self) -> None:
        lifecycle = self.root / "lifecycle.state.json"
        telegram_inbox.write_json_object(lifecycle, {"desired": "stopped"})
        args = argparse.Namespace(
            agent_watchdog=True,
            agent_lifecycle_state_path=str(lifecycle),
            relay_mode="tmux-enter",
            target_pane="tele-agent:codex.0",
            session="tele-agent",
            codex_window="codex",
        )
        with (
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
            mock.patch.object(
                telegram_inbox, "registered_codex_process_running"
            ) as process_probe,
        ):
            relay_failure = telegram_inbox.ensure_codex_target_for_agent_message(
                args, {}
            )
            watchdog_result = telegram_inbox.maintain_managed_codex_agent(
                args, self.root / "watchdog.jsonl"
            )

        self.assertIn("agent is stopped", relay_failure)
        self.assertIsNone(watchdog_result)
        start_agent.assert_not_called()
        process_probe.assert_not_called()

    def test_restart_agent_refuses_when_agent_is_stopped(self) -> None:
        lifecycle = self.root / "lifecycle.state.json"
        telegram_inbox.write_json_object(lifecycle, {"desired": "stopped"})
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            agent_lifecycle_state_path=str(lifecycle),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 34,
            "message": {
                "message_id": 35,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/restart_agent max",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=False,
            ),
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.root / "restart.jsonl"
            )

        start_agent.assert_not_called()
        self.assertIn("use /start_agent", send_reply.call_args.args[2])
        record = json.loads(
            (self.root / "restart.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(record["action"], "restart_agent_failed")

    def test_start_agent_refuses_when_agent_is_running(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 36,
            "message": {
                "message_id": 37,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/start_agent",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=True,
            ),
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.root / "start.jsonl"
            )

        start_agent.assert_not_called()
        self.assertIn("use /restart_agent", send_reply.call_args.args[2])
        record = json.loads(
            (self.root / "start.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(record["action"], "start_agent_failed")

    def test_live_reasoning_selector_changes_effort_without_restart(self) -> None:
        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_has_codex_process", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
            mock.patch.object(telegram_inbox, "wait_for_tmux_text", return_value="selector"),
            mock.patch.object(
                telegram_inbox,
                "current_codex_model_and_reasoning_effort",
                return_value=("gpt-5.6-sol", "max"),
            ),
            mock.patch.object(telegram_inbox, "current_codex_reasoning_effort", return_value="max"),
        ):
            selected = telegram_inbox.set_codex_reasoning_effort(
                "tele-agent:codex.0", "max"
            )

        self.assertEqual(selected, "max")
        self.assertIn(
            mock.call("tele-agent:codex.0", "/model", literal=True),
            send_keys.call_args_list,
        )
        self.assertIn(
            mock.call("tele-agent:codex.0", "Home"),
            send_keys.call_args_list,
        )
        self.assertEqual(
            send_keys.call_args_list.count(
                mock.call("tele-agent:codex.0", "Down")
            ),
            4,
        )

    def test_live_model_selector_switches_to_spark_without_restart(self) -> None:
        def selector_text(_target: str, expected: str, timeout: float = 5.0) -> str:
            if expected == "Select Model and Effort":
                return (
                    "  1. gpt-5.6-sol (current)\n"
                    "  2. gpt-5.6-terra\n"
                    "› 7. gpt-5.3-codex-spark\n"
                )
            return expected

        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
            mock.patch.object(
                telegram_inbox,
                "wait_for_tmux_text",
                side_effect=selector_text,
            ),
            mock.patch.object(
                telegram_inbox,
                "current_codex_model_and_reasoning_effort",
                return_value=("gpt-5.3-codex-spark", "high"),
            ),
        ):
            selected = telegram_inbox.set_codex_model(
                "tele-agent:codex.0",
                "spark",
            )

        self.assertEqual(selected, ("gpt-5.3-codex-spark", "high"))
        self.assertEqual(
            send_keys.call_args_list.count(
                mock.call("tele-agent:codex.0", "Down")
            ),
            8,
        )
        self.assertNotIn(
            mock.call("tele-agent:codex.0", "C-c"),
            send_keys.call_args_list,
        )

    def test_live_model_selector_switches_to_ds_flash_without_restart(self) -> None:
        def selector_text(_target: str, expected: str, timeout: float = 5.0) -> str:
            if expected == "Select Model and Effort":
                return (
                    "  1. gpt-5.6-sol (current)\n"
                    "  2. deepseek-v4-pro\n"
                    "› 3. deepseek-v4-flash\n"
                )
            return expected

        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
            mock.patch.object(
                telegram_inbox,
                "wait_for_tmux_text",
                side_effect=selector_text,
            ),
            mock.patch.object(
                telegram_inbox,
                "current_codex_model_and_reasoning_effort",
                return_value=("deepseek-v4-flash", "max"),
            ),
        ):
            selected = telegram_inbox.set_codex_model(
                "tele-agent:codex.0",
                "ds-flash",
            )

        self.assertEqual(selected, ("deepseek-v4-flash", "max"))
        self.assertEqual(
            send_keys.call_args_list.count(
                mock.call("tele-agent:codex.0", "Down")
            ),
            6,
        )
        self.assertNotIn(
            mock.call("tele-agent:codex.0", "C-c"),
            send_keys.call_args_list,
        )

    def test_live_model_selector_ds_flash_on_openai_pane_gives_guidance(self) -> None:
        def selector_text(_target: str, expected: str, timeout: float = 5.0) -> str:
            if expected == "Select Model and Effort":
                return "  1. gpt-5.6-sol (current)\n  2. gpt-5.3-codex-spark\n"
            return expected

        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys"),
            mock.patch.object(
                telegram_inbox,
                "wait_for_tmux_text",
                side_effect=selector_text,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "restart_agent ds-flash"):
                telegram_inbox.set_codex_model(
                    "tele-agent:codex.0",
                    "ds-flash",
                )

    def test_model_command_preserves_current_agent(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 39,
            "message": {
                "message_id": 40,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/model spark",
            },
        }
        log_path = self.root / "model.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "set_codex_model",
                return_value=("gpt-5.3-codex-spark", "high"),
            ) as set_model,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=None,
            ),
        ):
            telegram_inbox.handle_update(
                update,
                args,
                {},
                "token",
                "123",
                log_path,
            )

        set_model.assert_called_once_with(
            "tele-agent:codex.0",
            "gpt-5.3-codex-spark",
            None,
        )
        self.assertIn("current chat preserved", send_reply.call_args.args[2])
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "model")
        self.assertEqual(record["agent_model"], "gpt-5.3-codex-spark")

    def test_live_reasoning_rejects_startup_only_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "use /restart_agent"):
            telegram_inbox.parse_live_reasoning_effort("minimal")

    def test_reasoning_command_preserves_current_agent(self) -> None:
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 40,
            "message": {
                "message_id": 41,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/reasoning high",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox, "set_codex_reasoning_effort", return_value="high"
            ) as set_reasoning,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(
                telegram_inbox.agent_registry, "active_agent_for_pane", return_value=None
            ),
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)

        set_reasoning.assert_called_once_with("tele-agent:codex.0", "high")
        self.assertIn("session preserved", send_reply.call_args.args[2])

    def test_changed_old_session_is_tailed_instead_of_replayed(self) -> None:
        session_started = telegram_inbox.iso_timestamp_epoch("2026-07-17T00:00:00Z")
        assert session_started is not None
        self.meta.update(
            {
                "created_ts": session_started + 3600,
                "launch_source": "telegram-start-agent-reuse",
            }
        )
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "historical response",
                },
            }
        )
        message_state = self.root / "messages.state.json"
        telegram_inbox.write_json_object(
            message_state,
            {"agent_id": "older-agent", "session_path": "/old/session.jsonl", "offset": 99},
        )

        with (
            mock.patch.object(telegram_inbox.time, "time", return_value=session_started + 3660),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "123",
                self.meta,
                message_state,
                None,
                {},
                sessions_root=self.sessions,
            )

        self.assertEqual(sent, 0)
        send_reply.assert_not_called()
        state = telegram_inbox.read_json_object(message_state)
        self.assertEqual(state["offset"], self.session.stat().st_size)

    def test_fresh_agent_session_can_be_forwarded_from_start(self) -> None:
        session_started = telegram_inbox.iso_timestamp_epoch("2026-07-17T00:00:00Z")
        assert session_started is not None
        self.meta.update(
            {
                "created_ts": session_started,
                "launch_source": "start_codex_agent.sh",
            }
        )
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "fresh response",
                },
            }
        )
        message_state = self.root / "messages.state.json"
        telegram_inbox.write_json_object(
            message_state,
            {"agent_id": "older-agent", "session_path": "/old/session.jsonl", "offset": 99},
        )

        with (
            mock.patch.object(telegram_inbox.time, "time", return_value=session_started + 60),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "123",
                self.meta,
                message_state,
                None,
                {},
                sessions_root=self.sessions,
            )

        self.assertEqual(sent, 1)
        self.assertIn("fresh response", send_reply.call_args.args[2])

    def test_delayed_first_message_from_launch_session_is_not_skipped(self) -> None:
        session_started = telegram_inbox.iso_timestamp_epoch("2026-07-17T00:00:00Z")
        assert session_started is not None
        self.meta.update(
            {
                "created_ts": session_started,
                "launch_source": "start_codex_agent.sh",
            }
        )
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "response after delayed first input",
                },
            }
        )
        message_state = self.root / "messages.state.json"

        with (
            mock.patch.object(
                telegram_inbox.time,
                "time",
                return_value=session_started + 3600,
            ),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "123",
                self.meta,
                message_state,
                None,
                {},
                sessions_root=self.sessions,
            )

        self.assertEqual(sent, 1)
        self.assertIn("response after delayed first input", send_reply.call_args.args[2])

    def test_item_completed_agent_message_is_forwarded(self) -> None:
        session_started = telegram_inbox.iso_timestamp_epoch("2026-07-17T00:00:00Z")
        assert session_started is not None
        self.meta.update(
            {
                "created_ts": session_started,
                "launch_source": "start_codex_agent.sh",
            }
        )
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "msg_test",
                        "content": [
                            {"type": "Text", "text": "new schema response"},
                        ],
                        "phase": "final_answer",
                    },
                },
            }
        )
        message_state = self.root / "messages.state.json"

        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "123",
                self.meta,
                message_state,
                None,
                {},
                sessions_root=self.sessions,
            )

        self.assertEqual(sent, 1)
        self.assertIn("new schema response", send_reply.call_args.args[2])
        self.assertTrue(send_reply.call_args.args[2].endswith(" ∎"))

    def test_invalid_saved_offset_tails_instead_of_replaying(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "must not replay",
                },
            }
        )
        message_state = self.root / "messages.state.json"
        telegram_inbox.write_json_object(
            message_state,
            {
                "agent_id": self.meta["agent_id"],
                "session_path": str(self.session.resolve()),
                "offset": self.session.stat().st_size + 1000,
            },
        )

        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            sent = telegram_inbox.drain_codex_agent_messages(
                "token",
                "123",
                self.meta,
                message_state,
                None,
                {},
                sessions_root=self.sessions,
            )

        self.assertEqual(sent, 0)
        send_reply.assert_not_called()
        state = telegram_inbox.read_json_object(message_state)
        self.assertEqual(state["offset"], self.session.stat().st_size)

    def test_rounded_100_percent_is_not_treated_as_depleted(self) -> None:
        self._write_records(self._rate_event(100.0, reached_type=None))
        state = self._refresh()
        self.assertFalse(state.get("depleted", False))

    def test_explicit_rate_limit_type_sets_persistent_marker(self) -> None:
        self._write_records(self._rate_event(100.0, reached_type="rate_limit_reached"))
        state = self._refresh()
        self.assertTrue(state["depleted"])
        self.assertEqual(state["source"], "rate_limit_reached_type")
        self.assertEqual(state["resets_at"], 2_000_000_000)

    def test_credit_reached_type_has_no_rate_window_reset(self) -> None:
        self._write_records(
            self._rate_event(
                100.0,
                reached_type="workspace_owner_credits_depleted",
            )
        )
        state = self._refresh()
        self.assertTrue(state["depleted"])
        self.assertEqual(state["depletion_kind"], "usage_or_credit")
        self.assertIsNone(state["resets_at"])

    def test_structured_usage_error_sets_marker(self) -> None:
        self._write_records(
            self._rate_event(100.0),
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "message": "human-readable wording may change",
                    "error_info": "usage_limit_exceeded",
                },
            },
        )
        state = self._refresh()
        self.assertTrue(state["depleted"])
        self.assertEqual(state["source"], "codex_error_info")
        self.assertEqual(state["depletion_kind"], "usage_or_credit")
        self.assertIsNone(state["resets_at"])

    def test_structured_usage_error_object_variant_sets_marker(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "reason": {"codex_error_info": {"usage_limit_exceeded": {}}},
                },
            }
        )
        state = self._refresh()
        self.assertTrue(state["depleted"])
        self.assertEqual(state["error_code"], "usage_limit_exceeded")

    def test_message_text_cannot_trigger_marker(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "usage_limit_exceeded"},
            }
        )
        state = self._refresh()
        self.assertFalse(state.get("depleted", False))

    def test_available_snapshot_clears_marker(self) -> None:
        self._write_records(self._rate_event(100.0, reached_type="rate_limit_reached"))
        self._refresh()
        self._write_records(self._rate_event(12.0, reached_type=None, resets_at=2_100_000_000))
        state = self._refresh(now=1_900_000_010)
        self.assertFalse(state["depleted"])
        self.assertEqual(state["clear_reason"], "new_turn_available_rate_event")

    def test_new_turn_available_event_clears_old_structured_failure(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "error_info": "usage_limit_exceeded",
                },
            }
        )
        self._refresh()
        self._write_records(self._rate_event(1.0, reached_type=None))
        state = self._refresh(now=1_900_000_010)
        self.assertFalse(state["depleted"])
        self.assertEqual(state["clear_reason"], "new_turn_available_rate_event")

    def test_rate_window_failure_does_not_downgrade_structured_credit_failure(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "error_info": "usage_limit_exceeded",
                },
            }
        )
        self._refresh()
        self._write_records(
            self._rate_event(
                100.0,
                reached_type="rate_limit_reached",
                resets_at=1_900_000_005,
            )
        )
        state = self._refresh(now=1_900_000_010)
        self.assertTrue(state["depleted"])
        self.assertEqual(state["source"], "codex_error_info")
        self.assertEqual(state["depletion_kind"], "usage_or_credit")
        self.assertIsNone(state["resets_at"])

    def test_reset_time_expires_marker(self) -> None:
        self._write_records(self._rate_event(100.0, reached_type="rate_limit_reached", resets_at=1000))
        state = self._refresh(now=999)
        self.assertTrue(state["depleted"])
        state = self._refresh(now=1000)
        self.assertFalse(state["depleted"])
        self.assertEqual(state["clear_reason"], "reset_time_reached")

    def test_stale_reset_timestamp_cannot_clear_structured_credit_failure(self) -> None:
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "source": "codex_error_info",
                "error_code": "usage_limit_exceeded",
                "resets_at": 1000,
            },
        )
        state = self._refresh(now=2000)
        self.assertTrue(state["depleted"])

    def test_incremental_scan_does_not_skip_first_new_line(self) -> None:
        self._write_records(self._rate_event(20.0))
        self._refresh()
        self._write_records(self._rate_event(100.0, reached_type="rate_limit_reached"))
        state = self._refresh(now=1_900_000_010)
        self.assertTrue(state["depleted"])

    def test_normal_message_ignores_old_depletion_and_attempts_live_turn(self) -> None:
        telegram_inbox.write_json_object(
            self.state,
            {"depleted": True, "source": "codex_error_info", "resets_at": 2_000_000_000},
        )
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
            relay_mode="tmux-enter",
            allow_shell_pane=False,
            submit_delay=0,
            relay_confirmation_state_path=None,
            bridge_ack=False,
        )
        update = {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "are you there?",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(
                telegram_inbox,
                "ensure_codex_target_for_agent_message",
                return_value=None,
            ) as ensure,
            mock.patch.object(
                telegram_inbox,
                "paste_to_tmux",
                return_value="relayed to tele-agent:codex.0; submission confirmed",
            ) as paste,
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=False),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=None,
            ),
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)
        ensure.assert_called_once()
        paste.assert_called_once()
        send_reply.assert_not_called()
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "agent")
        self.assertIn("submission confirmed", record["relay_result"])

    def test_codex_reset_first_reply_lists_count_expiry_and_confirmation(self) -> None:
        reset_state = self.root / "reset.state.json"
        telegram_inbox.write_json_object(
            self.state,
            {
                "last_rate_summary": {
                    "windows": [
                        {
                            "name": "primary",
                            "used_percent": 81.0,
                            "resets_at": 2_000_000_000,
                            "window_minutes": 10080,
                        }
                    ]
                }
            },
        )
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(reset_state),
            repo_root=self.repo,
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 10,
            "message": {
                "message_id": 11,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/codex_reset",
            },
        }
        log_path = self.root / "listener.jsonl"
        entries = [
            "Full reset  Expires Jul 20, 2026",
            "Full reset  Expires Jul 27, 2026",
        ]
        live_usage = {
            "query_state": "success",
            "checked_at": "2026-07-28T20:30:00+08:00",
            "codex_version": "0.145.0",
            "status_lines": [
                "Weekly limit: 19% left",
                "(resets 20:32 on 4 Aug)",
            ],
            "credits_visibility": "not_reported_by_codex_status",
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "inspect_codex_live_usage",
                return_value=live_usage,
            ),
            mock.patch.object(telegram_inbox, "list_codex_usage_resets", return_value=(2, entries)),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)
        send_reply.assert_called_once()
        outgoing = send_reply.call_args.args[2]
        self.assertIn("Live Codex /status query", outgoing)
        self.assertIn("Weekly limit: 19% left", outgoing)
        self.assertIn("not a workspace credit", outgoing)
        self.assertNotIn("Last reported Codex limits", outgoing)
        self.assertIn("Banked Codex resets remaining: 2", outgoing)
        self.assertIn("Expires Jul 20, 2026", outgoing)
        self.assertIn("Expires Jul 27, 2026", outgoing)
        self.assertIn("Send /Confirm", outgoing)
        state = telegram_inbox.read_json_object(reset_state)
        self.assertEqual(state["phase"], "awaiting_confirmation")
        self.assertEqual(state["available"], 2)

    def test_codex_reset_zero_reports_only_no_redemption(self) -> None:
        reset_state = self.root / "reset.state.json"
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(reset_state),
            repo_root=self.repo,
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 10,
            "message": {
                "message_id": 11,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/codex_reset",
            },
        }
        log_path = self.root / "listener.jsonl"
        live_usage = {
            "query_state": "success",
            "checked_at": "2026-07-28T20:30:00+08:00",
            "codex_version": "0.145.0",
            "status_lines": ["Weekly limit: 100% left"],
            "credits_visibility": "not_reported_by_codex_status",
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "inspect_codex_live_usage",
                return_value=live_usage,
            ),
            mock.patch.object(telegram_inbox, "list_codex_usage_resets", return_value=(0, [])),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)
        outgoing = send_reply.call_args.args[2]
        self.assertIn("Banked Codex resets remaining: 0. No reset was redeemed.", outgoing)
        self.assertNotIn("agent restarted", outgoing.lower())
        self.assertNotIn("Nothing was changed", outgoing)
        self.assertEqual(telegram_inbox.read_json_object(reset_state)["phase"], "unavailable")

    def test_live_status_format_never_includes_old_depletion_snapshot(self) -> None:
        live_usage = {
            "query_state": "success",
            "checked_at": "2026-07-28T20:30:00+08:00",
            "codex_version": "0.145.0",
            "status_lines": [
                "Weekly limit: 100% left",
                "(resets 20:32 on 4 Aug)",
            ],
            "credits_visibility": "not_reported_by_codex_status",
        }
        text = telegram_inbox.format_live_codex_limits(live_usage)

        self.assertIn("Weekly limit: 100% left", text)
        self.assertIn("Live rate-window status: available", text)
        self.assertNotIn("usage_limit_exceeded", text)
        self.assertIn("not a workspace credit", text)

    def test_live_usage_propagates_fresh_account_query_failure(self) -> None:
        with mock.patch.object(
            telegram_inbox.codex_rate_limits,
            "read_rate_limits",
            side_effect=telegram_inbox.codex_rate_limits.RateLimitError(
                "fresh query failed"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fresh query failed"):
                telegram_inbox.inspect_codex_live_usage(self.repo)

    def test_live_query_clears_old_structured_depletion_state(self) -> None:
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "source": "codex_error_info",
                "error_code": "usage_limit_exceeded",
                "alert_pending": False,
            },
        )
        live_usage = {
            "query_state": "success",
            "checked_at": "2026-07-28T22:30:00+08:00",
            "codex_version": "0.145.0",
            "status_lines": [
                "5h limit: [████████████████████] 100% left",
                "Weekly limit: 100% left",
            ],
            "credits_visibility": "not_reported_by_codex_status",
        }

        state = telegram_inbox.reconcile_codex_usage_state_from_live_query(
            self.state,
            live_usage,
        )

        self.assertFalse(state["depleted"])
        self.assertEqual(state["clear_reason"], "live_status_query_available")
        self.assertTrue(state["last_live_rate_available"])

    def test_codex_usage_command_queries_live_and_clears_old_state(self) -> None:
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "source": "codex_error_info",
                "error_code": "usage_limit_exceeded",
            },
        )
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 12,
            "message": {
                "message_id": 13,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/codex_usage",
            },
        }
        live_usage = {
            "query_state": "success",
            "checked_at": "2026-07-28T22:30:00+08:00",
            "codex_version": "0.145.0",
            "status_lines": ["Weekly limit: 100% left"],
            "credits_visibility": "not_reported_by_codex_status",
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "inspect_codex_live_usage",
                return_value=live_usage,
            ) as inspect,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update,
                args,
                {},
                "token",
                "123",
                log_path,
            )

        inspect.assert_called_once_with(self.repo)
        self.assertIn("Live Codex /status query", send_reply.call_args.args[2])
        self.assertFalse(
            telegram_inbox.read_json_object(self.state)["depleted"]
        )
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "codex_usage_live")

    def test_usage_failure_tracks_triggering_telegram_message(self) -> None:
        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": (
                        "[TELEGRAM USER MESSAGE message_id=4452 from @tester] "
                        "How has Ultra been doing?"
                    ),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "reason": {
                        "codex_error_info": {"usage_limit_exceeded": {}}
                    },
                },
            },
        )

        state = self._refresh()

        self.assertTrue(state["depleted"])
        self.assertTrue(state["alert_pending"])
        self.assertEqual(state["failed_message_id"], 4452)

    def test_usage_failure_notice_replies_once_to_triggering_message(self) -> None:
        log_path = self.root / "listener.jsonl"
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "alert_pending": True,
                "failed_message_id": 4452,
                "error_code": "usage_limit_exceeded",
                "agent_id": "agent-test",
            },
        )
        with mock.patch.object(telegram_inbox, "send_reply") as send_reply:
            sent = telegram_inbox.notify_codex_usage_failure(
                "token",
                "123",
                self.state,
                log_path,
                {},
                4000,
            )
            sent_again = telegram_inbox.notify_codex_usage_failure(
                "token",
                "123",
                self.state,
                log_path,
                {},
                4000,
            )

        self.assertTrue(sent)
        self.assertFalse(sent_again)
        send_reply.assert_called_once()
        self.assertEqual(send_reply.call_args.kwargs["reply_to_message_id"], 4452)
        state = telegram_inbox.read_json_object(self.state)
        self.assertFalse(state["alert_pending"])
        self.assertIsInstance(state["alert_sent_ts"], int)
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[-1]["event"], "codex_usage_failure_notice_sent")

    def test_usage_failure_notice_remains_pending_after_send_error(self) -> None:
        log_path = self.root / "listener.jsonl"
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "alert_pending": True,
                "failed_message_id": 4452,
                "error_code": "usage_limit_exceeded",
            },
        )
        with mock.patch.object(
            telegram_inbox,
            "send_reply",
            side_effect=RuntimeError("network unavailable"),
        ):
            sent = telegram_inbox.notify_codex_usage_failure(
                "token",
                "123",
                self.state,
                log_path,
                {},
                4000,
            )

        self.assertFalse(sent)
        self.assertTrue(telegram_inbox.read_json_object(self.state)["alert_pending"])
        event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["event"], "codex_usage_failure_notice_failed")

    def test_start_agent_is_not_blocked_by_old_usage_failure(self) -> None:
        telegram_inbox.write_json_object(
            self.state,
            {
                "depleted": True,
                "detected_ts": 1_900_000_000,
                "source": "codex_error_info",
                "error_code": "usage_limit_exceeded",
            },
        )
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(self.root / "reset.state.json"),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 40,
            "message": {
                "message_id": 41,
                "date": 1_900_000_000,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/start_agent",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "managed_codex_agent_present",
                return_value=False,
            ),
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    {"agent_id": "agent-live"},
                ),
            ) as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
            mock.patch.object(
                telegram_inbox.agent_registry,
                "append_agent_event",
            ),
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", log_path
            )

        start_agent.assert_called_once()
        self.assertIn("Normal Telegram text", send_reply.call_args.args[2])
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "start_agent")
        lifecycle = self.state.with_name("telegram_agent_lifecycle.state.json")
        self.assertEqual(
            telegram_inbox.read_json_object(lifecycle)["desired"], "running"
        )

    def test_confirm_redeems_then_restarts_telegram_agent(self) -> None:
        reset_state = self.root / "reset.state.json"
        now = int(telegram_inbox.time.time())
        telegram_inbox.write_json_object(
            reset_state,
            {
                "phase": "awaiting_confirmation",
                "requested_ts": now,
                "expires_ts": now + 300,
                "chat_id": "123",
                "sender_id": "456",
                "available": 1,
                "entries": ["Full reset  Expires Jul 20, 2026"],
            },
        )
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(reset_state),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 20,
            "message": {
                "message_id": 21,
                "date": now,
                "chat": {"id": "123"},
                "from": {"id": 456, "username": "tester"},
                "text": "/Confirm",
            },
        }
        log_path = self.root / "listener.jsonl"
        with (
            mock.patch.object(
                telegram_inbox,
                "run_codex_reset_helper",
                return_value=(0, "RESET_SUCCESS\n", ""),
            ),
            mock.patch.object(
                telegram_inbox,
                "start_codex_agent",
                return_value=(
                    "tele-agent:codex.0",
                    "Started Codex in tele-agent:codex.0.",
                    {"agent_id": "new-agent"},
                ),
            ) as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(update, args, {}, "token", "123", log_path)
        start_agent.assert_called_once_with(
            repo_root=self.repo,
            session="tele-agent",
            window="codex",
            restart=True,
        )
        self.assertEqual(send_reply.call_count, 2)
        self.assertIn("redeemed successfully", send_reply.call_args.args[2])
        self.assertEqual(telegram_inbox.read_json_object(reset_state)["phase"], "completed")

    def test_confirm_redeems_without_starting_intentionally_stopped_agent(self) -> None:
        reset_state = self.root / "reset.state.json"
        lifecycle = self.root / "lifecycle.state.json"
        now = int(telegram_inbox.time.time())
        telegram_inbox.write_json_object(
            reset_state,
            {
                "phase": "awaiting_confirmation",
                "requested_ts": now,
                "expires_ts": now + 300,
                "chat_id": "123",
                "sender_id": "456",
                "available": 1,
                "entries": ["Full reset  Expires Jul 20, 2026"],
            },
        )
        telegram_inbox.write_json_object(lifecycle, {"desired": "stopped"})
        args = argparse.Namespace(
            codex_usage_state_path=str(self.state),
            codex_reset_state_path=str(reset_state),
            agent_lifecycle_state_path=str(lifecycle),
            repo_root=self.repo,
            session="tele-agent",
            codex_window="codex",
            max_log_chars=4000,
            target_pane="tele-agent:codex.0",
        )
        update = {
            "update_id": 22,
            "message": {
                "message_id": 23,
                "date": now,
                "chat": {"id": "123"},
                "from": {"id": 456},
                "text": "/Confirm",
            },
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "run_codex_reset_helper",
                return_value=(0, "RESET_SUCCESS\n", ""),
            ),
            mock.patch.object(telegram_inbox, "start_codex_agent") as start_agent,
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, args, {}, "token", "123", self.root / "reset-stopped.jsonl"
            )

        start_agent.assert_not_called()
        self.assertIn("agent remains stopped", send_reply.call_args.args[2])
        completed = telegram_inbox.read_json_object(reset_state)
        self.assertEqual(completed["phase"], "completed")
        self.assertFalse(completed["agent_restarted"])

    def test_reset_confirmation_expires_without_action(self) -> None:
        reset_state = self.root / "reset.state.json"
        telegram_inbox.write_json_object(
            reset_state,
            {
                "phase": "awaiting_confirmation",
                "expires_ts": 100,
                "chat_id": "123",
                "sender_id": "456",
            },
        )
        pending = telegram_inbox.reset_confirmation_state(
            reset_state, "123", "456", now=101
        )
        self.assertIsNone(pending)
        self.assertEqual(telegram_inbox.read_json_object(reset_state)["phase"], "expired")

    def test_reset_listing_parser_preserves_every_expiry(self) -> None:
        output = (
            "AVAILABLE=2\n"
            "RESET=Full reset  Expires 05:09 on 12 Aug 2026.\n"
            "RESET=Full reset  Expires 02:13 on 13 Aug 2026.\n"
        )
        with mock.patch.object(
            telegram_inbox,
            "run_codex_reset_helper",
            return_value=(0, output, ""),
        ):
            available, entries = telegram_inbox.list_codex_usage_resets(self.repo)
        self.assertEqual(available, 2)
        self.assertEqual(len(entries), 2)
        self.assertIn("12 Aug 2026", entries[0])
        self.assertIn("13 Aug 2026", entries[1])

    def test_tmux_relay_submits_once_and_confirms_message(self) -> None:
        relay_text = (
            "[TELEGRAM USER MESSAGE message_id=77 from @tester | 456] hello"
        )
        checkpoint = (self.session, self.session.stat().st_size)
        enter_count = 0

        def fake_run(command, **_kwargs):
            nonlocal enter_count
            if command[-1] == "Enter":
                enter_count += 1
                self._write_records(
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": relay_text},
                    }
                )
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=False,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_command", return_value="codex"),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=checkpoint
            ),
            mock.patch.object(telegram_inbox.subprocess, "run", side_effect=fake_run),
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                relay_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0.01,
            )

        self.assertEqual(enter_count, 1)
        self.assertIn("submission confirmed", result)

    def test_tmux_relay_records_pending_without_resubmit_or_false_failure(self) -> None:
        relay_text = (
            "[TELEGRAM USER MESSAGE message_id=78 from @tester | 456] hello"
        )
        checkpoint = (self.session, self.session.stat().st_size)
        pending_state = self.root / "relay-confirmation.state.json"
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=False,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_command", return_value="codex"),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=checkpoint
            ),
            mock.patch.object(telegram_inbox.subprocess, "run", side_effect=fake_run),
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                relay_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0,
                pending_state_path=pending_state,
            )

        self.assertTrue(result.startswith("relayed to "))
        self.assertIn("pending confirmation", result)
        self.assertEqual(sum(command[-1] == "Enter" for command in commands), 1)
        self.assertEqual(commands[-1][-1], "Enter")
        pending = telegram_inbox.read_json_object(pending_state)["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message_id"], 78)
        self.assertEqual(pending[0]["offset"], checkpoint[1])

    def test_tmux_relay_does_not_merge_new_message_into_pending_composer(self) -> None:
        old_marker = "[TELEGRAM USER MESSAGE message_id=7801"
        new_text = "[TELEGRAM USER MESSAGE message_id=7802 from @tester | 456] new"
        pending_state = self.root / "relay-confirmation.state.json"
        telegram_inbox.write_json_object(
            pending_state,
            {
                "pending": [
                    {
                        "agent_id": "agent-test",
                        "created_ts": 100.0,
                        "marker": old_marker,
                        "message_id": 7801,
                        "offset": self.session.stat().st_size,
                        "session_path": str(self.session),
                        "target_pane": "tele-agent:codex.0",
                    }
                ],
                "version": 1,
            },
        )
        meta = {
            "agent_id": "agent-test",
            "codex_session_path": str(self.session),
        }

        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=True,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                return_value=(self.session, self.session.stat().st_size),
            ),
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="absent",
            ),
            mock.patch.object(telegram_inbox.subprocess, "run") as run,
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                new_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0,
                pending_state_path=pending_state,
            )

        self.assertIn("previous Telegram message 7801", result)
        self.assertIn("composer was left unchanged", result)
        run.assert_not_called()

    def test_tmux_relay_retries_enter_once_when_first_submit_is_ignored(self) -> None:
        relay_text = (
            "[TELEGRAM USER MESSAGE message_id=779 from @tester | 456] hello"
        )
        checkpoint = (self.session, self.session.stat().st_size)
        enter_count = 0

        def fake_run(command, **_kwargs):
            nonlocal enter_count
            if command[-1] == "Enter":
                enter_count += 1
                if enter_count == 2:
                    self._write_records(
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": relay_text},
                        }
                    )
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(
                telegram_inbox,
                "registered_codex_process_running",
                return_value=False,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_command", return_value="codex"),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=checkpoint
            ),
            mock.patch.object(telegram_inbox.subprocess, "run", side_effect=fake_run),
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                relay_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0.01,
            )

        self.assertEqual(enter_count, 2)
        self.assertIn("submission confirmed", result)

    def test_tmux_relay_bootstraps_lazily_created_first_session(self) -> None:
        relay_text = (
            "[TELEGRAM USER MESSAGE message_id=780 from @tester | 456] hello"
        )
        checkpoint = (self.session, 0)
        meta = {
            "agent_id": "new-agent",
            "codex_session_path": None,
            "launch_source": "telegram-restart-agent",
        }
        enter_count = 0

        def fake_run(command, **_kwargs):
            nonlocal enter_count
            if command[-1] == "Enter":
                enter_count += 1
                self._write_records(
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": relay_text},
                    }
                )
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_command", return_value="codex"),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_session_checkpoint",
                side_effect=[None, checkpoint],
            ),
            mock.patch.object(telegram_inbox.subprocess, "run", side_effect=fake_run),
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                relay_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
                confirmation_timeout=0.01,
            )

        self.assertEqual(enter_count, 1)
        self.assertIn("submission confirmed", result)

    def test_tmux_relay_still_rejects_unregistered_sessionless_pane(self) -> None:
        relay_text = (
            "[TELEGRAM USER MESSAGE message_id=781 from @tester | 456] hello"
        )
        with (
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(telegram_inbox, "tmux_pane_command", return_value="codex"),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
            mock.patch.object(
                telegram_inbox, "codex_session_checkpoint", return_value=None
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=None,
            ),
            mock.patch.object(telegram_inbox.subprocess, "run") as run,
        ):
            result = telegram_inbox.paste_to_tmux(
                "tele-agent:codex.0",
                relay_text,
                press_enter=True,
                allow_shell_pane=False,
                submit_delay=0,
            )

        self.assertEqual(
            result, "not relayed: the active Codex session could not be verified"
        )
        run.assert_not_called()

    def test_pending_bootstrap_relay_links_delayed_first_session(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=782"
        pending_state = self.root / "relay-confirmation.state.json"
        meta_without_session = {
            "agent_id": "new-agent",
            "codex_session_path": None,
            "launch_source": "telegram-restart-agent",
        }
        meta_with_session = {
            **meta_without_session,
            "codex_session_path": str(self.session),
        }

        with mock.patch.object(
            telegram_inbox.agent_registry,
            "active_agent_for_pane",
            return_value=meta_without_session,
        ):
            telegram_inbox.register_pending_codex_submission(
                pending_state,
                None,
                marker,
                "tele-agent:codex.0",
                now=100.0,
            )

        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta_without_session,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=meta_without_session,
            ),
        ):
            first = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state, now=110.0
            )
        self.assertEqual(first["confirmed"], [])
        self.assertEqual(len(first["pending"]), 1)

        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": marker + " from @tester | 456] hello",
                },
            }
        )
        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta_without_session,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=meta_with_session,
            ),
        ):
            second = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state, now=120.0
            )

        self.assertEqual(len(second["confirmed"]), 1)
        self.assertEqual(second["pending"], [])

    def test_pending_relay_confirms_later_without_resubmission(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=79"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )

        first = telegram_inbox.reconcile_pending_codex_submissions(
            pending_state,
            log_path=log_path,
            now=160.0,
        )
        self.assertEqual(first["confirmed"], [])
        self.assertEqual(len(first["pending"]), 1)

        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": marker + " from @tester | 456] hello",
                },
            }
        )
        second = telegram_inbox.reconcile_pending_codex_submissions(
            pending_state,
            log_path=log_path,
            now=200.0,
        )

        self.assertEqual(len(second["confirmed"]), 1)
        self.assertEqual(second["confirmed"][0]["latency_seconds"], 100.0)
        self.assertEqual(second["pending"], [])
        event = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "telegram_relay_submission_confirmed")
        self.assertEqual(event["message_id"], 79)

    def test_pending_relay_replays_once_after_codex_process_restart(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=790"
        relay_text = marker + " from @tester | 456] survive restart"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        target_pane = "tele-agent:codex.0"
        meta = {
            "agent_id": "agent-test",
            "codex_session_path": None,
            "launch_source": "telegram-start-agent",
        }
        with mock.patch.object(
            telegram_inbox.agent_registry,
            "active_agent_for_pane",
            return_value=meta,
        ):
            telegram_inbox.register_pending_codex_submission(
                pending_state,
                None,
                marker,
                target_pane,
                now=100.0,
                relay_text=relay_text,
                process_identity="100:1000",
            )

        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="absent",
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_process_identity",
                return_value="200:2000",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
            mock.patch.object(
                telegram_inbox, "tmux_paste_text_atomic"
            ) as paste_text,
            mock.patch.object(
                telegram_inbox,
                "wait_for_codex_bootstrap_submission",
                return_value=(True, (self.session, 0)),
            ),
            mock.patch.object(telegram_inbox.time, "sleep"),
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=120.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.01,
            )

        self.assertEqual(
            send_keys.call_args_list,
            [mock.call(target_pane, "C-u"), mock.call(target_pane, "Enter")],
        )
        paste_text.assert_called_once_with(target_pane, relay_text)
        self.assertEqual(len(result["retried"]), 1)
        self.assertEqual(len(result["confirmed"]), 1)
        self.assertEqual(result["pending"], [])
        self.assertEqual(
            result["confirmed"][0]["recovery_method"],
            "replacement_process_replay",
        )
        self.assertEqual(
            telegram_inbox.read_json_object(pending_state)["pending"], []
        )
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "telegram_relay_replayed_after_codex_restart",
                "telegram_relay_submission_confirmed",
            ],
        )

    def test_pending_relay_recovers_message_still_in_composer(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=80"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )

        def submit_pending_message(target_pane: str, key: str) -> None:
            self.assertEqual(target_pane, "tele-agent:codex.0")
            self.assertEqual(key, "Enter")
            self._write_records(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": marker + " from @tester | 456] hello",
                    },
                }
            )

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="composer",
            ),
            mock.patch.object(
                telegram_inbox,
                "tmux_send_keys",
                side_effect=submit_pending_message,
            ) as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=120.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.01,
            )

        send_keys.assert_called_once_with("tele-agent:codex.0", "Enter")
        self.assertEqual(len(result["retried"]), 1)
        self.assertEqual(len(result["confirmed"]), 1)
        self.assertTrue(result["confirmed"][0]["recovered"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["stalled"], [])
        self.assertEqual(
            telegram_inbox.read_json_object(pending_state)["pending"],
            [],
        )
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "telegram_relay_submission_retried",
                "telegram_relay_submission_confirmed",
            ],
        )

    def test_pending_relay_keeps_retrying_without_false_stall(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=83"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="composer",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            first = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=120.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.0,
            )
            second = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=200.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.0,
            )

        self.assertEqual(
            send_keys.call_args_list,
            [
                mock.call("tele-agent:codex.0", "Enter"),
                mock.call("tele-agent:codex.0", "Enter"),
            ],
        )
        self.assertEqual(len(first["retried"]), 1)
        self.assertEqual(first["stalled"], [])
        self.assertEqual(len(first["pending"]), 1)
        self.assertEqual(first["pending"][0]["recovery_attempts"], 1)
        self.assertNotIn("stalled_ts", first["pending"][0])
        self.assertEqual(len(second["retried"]), 1)
        self.assertEqual(second["stalled"], [])
        self.assertEqual(len(second["pending"]), 1)
        self.assertEqual(second["pending"][0]["recovery_attempts"], 2)
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "telegram_relay_submission_retried",
                "telegram_relay_submission_retried",
            ],
        )

    def test_pending_relay_does_not_touch_composer_during_active_turn(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=831"
        pending_state = self.root / "relay-confirmation.state.json"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )

        with (
            mock.patch.object(
                telegram_inbox,
                "codex_session_turn_active",
                return_value=True,
            ),
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
            ) as pane_location,
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                now=120.0,
                recovery_grace_seconds=10.0,
            )

        pane_location.assert_not_called()
        send_keys.assert_not_called()
        self.assertEqual(result["retried"], [])
        self.assertEqual(result["stalled"], [])
        self.assertEqual(len(result["pending"]), 1)

    def test_pending_relay_reopens_legacy_latency_stall(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=832"
        pending_state = self.root / "relay-confirmation.state.json"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )
        state = telegram_inbox.read_json_object(pending_state)
        state["pending"][0].update(
            {
                "stalled_ts": 110.0,
                "stalled_reason": "enter_retry_unconfirmed",
            }
        )
        telegram_inbox.write_json_object(pending_state, state)

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="composer",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                now=120.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.0,
            )

        send_keys.assert_called_once_with("tele-agent:codex.0", "Enter")
        self.assertEqual(result["stalled"], [])
        self.assertNotIn("stalled_ts", result["pending"][0])
        self.assertNotIn("stalled_reason", result["pending"][0])

    def test_pending_relay_waits_for_jsonl_when_marker_reaches_history(
        self,
    ) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=84"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
            process_identity="100:1000",
        )

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="history",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=120.0,
                recovery_grace_seconds=10.0,
            )

        send_keys.assert_not_called()
        self.assertEqual(result["retried"], [])
        self.assertEqual(result["stalled"], [])
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(
            result["pending"][0]["pane_submission_observed_ts"], 120.0
        )
        event = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(
            event["event"], "telegram_relay_submission_observed_in_history"
        )

        self._write_records(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": marker + " from @tester | 456] hello",
                },
            }
        )
        confirmed = telegram_inbox.reconcile_pending_codex_submissions(
            pending_state,
            log_path=log_path,
            now=160.0,
        )
        self.assertEqual(len(confirmed["confirmed"]), 1)
        self.assertEqual(confirmed["pending"], [])

    def test_pending_relay_does_not_stall_when_retry_moves_to_history(self) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=85"
        pending_state = self.root / "relay-confirmation.state.json"
        log_path = self.root / "listener.jsonl"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
            process_identity="100:1000",
        )

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                side_effect=["composer", "history"],
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                log_path=log_path,
                now=120.0,
                recovery_grace_seconds=10.0,
                recovery_confirmation_timeout=0.0,
            )

        send_keys.assert_called_once_with("tele-agent:codex.0", "Enter")
        self.assertEqual(len(result["retried"]), 1)
        self.assertEqual(result["stalled"], [])
        self.assertEqual(
            result["pending"][0]["pane_submission_observed_ts"], 120.0
        )
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "telegram_relay_submission_retried",
                "telegram_relay_submission_observed_in_history",
            ],
        )

    def test_history_observed_pending_only_blocks_a_replacement_process(self) -> None:
        pending_state = self.root / "relay-confirmation.state.json"
        target_pane = "tele-agent:codex.0"
        telegram_inbox.write_json_object(
            pending_state,
            {
                "pending": [
                    {
                        "agent_id": "agent-test",
                        "codex_process_identity": "100:1000",
                        "marker": "[TELEGRAM USER MESSAGE message_id=86",
                        "message_id": 86,
                        "pane_submission_observed_ts": 120.0,
                        "target_pane": target_pane,
                    }
                ],
                "version": 1,
            },
        )
        meta = {"agent_id": "agent-test"}

        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_process_identity",
                return_value="100:1000",
            ),
        ):
            same_process = telegram_inbox.current_pending_codex_submissions(
                pending_state, target_pane
            )
        self.assertEqual(same_process, [])

        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_process_identity",
                return_value="200:2000",
            ),
        ):
            replacement_process = telegram_inbox.current_pending_codex_submissions(
                pending_state, target_pane
            )
        self.assertEqual(len(replacement_process), 1)

        state = telegram_inbox.read_json_object(pending_state)
        state["pending"][0]["replacement_codex_process_identity"] = "200:2000"
        telegram_inbox.write_json_object(pending_state, state)
        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox,
                "codex_process_identity",
                return_value="200:2000",
            ),
        ):
            replayed_process = telegram_inbox.current_pending_codex_submissions(
                pending_state, target_pane
            )
        self.assertEqual(replayed_process, [])

    def test_pending_relay_does_not_submit_when_marker_is_not_in_composer(
        self,
    ) -> None:
        marker = "[TELEGRAM USER MESSAGE message_id=81"
        pending_state = self.root / "relay-confirmation.state.json"
        checkpoint = (self.session, self.session.stat().st_size)
        telegram_inbox.register_pending_codex_submission(
            pending_state,
            checkpoint,
            marker,
            "tele-agent:codex.0",
            now=100.0,
        )

        with (
            mock.patch.object(
                telegram_inbox,
                "pending_codex_submission_pane_location",
                return_value="absent",
            ),
            mock.patch.object(telegram_inbox, "tmux_send_keys") as send_keys,
        ):
            result = telegram_inbox.reconcile_pending_codex_submissions(
                pending_state,
                now=200.0,
                recovery_grace_seconds=10.0,
            )

        send_keys.assert_not_called()
        self.assertEqual(result["retried"], [])
        self.assertEqual(len(result["pending"]), 1)

    def test_composer_detection_requires_marker_in_latest_prompt(self) -> None:
        item = {
            "agent_id": "agent-test",
            "marker": "[TELEGRAM USER MESSAGE message_id=82",
            "target_pane": "tele-agent:codex.0",
        }
        meta = {"agent_id": "agent-test"}
        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(
                telegram_inbox,
                "tmux_pane_has_codex_process",
                return_value=True,
            ),
            mock.patch.object(
                telegram_inbox,
                "run_short",
                return_value=(
                    "› [TELEGRAM USER MESSAGE message_id=82 from @tester] hello\n"
                    "\n"
                    "  gpt-5.6-sol high · /workspace"
                ),
            ),
        ):
            self.assertEqual(
                telegram_inbox.pending_codex_submission_pane_location(item),
                "composer",
            )
            self.assertTrue(telegram_inbox.pending_codex_submission_in_composer(item))
            capture_command = telegram_inbox.run_short.call_args.args[0]
            self.assertIn("-S", capture_command)
            self.assertIn("-", capture_command)

        with (
            mock.patch.object(
                telegram_inbox.agent_registry,
                "active_agent_for_pane",
                return_value=meta,
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=True),
            mock.patch.object(
                telegram_inbox,
                "tmux_pane_has_codex_process",
                return_value=True,
            ),
            mock.patch.object(
                telegram_inbox,
                "run_short",
                return_value=(
                    "› [TELEGRAM USER MESSAGE message_id=82 from @tester] hello\n"
                    "• completed\n"
                    "› a different unsent message"
                ),
            ),
        ):
            self.assertEqual(
                telegram_inbox.pending_codex_submission_pane_location(item),
                "history",
            )
            self.assertFalse(telegram_inbox.pending_codex_submission_in_composer(item))


if __name__ == "__main__":
    unittest.main()
