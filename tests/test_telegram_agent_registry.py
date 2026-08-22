from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_agent_registry as registry  # noqa: E402


class TelegramAgentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.sessions = self.home / ".codex" / "sessions" / "2026" / "07" / "19"
        self.sessions.mkdir(parents=True)
        self.log_dir = self.root / "telegram-runtime"
        self.env = mock.patch.dict(
            os.environ,
            {
                "TELEAGENT_LOG_DIR": str(self.log_dir),
                "TELEAGENT_AGENT_DIR": str(self.log_dir / "agents"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _session(self, name: str, timestamp: str, cwd: Path | None = None) -> Path:
        path = self.sessions / name
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": timestamp,
                    "payload": {"cwd": str(cwd or self.repo)},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_new_process_rejects_old_session_seen_through_process_fd(self) -> None:
        old_session = self._session("old.jsonl", "2026-07-19T01:00:00Z")
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None
        meta = {
            "created_ts": agent_started,
            "launch_source": "start_codex_agent.sh",
            "repo_root": str(self.repo),
        }

        self.assertFalse(registry.codex_session_matches_agent(meta, old_session))

    def test_refresh_does_not_link_stale_process_fd_candidate(self) -> None:
        old_session = self._session("old.jsonl", "2026-07-19T01:00:00Z")
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None
        meta = {
            "agent_id": "agent-test",
            "codex_session_path": None,
            "created_ts": agent_started,
            "launch_source": "start_codex_agent.sh",
            "repo_root": str(self.repo),
            "target_pane": "tele-agent:codex.0",
        }

        with mock.patch.object(
            registry,
            "codex_session_for_pane",
            return_value=(old_session, "process_fd"),
        ):
            refreshed = registry.refresh_codex_session_link(
                meta, target_pane="tele-agent:codex.0"
            )

        self.assertIsNone(refreshed["codex_session_path"])

    def test_launch_fallback_uses_embedded_timestamp_not_recent_mtime(self) -> None:
        old_session = self._session("old.jsonl", "2026-07-19T01:00:00Z")
        old_session.touch()
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None

        with mock.patch.object(registry.Path, "home", return_value=self.home):
            found, reason = registry.recent_codex_session(
                agent_started, repo_root=self.repo
            )

        self.assertIsNone(found)
        self.assertEqual(reason, "mtime_fallback_not_found")

    def test_launch_fallback_accepts_matching_new_session(self) -> None:
        new_session = self._session("new.jsonl", "2026-07-19T04:00:01Z")
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None

        with mock.patch.object(registry.Path, "home", return_value=self.home):
            found, reason = registry.recent_codex_session(
                agent_started, repo_root=self.repo
            )

        self.assertEqual(found, new_session)
        self.assertEqual(reason, "mtime_fallback")

    def test_refresh_can_find_delayed_first_session_without_open_fd(self) -> None:
        new_session = self._session("new.jsonl", "2026-07-19T04:00:01Z")
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None
        meta = {
            "agent_id": "agent-test",
            "agent_jsonl": str(self.log_dir / "agents" / "agent-test" / "events.jsonl"),
            "codex_session_path": None,
            "created_ts": agent_started,
            "launch_source": "start_codex_agent.sh",
            "repo_root": str(self.repo),
            "target_pane": "tele-agent:codex.0",
        }

        with (
            mock.patch.object(registry.Path, "home", return_value=self.home),
            mock.patch.object(
                registry,
                "codex_session_for_pane",
                return_value=(None, "process_fd_not_found"),
            ),
        ):
            refreshed = registry.refresh_codex_session_link(
                meta, target_pane="tele-agent:codex.0"
            )

        self.assertEqual(refreshed["codex_session_path"], str(new_session))
        self.assertEqual(refreshed["codex_session_detection"], "mtime_fallback")

    def test_process_fd_discovery_ignores_nested_helper_codex(self) -> None:
        main_session = self._session("main.jsonl", "2026-07-19T04:00:00Z")
        helper_session = self._session("helper.jsonl", "2026-07-19T05:00:00Z")
        helper_session.touch()
        rows = [
            (100, 1, "bash"),
            (110, 100, "bash"),
            (120, 110, "codex"),
            (130, 120, "bash"),
            (140, 130, "codex"),
        ]

        def open_sessions(pid: int) -> list[Path]:
            return {120: [main_session], 140: [helper_session]}.get(pid, [])

        with (
            mock.patch.object(registry, "tmux_pane_pid", return_value=100),
            mock.patch.object(registry, "ps_rows", return_value=rows),
            mock.patch.object(
                registry, "session_files_open_by_pid", side_effect=open_sessions
            ) as session_files,
        ):
            found, reason = registry.codex_session_for_pane("tele-agent:codex.0")

        self.assertEqual(found, main_session)
        self.assertEqual(reason, "process_fd")
        session_files.assert_called_once_with(120)

    def test_process_fd_discovery_keeps_preferred_session_opened_by_main(self) -> None:
        main_session = self._session("main.jsonl", "2026-07-19T04:00:00Z")
        helper_session = self._session("helper.jsonl", "2026-07-19T05:00:00Z")
        helper_session.touch()
        rows = [(100, 1, "bash"), (120, 100, "codex")]

        with (
            mock.patch.object(registry, "tmux_pane_pid", return_value=100),
            mock.patch.object(registry, "ps_rows", return_value=rows),
            mock.patch.object(
                registry,
                "session_files_open_by_pid",
                return_value=[main_session, helper_session],
            ),
        ):
            found, reason = registry.codex_session_for_pane(
                "tele-agent:codex.0", preferred_session_path=main_session
            )

        self.assertEqual(found, main_session)
        self.assertEqual(reason, "process_fd")

    def test_process_fd_discovery_uses_root_session_when_link_is_missing(self) -> None:
        main_session = self._session("main.jsonl", "2026-07-19T04:00:00Z")
        helper_session = self._session("helper.jsonl", "2026-07-19T05:00:00Z")
        helper_session.touch()
        rows = [(100, 1, "bash"), (120, 100, "codex")]

        with (
            mock.patch.object(registry, "tmux_pane_pid", return_value=100),
            mock.patch.object(registry, "ps_rows", return_value=rows),
            mock.patch.object(
                registry,
                "session_files_open_by_pid",
                return_value=[helper_session, main_session],
            ),
        ):
            found, reason = registry.codex_session_for_pane("tele-agent:codex.0")

        self.assertEqual(found, main_session)
        self.assertEqual(reason, "process_fd")

    def test_refresh_keeps_valid_link_when_process_fd_is_temporarily_missing(self) -> None:
        current_session = self._session("current.jsonl", "2026-07-19T04:00:00Z")
        agent_started = registry.iso_timestamp_epoch("2026-07-19T04:00:00Z")
        assert agent_started is not None
        meta = {
            "agent_id": "agent-test",
            "codex_session_path": str(current_session),
            "codex_session_detection": "process_fd",
            "created_ts": agent_started,
            "launch_source": "start_codex_agent.sh",
            "repo_root": str(self.repo),
            "target_pane": "tele-agent:codex.0",
        }

        with (
            mock.patch.object(
                registry,
                "codex_session_for_pane",
                return_value=(None, "process_fd_not_found"),
            ),
            mock.patch.object(registry, "recent_codex_session") as fallback,
        ):
            refreshed = registry.refresh_codex_session_link(meta)

        self.assertEqual(refreshed["codex_session_path"], str(current_session))
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
