"""Delivery services for the Telegram relay."""

from __future__ import annotations


import json
import os
import re
import time
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402
from notify import redact
from telegram_format import telegram_final_marker_suffix

from .events import TurnDelivery, turn_identity
from . import settings as _settings
from . import messages as _messages
from . import routing as _routing
from . import sessions as _sessions
from . import state as _state
from . import transport as _transport


def drain_agent_outbox(
    token: str,
    chat_id: str,
    outbox_path: Path,
    offset_path: Path,
    log_path: Path | None = None,
    max_ack_age_seconds: int = 120,
    max_progress_age_seconds: int = 300,
    route_state_path: Path | None = None,
    is_group_route: bool = False,
) -> None:
    if not outbox_path.exists():
        return
    offset = 0
    if offset_path.exists():
        try:
            offset = int(offset_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            offset = 0
    size = outbox_path.stat().st_size
    if offset > size:
        offset = 0
    with outbox_path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                offset = handle.tell()
                continue
            text = str(record.get("text") or "").strip()
            if not text:
                offset = handle.tell()
                continue
            phase = str(record.get("phase") or "").strip().lower()
            if is_group_route and phase != "final":
                offset = handle.tell()
                continue
            record_ts = record.get("ts")
            age_limit = None
            if phase == "ack":
                age_limit = max_ack_age_seconds
            elif phase == "progress":
                age_limit = max_progress_age_seconds
            if age_limit is not None and age_limit >= 0:
                try:
                    age_seconds = int(time.time()) - int(record_ts)
                except (TypeError, ValueError):
                    age_seconds = None
                if age_seconds is not None and age_seconds > age_limit:
                    if log_path is not None:
                        _state.append_jsonl(
                            log_path,
                            {
                                "ts": int(time.time()),
                                "agent_id": record.get("agent_id"),
                                "event": "agent_outbox_skipped_stale",
                                "phase": phase,
                                "title": record.get("title"),
                                "line_start": line_start,
                                "age_seconds": age_seconds,
                                "age_limit_seconds": age_limit,
                                "next_offset": handle.tell(),
                            },
                        )
                    agent_registry.append_agent_event(
                        record.get("agent_jsonl"),
                        {
                            "agent_id": record.get("agent_id"),
                            "event": "agent_outbox_skipped_stale",
                            "phase": phase,
                            "title": record.get("title"),
                            "line_start": line_start,
                            "age_seconds": age_seconds,
                            "age_limit_seconds": age_limit,
                            "next_offset": handle.tell(),
                        },
                    )
                    offset = handle.tell()
                    continue
            try:
                _transport.send_reply(token, chat_id, text)
            except Exception as exc:
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(time.time()),
                            "agent_id": record.get("agent_id"),
                            "event": "agent_outbox_send_failed",
                            "phase": record.get("phase"),
                            "title": record.get("title"),
                            "line_start": line_start,
                            "error": str(exc),
                        },
                    )
                agent_registry.append_agent_event(
                    record.get("agent_jsonl"),
                    {
                        "agent_id": record.get("agent_id"),
                        "event": "agent_outbox_send_failed",
                        "phase": record.get("phase"),
                        "title": record.get("title"),
                        "line_start": line_start,
                        "error": str(exc),
                    },
                )
                _state.write_offset(offset_path, line_start)
                return
            if log_path is not None:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "agent_id": record.get("agent_id"),
                        "event": "agent_outbox_sent",
                        "phase": record.get("phase"),
                        "title": record.get("title"),
                        "line_start": line_start,
                        "next_offset": handle.tell(),
                    },
                )
            agent_registry.append_agent_event(
                record.get("agent_jsonl"),
                {
                    "agent_id": record.get("agent_id"),
                    "event": "agent_outbox_sent",
                    "phase": record.get("phase"),
                    "title": record.get("title"),
                    "line_start": line_start,
                    "next_offset": handle.tell(),
                },
            )
            if route_state_path is not None and phase == "final":
                _routing.clear_reply_route_chat_id(route_state_path, chat_id)
            offset = handle.tell()
    _state.write_offset(offset_path, offset)


def format_forwarded_agent_message(
    text: str,
    phase: str,
    env: dict[str, str],
    max_chars: int,
    *,
    add_final_marker: bool = True,
) -> tuple[str, bool]:
    cleaned = redact(text, env).strip()
    cleaned = _settings.LEGACY_REPLY_PREFIX_RE.sub("", cleaned, count=1).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not cleaned:
        return "", False
    is_final = phase == "final_answer"
    # Keep the marker inline for normal prose, but outside a trailing fenced
    # code block, quote, list, heading, or other block construct.
    suffix = (
        telegram_final_marker_suffix(cleaned) if is_final and add_final_marker else ""
    )
    available = max(1, max_chars - len(suffix))
    truncated = len(cleaned) > available
    if truncated:
        cleaned = cleaned[: max(1, available - 1)].rstrip() + "…"
    return cleaned + suffix, truncated


def codex_agent_message(record: dict[str, Any]) -> tuple[str, str] | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "response_item":
        # Codex rollouts write assistant text as response_item payloads with
        # role=assistant and phase=final_answer / commentary. Only assistant
        # content should be forwarded to Telegram.
        if payload.get("type") == "message" and payload.get("role") in (
            None,
            "assistant",
        ):
            parts: list[str] = []
            content = payload.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = str(part.get("text") or "").strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts), str(payload.get("phase") or "")
        return None
    if record.get("type") != "event_msg":
        return None
    if payload.get("type") == "agent_message":
        return str(payload.get("message") or "").strip(), str(
            payload.get("phase") or ""
        )
    if payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "AgentMessage":
        return None
    parts = []
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "Text":
                continue
            text = str(part.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts), str(item.get("phase") or "")


def codex_agent_message_id(record: dict[str, Any]) -> str:
    """Return the stable Codex message ID shared by duplicate event schemas."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if record.get("type") == "response_item":
        return str(payload.get("id") or "")
    if record.get("type") != "event_msg":
        return ""
    if payload.get("type") == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "AgentMessage":
            return str(item.get("id") or "")
    return str(payload.get("id") or "")


def instance_suppresses_final_marker() -> bool:
    """Return whether this instance opts out of the trailing final marker."""
    return os.environ.get("TELEAGENT_SUPPRESS_FINAL_MARKER", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def drain_codex_agent_messages(
    token: str,
    chat_id: str,
    meta: dict[str, Any] | None,
    state_path: Path,
    log_path: Path | None,
    env: dict[str, str],
    max_commentary_chars: int = 1200,
    max_final_chars: int = 3600,
    sessions_root: Path | None = None,
    route_state_path: Path | None = None,
    is_group_route: bool = False,
    max_group_final_chars: int = 900,
) -> int:
    if not meta or not meta.get("codex_session_path"):
        return 0
    session_path = Path(str(meta["codex_session_path"]))
    if not _sessions.valid_codex_session_for_agent(
        meta, session_path, sessions_root=sessions_root
    ):
        return 0

    state = _state.read_json_object(state_path)
    session_text = str(session_path.resolve())
    agent_id = str(meta.get("agent_id") or "")
    size = session_path.stat().st_size
    same_agent = bool(agent_id) and state.get("agent_id") == agent_id
    same_session = same_agent and state.get("session_path") == session_text
    turn = TurnDelivery.restore(state if same_session else {}, chat_id)
    if same_session:
        try:
            offset = int(state.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0 or offset > size:
            # Corrupt or stale progress must never turn into a replay. Tail the
            # current file and wait for new messages instead.
            offset = size
        last_message_id = str(state.get("last_message_id") or "")
        turn.route_id = str(state.get("active_route_id") or "")
        turn.chat_id = str(state.get("active_chat_id") or "")
        turn.is_group = bool(state.get("active_is_group"))
    else:
        last_message_id = ""
        turn.route_id = ""
        turn.chat_id = ""
        turn.is_group = False
        session_started = _sessions.iso_timestamp_epoch(
            _sessions.codex_session_metadata(session_path).get("timestamp")
        )
        try:
            agent_started = float(meta.get("created_ts"))
        except (TypeError, ValueError):
            agent_started = float("inf")
        session_matches_launch = (
            session_started is not None
            and session_started
            >= agent_started - agent_registry.SESSION_START_SLOP_SECONDS
        )
        # Read from the beginning only when the embedded session timestamp
        # proves that a newly registered agent created the session. A path
        # change within the same agent is never allowed to reset progress: it
        # may be a transient helper rollout, and replaying it is worse than
        # waiting for the next newly appended message.
        offset = 0 if not same_agent and session_matches_launch else size
        _persist_agent_message_state(
            state_path,
            agent_id=agent_id,
            session_path=session_text,
            offset=offset,
            last_message_id=last_message_id,
            delivery_state=turn.snapshot(),
        )
        if offset == size:
            return 0

    sent = 0
    with session_path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line_start = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                handle.seek(line_start)
                break
            next_offset = handle.tell()
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                offset = next_offset
                continue

            identity = turn_identity(record)
            if identity:
                turn.begin(identity)
            message_route_id = _messages.telegram_route_id_from_codex_record(record)
            if message_route_id:
                turn.bind(
                    message_route_id,
                    lambda key: (
                        _routing.reply_route_details(route_state_path, key)
                        if route_state_path is not None
                        else None
                    ),
                )
                if turn.conflicted and log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(time.time()),
                            "event": "cross_chat_turn_quarantined",
                            "turn_id": turn.turn_id,
                            "route_id": message_route_id,
                        },
                    )
            agent_message = codex_agent_message(record)
            if agent_message is None:
                offset = next_offset
                continue
            raw_text, phase = agent_message
            if not raw_text:
                offset = next_offset
                continue
            message_id = codex_agent_message_id(record)
            message_key = turn.message_key(raw_text, phase)
            if message_key in turn.recent:
                offset = next_offset
                continue
            if message_id and message_id == last_message_id:
                offset = next_offset
                _persist_agent_message_state(
                    state_path,
                    agent_id=agent_id,
                    session_path=session_text,
                    offset=offset,
                    last_message_id=last_message_id,
                    delivery_state=turn.snapshot(),
                    active_route_id=turn.route_id,
                    active_chat_id=turn.chat_id,
                    active_is_group=turn.is_group,
                )
                continue
            target_chat_id = turn.chat_id or chat_id
            target_is_group = bool(turn.chat_id and turn.is_group)
            if not turn.chat_id and route_state_path is None:
                target_is_group = bool(is_group_route)
            if target_is_group and phase != "final_answer":
                offset = next_offset
                continue
            max_chars = (
                max_group_final_chars
                if target_is_group and phase == "final_answer"
                else (
                    max_final_chars if phase == "final_answer" else max_commentary_chars
                )
            )
            outgoing, truncated = format_forwarded_agent_message(
                raw_text,
                phase,
                env,
                max(max_chars, len(raw_text) + 16)
                if phase == "final_answer"
                else max_chars,
                add_final_marker=(
                    not target_is_group and not instance_suppresses_final_marker()
                ),
            )
            if not outgoing.strip():
                offset = next_offset
                continue
            try:
                reply_options = {}
                if turn.topic_id is not None:
                    reply_options["message_thread_id"] = turn.topic_id
                if turn.source_message_id is not None:
                    reply_options["reply_to_message_id"] = turn.source_message_id
                _transport.send_reply(token, target_chat_id, outgoing, **reply_options)
            except Exception as exc:
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(time.time()),
                            "agent_id": agent_id or None,
                            "event": "codex_agent_message_send_failed",
                            "phase": phase,
                            "line_start": line_start,
                            "error": _transport.short_error(exc, env),
                        },
                    )
                _persist_agent_message_state(
                    state_path,
                    agent_id=agent_id,
                    session_path=session_text,
                    offset=line_start,
                    last_message_id=last_message_id,
                    delivery_state=turn.snapshot(),
                    active_route_id=turn.route_id,
                    active_chat_id=turn.chat_id,
                    active_is_group=turn.is_group,
                )
                return sent
            sent += 1
            turn.delivered(message_key, phase)
            offset = next_offset
            if message_id:
                last_message_id = message_id
            event = {
                "agent_id": agent_id or None,
                "event": "codex_agent_message_sent",
                "phase": phase,
                "chars": len(outgoing),
                "truncated": truncated,
                "line_start": line_start,
                "next_offset": next_offset,
                "codex_session_path": session_text,
                "chat_id": target_chat_id,
                "is_group": target_is_group,
                "route_id": turn.route_id or None,
            }
            if log_path is not None:
                _state.append_jsonl(log_path, {"ts": int(time.time()), **event})
            agent_registry.append_agent_event(meta, event)
            if route_state_path is not None and phase == "final_answer":
                _routing.clear_reply_route_chat_id(
                    route_state_path,
                    target_chat_id,
                    route_id=turn.route_id or None,
                )
                turn.route_id = ""
                turn.chat_id = ""
                turn.is_group = False
            _persist_agent_message_state(
                state_path,
                agent_id=agent_id,
                session_path=session_text,
                offset=offset,
                last_message_id=last_message_id,
                delivery_state=turn.snapshot(),
                active_route_id=turn.route_id,
                active_chat_id=turn.chat_id,
                active_is_group=turn.is_group,
            )

    _persist_agent_message_state(
        state_path,
        agent_id=agent_id,
        session_path=session_text,
        offset=offset,
        last_message_id=last_message_id,
        delivery_state=turn.snapshot(),
        active_route_id=turn.route_id,
        active_chat_id=turn.chat_id,
        active_is_group=turn.is_group,
    )
    return sent


def _persist_agent_message_state(
    state_path: Path,
    *,
    agent_id: str,
    session_path: str,
    offset: int,
    last_message_id: str,
    active_route_id: str = "",
    active_chat_id: str = "",
    active_is_group: bool = False,
    anchor_ts: float | None = None,
    delivery_state: dict[str, Any] | None = None,
) -> None:
    value: dict[str, Any] = {
        "agent_id": agent_id,
        "session_path": session_path,
        "offset": offset,
        "last_message_id": last_message_id,
        "active_route_id": active_route_id,
        "active_chat_id": active_chat_id,
        "active_is_group": bool(active_is_group),
        "updated_ts": int(time.time()),
    }
    if delivery_state is not None:
        value.update(delivery_state)
    if anchor_ts is not None:
        value["anchor_ts"] = anchor_ts
    _state.write_json_object(state_path, value)
