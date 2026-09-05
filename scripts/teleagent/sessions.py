"""Sessions services for the Telegram relay."""

from __future__ import annotations


import json
from datetime import datetime
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402


def codex_session_metadata(session_path: Path) -> dict[str, Any]:
    try:
        with session_path.open("r", encoding="utf-8") as handle:
            for _ in range(32):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta" and isinstance(
                    record.get("payload"), dict
                ):
                    return {"timestamp": record.get("timestamp"), **record["payload"]}
    except OSError:
        return {}
    return {}


def iso_timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def valid_codex_session_for_agent(
    meta: dict[str, Any],
    session_path: Path,
    sessions_root: Path | None = None,
) -> bool:
    try:
        resolved_session = session_path.resolve()
        if not resolved_session.is_file():
            return False
        expected_home = agent_registry.codex_home_from_meta(meta)
        if expected_home is not None:
            allowed_roots = [(expected_home / "sessions").resolve()]
        elif sessions_root is not None:
            allowed_roots = [sessions_root.resolve()]
        else:
            allowed_roots = [(Path.home() / ".codex" / "sessions").resolve()]
            target_pane = str(meta.get("target_pane") or "")
            if target_pane:
                allowed_roots.extend(
                    (home / "sessions").resolve()
                    for home in agent_registry.codex_homes_for_pane(target_pane)
                )
        if not any(resolved_session.is_relative_to(root) for root in allowed_roots):
            return False
    except OSError:
        return False

    session_meta = codex_session_metadata(resolved_session)
    session_cwd = str(session_meta.get("cwd") or "")
    repo_root = str(meta.get("repo_root") or "")
    if not session_cwd or not repo_root:
        return False
    if not agent_registry.codex_session_cwd_matches_agent(meta, session_cwd):
        return False

    if not agent_registry.codex_session_matches_agent(meta, resolved_session):
        return False

    if meta.get("codex_session_detection") == "process_fd":
        return True

    # A fallback session must have been created with this agent. This prevents
    # a stale pane registry from following whichever unrelated Codex session
    # happened to be modified most recently.
    session_started = iso_timestamp_epoch(session_meta.get("timestamp"))
    try:
        agent_started = float(meta.get("created_ts"))
    except (TypeError, ValueError):
        return False
    return session_started is not None and session_started >= agent_started - 5
