"""Read-only evidence for archived delivery checks; never replay or clear work."""

from __future__ import annotations

import json
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import telegram_agent_registry as registry

from .settings import SGT


def requested_at(item):
    message = (
        (item.get("update") or {}).get("message")
        or (item.get("update") or {}).get("edited_message")
        or {}
    )
    for value in (
        item.get("created_ts"),
        item.get("queued_ts"),
        item.get("received_ts"),
        message.get("date"),
        item.get("failed_ts"),
        item.get("stalled_ts"),
    ):
        try:
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                return float(value)
        except (ValueError, TypeError):
            continue
    return 0


@lru_cache(maxsize=128)
def _receipt_in_file(path_text, _mtime_ns, _size, expected):
    # History is an optional read, with a bound on old/very large rollouts.
    # Only exact root user input is evidence, never quoted assistant/tool text.
    remaining = 64 * 1024 * 1024
    try:
        with Path(path_text).open("rb") as handle:
            while remaining > 0:
                raw = handle.readline(remaining + 1)
                remaining -= len(raw)
                if not raw or remaining < 0 or not raw.endswith(b"\n"):
                    break
                if b'"user"' not in raw and b'"user_message"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                text = ""
                if (
                    record.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                    and isinstance(payload.get("content"), list)
                ):
                    text = "\n".join(
                        str(part.get("text") or "")
                        for part in payload.get("content", [])
                        if isinstance(part, dict)
                    )
                elif (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "user_message"
                ):
                    text = str(payload.get("message") or "")
                if text == expected or text.startswith(expected + "\n"):
                    return True
    except OSError:
        pass
    return False


def receipt_status(item):
    expected = str(item.get("relay_text") or "")
    paths = [item.get("session_path"), item.get("delivery_session_path")]
    if item.get("agent_id"):
        meta = registry.load_agent_meta(agent_id=item["agent_id"]) or {}
        paths.append(meta.get("codex_session_path"))
    available = False
    for value in dict.fromkeys(path for path in paths if path):
        path = Path(value)
        try:
            info = path.stat()
        except OSError:
            continue
        available = True
        if expected and _receipt_in_file(
            str(path), info.st_mtime_ns, info.st_size, expected
        ):
            return "received by agent (receipt found later)"
    return (
        "receipt unconfirmed"
        if available
        else "receipt cannot be verified; session log unavailable"
    )


REASONS = {
    "stale_pending_cleared": "confirmation timed out",
    "marker_absent_unconfirmed_same_process": "confirmation marker was not observed",
    "enter_retry_unconfirmed": "Enter retry was not confirmed",
    "recovery_attempt_limit_reached": "confirmation retries ended",
}


def format_history(items, is_group):
    if not items:
        return "No archived delivery checks in this chat."
    lines = [
        f"Delivery history: {len(items)} archived check(s).",
        "These records track receipt by the agent.",
    ]
    for item in sorted(items, key=requested_at, reverse=True)[:8]:
        message = (
            (item.get("update") or {}).get("message")
            or (item.get("update") or {}).get("edited_message")
            or {}
        )
        identifier = message.get("message_id") or item.get("message_id") or "?"
        stamp = requested_at(item)
        try:
            date = (
                datetime.fromtimestamp(stamp, SGT).strftime("%d %b %H:%M")
                if stamp
                else "date unavailable"
            )
        except (ValueError, OSError, OverflowError):
            date = "date unavailable"
        outcome = (
            receipt_status(item)
            if item.get("_receipt_check")
            else "outcome unconfirmed"
        )
        reason = REASONS.get(item.get("stalled_reason"), "")
        if not reason and not is_group:
            reason = str(
                item.get("error")
                or item.get("last_error")
                or item.get("stalled_reason")
                or ""
            )
        reason = " ".join(reason.split())[:130]
        if outcome.startswith("received by agent"):
            reason = ""
        lines.append(
            f"{date} · #{identifier} · {outcome}" + (f"; {reason}" if reason else "")
        )
    if len(items) > 8:
        lines.append("Showing the 8 most recent records.")
    lines.append("/queue returns to current waiting work.")
    return "\n".join(lines)
