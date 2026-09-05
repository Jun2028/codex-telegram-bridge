"""Status services for the Telegram relay."""

from __future__ import annotations


import argparse
from contextlib import closing
import json
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402

from . import settings as _settings
from . import lifecycle as _lifecycle
from . import models as _models
from . import processes as _processes
from . import sessions as _sessions
from . import state as _state
from . import submission as _submission


def format_uptime(seconds: float) -> str:
    """Render an elapsed duration as a compact human-readable string."""
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def codex_session_start_epoch(session_path: str | None) -> float | None:
    """Best-effort start time from the first line of a Codex session JSONL."""
    if not session_path:
        return None
    try:
        with open(session_path, encoding="utf-8") as handle:
            first = handle.readline()
        if not first:
            return None
        record = json.loads(first)
        return agent_registry.iso_timestamp_epoch(record.get("timestamp"))
    except (OSError, json.JSONDecodeError):
        return None


def codex_session_goal_status(session_path: str | Path | None) -> str:
    """Read the bound thread's persistent goal, without inspecting terminal prose."""
    if not session_path:
        return "unknown"
    path = Path(session_path)
    try:
        with path.open(encoding="utf-8") as handle:
            record = json.loads(handle.readline())
        if record.get("type") != "session_meta":
            return "unknown"
        thread_id = record["payload"]["id"]
        # Both live and archived rollouts belong to their own Codex home.
        session_root = next(
            parent
            for parent in path.parents
            if parent.name in {"sessions", "archived_sessions"}
        )
        db = session_root.parent / "goals_1.sqlite"
        with closing(
            sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as conn:
            row = conn.execute(
                "SELECT status FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            return "off"
        status = row[0]
        if status in {
            "active", "paused", "blocked", "usage_limited", "budget_limited", "complete"
        }:
            return status
    except (OSError, ValueError, KeyError, TypeError, StopIteration, sqlite3.Error):
        pass
    return "unknown"


def codex_session_context_snapshot(
    session_path: str | Path | None,
) -> dict[str, Any] | None:
    """Read current context usage and compaction count from a Codex session log.

    Codex writes a token_count event after each turn whose last_token_usage
    reflects the current context size, alongside the model context window.
    Compactions are recorded both as top-level ``compacted`` records and as
    ``context_compacted`` event messages; the former is preferred when present.
    """
    if not session_path:
        return None
    path = Path(session_path)
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return None
    with handle:
        context_tokens = None
        context_window = None
        compacted_records = 0
        context_compacted_events = 0
        for raw_line in handle:
            if "token_count" not in raw_line and "compacted" not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            record_type = record.get("type")
            if record_type == "compacted":
                compacted_records += 1
                continue
            if record_type != "event_msg":
                continue
            payload = record.get("payload") or {}
            payload_type = payload.get("type")
            if payload_type == "context_compacted":
                context_compacted_events += 1
                continue
            if payload_type != "token_count":
                continue
            info = payload.get("info") or {}
            last_usage = info.get("last_token_usage") or {}
            if isinstance(last_usage.get("input_tokens"), int):
                context_tokens = last_usage["input_tokens"]
            if isinstance(info.get("model_context_window"), int):
                context_window = info["model_context_window"]
    if (
        context_tokens is None
        and compacted_records == 0
        and context_compacted_events == 0
    ):
        return None
    return {
        "context_tokens": context_tokens,
        "context_window": context_window,
        "compactions": compacted_records or context_compacted_events,
    }


def latest_agent_message_text(session_path: str, max_chars: int = 280) -> str:
    """Return the newest assistant text from a rollout for /status."""
    try:
        size = Path(session_path).stat().st_size
    except OSError:
        return "(none)"
    read_from = max(0, size - 512 * 1024)
    last_text = ""
    last_timestamp: str | None = None
    try:
        with Path(session_path).open("rb") as handle:
            handle.seek(read_from)
            for raw_line in handle:
                try:
                    record = json.loads(raw_line.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                content = None
                if (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "agent_message"
                ):
                    content = [{"text": payload.get("message") or ""}]
                if (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "item_completed"
                ):
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "AgentMessage":
                        content = item.get("content")
                if payload.get("type") == "message" and payload.get("role") in (
                    None,
                    "assistant",
                ):
                    content = payload.get("content")
                if isinstance(content, list):
                    text = " ".join(
                        str(part.get("text") or "").strip()
                        for part in content
                        if isinstance(part, dict)
                        and str(part.get("text") or "").strip()
                    ).strip()
                    if text:
                        last_text = text
                        last_timestamp = record.get("timestamp")
    except OSError:
        pass
    if not last_text:
        return "(none)"
    flat = " ".join(last_text.split())
    stamp = ""
    if isinstance(last_timestamp, str):
        try:
            parsed = datetime.fromisoformat(
                last_timestamp.replace("Z", "+00:00")
            ).astimezone(_settings.SGT)
            stamp = parsed.strftime("%H:%M:%S") + " "
        except ValueError:
            stamp = ""
    snippet = flat[: max(1, max_chars - 1)].rstrip() + (
        "…" if len(flat) > max_chars else ""
    )
    return f"{stamp}{snippet}"


def format_system_status(
    session: str,
    target_pane: str,
    _tmux_lines: int,
    args: argparse.Namespace | None = None,
    auth_failure: dict[str, Any] | None = None,
    audience_chat_id: str | None = None,
) -> str:
    """Compact /status snapshot with model and agent uptime, not host dumps."""
    now = datetime.now(_settings.SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
    if _processes.tmux_target_exists(target_pane):
        if _processes.codex_target_ready(target_pane):
            process = "codex"
        else:
            process = _processes.tmux_pane_command(target_pane) or "(unknown)"
    else:
        process = "(pane missing)"
    desired = (
        _lifecycle.agent_desired_state(args)
        if args is not None
        else _settings.AGENT_DESIRED_RUNNING
    )
    auth_text = "reauth required" if auth_failure else "no failure detected"
    meta = agent_registry.active_agent_for_pane(target_pane)
    if meta:
        meta = agent_registry.refresh_codex_session_link(meta, target_pane=target_pane)
    session_path = str(meta.get("codex_session_path") or "") if meta else ""
    if not session_path:
        fallback_session, _fallback_method = agent_registry.codex_session_for_pane(
            target_pane
        )
        if fallback_session:
            session_path = str(fallback_session)
    if args is not None:
        state_text = getattr(args, "agent_message_state_path", None)
        if state_text:
            drain_state = _state.read_json_object(Path(state_text))
            try:
                anchor_age = time.time() - float(drain_state.get("anchor_ts") or 0)
            except (TypeError, ValueError):
                anchor_age = float("inf")
            anchored = drain_state.get("session_path")
            if (
                meta
                and anchor_age <= 3600
                and isinstance(anchored, str)
                and anchored
                and _sessions.valid_codex_session_for_agent(meta, Path(anchored))
            ):
                session_path = anchored
    current = _models.codex_session_model_and_reasoning_effort(session_path)
    if current is None:
        current = _models.current_codex_model_and_reasoning_effort(
            target_pane, session_path
        )
    model_text = f"{current[0]} / {current[1]}" if current else "(unknown)"
    uptime_epoch = None
    if meta:
        try:
            uptime_epoch = float(meta.get("created_ts"))
        except (TypeError, ValueError):
            uptime_epoch = None
    if uptime_epoch is None:
        uptime_epoch = codex_session_start_epoch(session_path)
    uptime_text = (
        format_uptime(time.time() - uptime_epoch)
        if uptime_epoch is not None
        else "(unknown)"
    )
    activity = (
        "stopped"
        if desired == _settings.AGENT_DESIRED_STOPPED
        else (
            "working"
            if process == "codex"
            and session_path
            and _submission.codex_session_turn_active(Path(session_path))
            else "idle"
            if process == "codex"
            else "recovering (agent process missing)"
        )
    )
    if auth_failure:
        activity = "sign-in required — /reauth in private"
    goal_text = codex_session_goal_status(session_path)
    lines = [
        f"{socket.gethostname()} · {now}",
        f"state: {desired} · {activity}",
        f"goal mode: {goal_text}",
        f"model: {model_text}",
        f"auth: {auth_text}",
        f"uptime: {uptime_text}",
    ]
    if args is not None:
        queue_path = getattr(args, "relay_queue_state_path", "")
        queue_state = _state.read_json_object(Path(queue_path)) if queue_path else {}

        def visible(item):
            update = item.get("update") or {}
            message = update.get("message") or update.get("edited_message") or {}
            return (
                audience_chat_id is None
                or str((message.get("chat") or {}).get("id")) == audience_chat_id
            )

        waiting = sum(visible(item) for item in queue_state.get("tasks", []))
        lines.append(f"queue: {waiting} waiting — /queue")
        health_path = getattr(args, "health_state_path", "")
        health = _state.read_json_object(Path(health_path)) if health_path else {}
        if health.get("control_update_id"):
            age = format_uptime(
                time.time() - float(health.get("control_started_ts") or time.time())
            )
            lines.append(f"control: processing for {age}")
        delivered = health.get("delivery_ok_ts")
        if health.get("delivery_error"):
            lines.append(
                "reply delivery: failing; pending replies are retained for retry"
            )
        if health.get("control_error"):
            lines.append(
                "background checks: failing; inspect the listener error notice"
            )
        if delivered:
            lines.append(
                f"reply check: {format_uptime(time.time() - float(delivered))} ago"
            )
        else:
            lines.append("reply check: no successful check recorded")
        reply_path = getattr(args, "control_reply_state_path", "")
        replies = _state.read_json_object(Path(reply_path)) if reply_path else {}
        pending_replies = sum(
            audience_chat_id is None or str(item.get("chat_id")) == audience_chat_id
            for item in replies.get("pending", [])
        )
        if pending_replies:
            lines.append(f"control replies waiting for delivery: {pending_replies}")
    if session_path:
        context_snapshot = codex_session_context_snapshot(session_path)
        if context_snapshot:
            used, window = (
                context_snapshot.get("context_tokens"),
                context_snapshot.get("context_window"),
            )
            if isinstance(used, int) and isinstance(window, int) and window > 0:
                lines.append(
                    f"context: {used * 100 // window}% used · {context_snapshot.get('compactions', 0)} compactions"
                )
    # Shared agent history is never previewed in status, particularly in groups.
    return "\n".join(lines)
