"""Messages services for the Telegram relay."""

from __future__ import annotations


import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import settings as _settings
from . import attachments as _attachments
from . import models as _models


def normalize_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    if not stripped.startswith("/"):
        return "(agent-message)", stripped
    parts = stripped.split(maxsplit=1)
    first, rest = parts[0], parts[1] if len(parts) > 1 else ""
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def sender_label(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    parts = []
    if sender.get("username"):
        parts.append(f"@{sender['username']}")
    name = " ".join(
        str(sender.get(key, "")).strip() for key in ("first_name", "last_name")
    ).strip()
    if name:
        parts.append(name)
    if sender.get("id"):
        parts.append(str(sender["id"]))
    return " | ".join(parts) or "unknown"


def visible_message_text(message: dict[str, Any]) -> str:
    """Return the complete user-visible text carried by a Telegram message."""
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def telegram_message_time(message: dict[str, Any]) -> str:
    raw_date = message.get("date")
    try:
        return (
            datetime.fromtimestamp(int(raw_date), timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown time"


def telegram_update_route_id(update: dict[str, Any]) -> str:
    """Return a stable, non-secret identifier for one inbound Telegram update."""
    raw_update_id = update.get("update_id")
    try:
        return f"u{int(raw_update_id)}"
    except (TypeError, ValueError):
        pass
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return ""
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    message_id = message.get("message_id")
    if not chat_id or message_id is None:
        return ""
    # Telegram update IDs are globally unique. This fallback is only for
    # direct/local callers that construct a message without an update_id.
    safe_chat_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", chat_id)
    try:
        safe_message_id = int(message_id)
    except (TypeError, ValueError):
        safe_message_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(message_id))
    return f"c{safe_chat_id}:m{safe_message_id}"


def telegram_route_id_from_text(text: str) -> str | None:
    match = _settings.TELEGRAM_ROUTE_ID_RE.match(text.lstrip())
    return match.group(1) if match is not None else None


def telegram_route_id_from_codex_record(record: dict[str, Any]) -> str | None:
    """Extract the relay binding from a persisted Codex user-message record."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    text = ""
    if (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ):
        content = payload.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
    elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = str(payload.get("message") or "")
    if not text:
        return None
    return telegram_route_id_from_text(text)


def format_reply_context(message: dict[str, Any]) -> str:
    replied = message.get("reply_to_message")
    if not isinstance(replied, dict):
        return ""
    replied_text = visible_message_text(replied)
    if not replied_text:
        replied_text = "[non-text Telegram message]"
    replied_id = replied.get("message_id")
    message_ref = f" message_id={replied_id}" if replied_id is not None else ""
    return (
        f"\n\n[REPLIED-TO TELEGRAM MESSAGE{message_ref} from {sender_label(replied)} "
        f"at {telegram_message_time(replied)}]\n{replied_text}\n"
        "[/REPLIED-TO TELEGRAM MESSAGE]"
    )


def format_agent_message(
    message: dict[str, Any],
    text: str,
    person_context: str = "",
    route_id: str = "",
) -> str:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    compact_text = text.strip()
    message_id = message.get("message_id")
    message_ref = f" message_id={message_id}" if message_id is not None else ""
    route_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(route_id or "").strip())
    route_ref = f" route_id={route_id}" if route_id else ""
    chat = message.get("chat") or {}
    source_type = str(chat.get("type") or "private")
    source_id = str(chat.get("id") or "unknown")
    source_ref = f" chat={source_type}:{source_id}"
    if isinstance(message.get("message_thread_id"), int):
        source_ref += f" topic={message['message_thread_id']}"
    reply_context = format_reply_context(message)
    memory_context = f"\n\n{person_context.strip()}" if person_context.strip() else ""
    return (
        f"[TELEGRAM USER MESSAGE{message_ref}{route_ref}{source_ref} from {sender_label(message)} at {ts}] {compact_text}"
        f"{reply_context}{memory_context} "
        "Your normal Codex agent_message events are forwarded to Telegram automatically. "
        "Keep user-facing updates concise. Do not call telegram_agent_reply.sh and do not add "
        "ACK/PROGRESS/FINAL labels; the bridge appends ∎ only to the final answer. "
        "One bot has one persistent agent. Answer for the originating chat shown above. "
        "A group audience must not receive content from private exchanges unless the owner explicitly shares it. "
        "Messages from another chat wait behind the active task; they do not change its reply destination."
    )


def parse_message_ids(payload: str) -> list[int]:
    ids: list[int] = []
    for raw_part in payload.replace(",", " ").split():
        try:
            ids.append(int(raw_part))
        except ValueError:
            raise ValueError(f"invalid message id: {raw_part}") from None
    return ids


def parse_count(payload: str, default: int, *, maximum: int = 10) -> int:
    stripped = payload.strip()
    if not stripped:
        return default
    try:
        count = int(stripped.split()[0])
    except ValueError:
        raise ValueError(f"invalid count: {stripped.split()[0]}") from None
    if count < 1:
        raise ValueError("count must be at least 1")
    if count > maximum:
        raise ValueError(f"count must be at most {maximum}")
    return count


def record_message_text(record: dict[str, Any]) -> str:
    document = record.get("document")
    if record.get("action") == "agent_document" and isinstance(document, dict):
        required = {"path", "original_name", "suffix", "size_bytes", "sha256"}
        if required.issubset(document):
            caption = str(record.get("text_full") or record.get("text") or "")
            return _attachments.format_inbound_document_text(document, caption)
    return str(record.get("text_full") or record.get("text") or "")


def iter_agent_message_records(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        raise FileNotFoundError(f"Telegram inbox log not found: {log_path}")

    records: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("action") not in _settings.RELAYED_AGENT_ACTIONS:
                continue
            if record.get("message_id") is None:
                continue
            if not record_message_text(record).strip():
                continue
            records.append(record)
    return records


def load_agent_records_by_message_id(
    log_path: Path, message_ids: list[int]
) -> list[dict[str, Any]]:
    wanted = set(message_ids)
    found: dict[int, dict[str, Any]] = {}
    if not log_path.exists():
        raise FileNotFoundError(f"Telegram inbox log not found: {log_path}")
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                message_id = int(record.get("message_id"))
            except (TypeError, ValueError):
                continue
            if (
                message_id in wanted
                and record.get("action") in _settings.RELAYED_AGENT_ACTIONS
            ):
                found[message_id] = record

    missing = [str(message_id) for message_id in message_ids if message_id not in found]
    if missing:
        raise LookupError(
            "missing relayed Telegram message id(s): " + ", ".join(missing)
        )
    return [found[message_id] for message_id in message_ids]


def load_recent_agent_records(
    log_path: Path, count: int, min_chars: int = 0
) -> list[dict[str, Any]]:
    records = [
        record
        for record in iter_agent_message_records(log_path)
        if len(record_message_text(record).strip()) >= min_chars
    ]
    if len(records) < count:
        raise LookupError(
            f"only found {len(records)} matching relayed Telegram message(s)"
        )
    return records[-count:]


def recent_messages_text(log_path: Path, count: int) -> str:
    records = load_recent_agent_records(log_path, count)
    lines = ["Recent relayed Telegram messages:"]
    for record in records:
        text = " ".join(record_message_text(record).strip().split())
        preview = text[:160] + ("..." if len(text) > 160 else "")
        truncated = bool(record.get("text_truncated")) or (
            "text_full" not in record and len(str(record.get("text") or "")) >= 4000
        )
        suffix = " truncated-log" if truncated else ""
        lines.append(
            f"- message_id={record.get('message_id')} update_id={record.get('update_id')} "
            f"time={record.get('telegram_iso')} chars={len(text)}{suffix}: {preview}"
        )
    return "\n".join(lines)


def format_replayed_messages(
    message: dict[str, Any],
    records: list[dict[str, Any]],
    log_path: Path,
    route_id: str = "",
) -> str:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    route_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(route_id or "").strip())
    route_ref = f" route_id={route_id}" if route_id else ""
    parts = [
        f"[TELEGRAM REPLAY{route_ref} from {sender_label(message)} at {ts}] "
        f"Replaying {len(records)} prior Telegram messages from {log_path}. "
        "Treat them as one continuous human instruction batch in the exact order below. "
        "Do not drop earlier items just because a later message is a continuation."
    ]
    for record in records:
        message_id = record.get("message_id")
        update_id = record.get("update_id")
        telegram_iso = record.get("telegram_iso") or "(unknown time)"
        text = " ".join(record_message_text(record).strip().split())
        parts.append(
            f"--- message_id={message_id} update_id={update_id} telegram_time={telegram_iso} ---\n{text}"
        )
    parts.append(
        "Act on the combined batch and keep user-facing agent messages concise; they are forwarded automatically. "
        "If any referenced message content is missing or truncated, stop and ask."
    )
    return "\n\n".join(parts)


def parse_agent_launch_payload(payload: str) -> tuple[str, str, bool]:
    """Parse optional positional model and reasoning overrides."""
    try:
        tokens = shlex.split(payload)
    except ValueError as exc:
        raise ValueError(f"invalid quoting: {exc}") from exc

    if len(tokens) > 2:
        raise ValueError(
            "agent lifecycle commands accept at most a model and reasoning level, "
            "and never a prompt"
        )
    model = _settings.DEFAULT_CODEX_AGENT_MODEL
    reasoning_effort = _settings.DEFAULT_CODEX_AGENT_REASONING_EFFORT
    if not tokens:
        return model, reasoning_effort, False
    first = tokens[0].lower()
    reasoning_explicit = len(tokens) == 2
    if len(tokens) == 1 and first in _settings.SUPPORTED_CODEX_REASONING_EFFORTS:
        reasoning_effort = first
    else:
        model = _models.normalize_codex_agent_model(first)
        if reasoning_explicit:
            reasoning_effort = tokens[1].lower()
            if reasoning_effort not in _settings.SUPPORTED_CODEX_REASONING_EFFORTS:
                raise ValueError(
                    "unknown reasoning level; use none, minimal, low, medium, "
                    "high, xhigh, max, or ultra"
                )
        else:
            reasoning_effort = _settings.LATEST_OPENAI_CODEX_AGENT_REASONING_EFFORT
    if (
        model
        in {
            _settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
            _settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
        }
        and not reasoning_explicit
    ):
        reasoning_effort = "max"
    _models.validate_model_reasoning_effort(model, reasoning_effort)
    return model, reasoning_effort, True
