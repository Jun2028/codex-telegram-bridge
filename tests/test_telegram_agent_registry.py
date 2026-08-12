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


if __name__ == "__main__":
    unittest.main()
