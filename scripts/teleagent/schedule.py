"""Schedule services for the Telegram relay."""

from __future__ import annotations


import argparse
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import settings as _settings
from . import auth as _auth
from . import lifecycle as _lifecycle
from . import messages as _messages
from . import routing as _routing
from . import state as _state
from . import submission as _submission
from . import transport as _transport


def parse_timed_payload(payload: str) -> tuple[float, str]:
    parts = payload.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("usage: /timed HOURS MESSAGE")
    raw_hours, message_text = parts
    try:
        hours = float(raw_hours)
    except ValueError:
        raise ValueError(f"invalid hours: {raw_hours}") from None
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a positive finite number")
    message_text = message_text.strip()
    if not message_text:
        raise ValueError("timed message cannot be empty")
    return hours, message_text


def timed_message_snapshot(
    message: dict[str, Any],
    message_text: str,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "message_id": message.get("message_id"),
        "date": message.get("date"),
        "from": dict(message.get("from") or {}),
        "chat": dict(message.get("chat") or {}),
        "text": message_text,
    }
    if isinstance(message.get("message_thread_id"), int):
        snapshot["message_thread_id"] = message["message_thread_id"]
    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        snapshot["reply_to_message"] = {
            "message_id": replied.get("message_id"),
            "date": replied.get("date"),
            "from": dict(replied.get("from") or {}),
            "text": replied.get("text"),
            "caption": replied.get("caption"),
        }
    return snapshot


def write_timed_message_state(path: Path, value: dict[str, Any]) -> None:
    _state.write_json_object(path, value)
    path.chmod(0o600)


def schedule_timed_message(
    state_path: Path,
    message: dict[str, Any],
    message_text: str,
    hours: float,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], bool]:
    created_ts = time.time() if now is None else now
    chat_id = str((message.get("chat") or {}).get("id", ""))
    message_id = message.get("message_id")
    if not chat_id or message_id is None:
        raise ValueError("Telegram message is missing chat or message id")
    task_id = f"{chat_id}:{message_id}"
    due_ts = created_ts + hours * 3600.0
    try:
        datetime.fromtimestamp(due_ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("hours are outside the supported scheduling range") from None
    state = _state.read_json_object(state_path)
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    for task in tasks:
        if isinstance(task, dict) and task.get("id") == task_id:
            return task, False

    task = {
        "id": task_id,
        "status": "pending",
        "created_ts": created_ts,
        "due_ts": due_ts,
        "hours": hours,
        "message": timed_message_snapshot(message, message_text),
        "attempts": 0,
    }
    tasks.append(task)
    state.update({"version": 1, "updated_ts": created_ts, "tasks": tasks})
    write_timed_message_state(state_path, state)
    return task, True


def timed_message_task_chat_id(task: dict[str, Any]) -> str:
    snapshot = task.get("message")
    if isinstance(snapshot, dict):
        chat = snapshot.get("chat")
        if isinstance(chat, dict) and chat.get("id") is not None:
            return str(chat["id"])
    task_id = str(task.get("id") or "")
    return task_id.partition(":")[0] if ":" in task_id else ""


def timed_message_is_active(task: dict[str, Any]) -> bool:
    return (
        str(task.get("status") or "pending").strip().lower()
        in _settings.ACTIVE_TIMED_MESSAGE_STATUSES
    )


def timed_message_sort_key(
    task: dict[str, Any], index: int
) -> tuple[float, float, int]:
    def finite_timestamp(value: Any) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return float("inf")
        return timestamp if math.isfinite(timestamp) else float("inf")

    return (
        finite_timestamp(task.get("due_ts")),
        finite_timestamp(task.get("created_ts")),
        index,
    )


def timed_messages_for_chat(
    state_path: Path,
    chat_id: str,
) -> list[dict[str, Any]]:
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    indexed = [
        (index, task)
        for index, task in enumerate(raw_tasks)
        if (
            isinstance(task, dict)
            and timed_message_task_chat_id(task) == str(chat_id)
            and timed_message_is_active(task)
        )
    ]
    indexed.sort(key=lambda item: timed_message_sort_key(item[1], item[0]))
    return [task for _, task in indexed]


def remove_timed_messages(
    state_path: Path,
    chat_id: str,
    number: int | None = None,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    indexed = [
        (index, task)
        for index, task in enumerate(raw_tasks)
        if (
            isinstance(task, dict)
            and timed_message_task_chat_id(task) == str(chat_id)
            and timed_message_is_active(task)
        )
    ]
    indexed.sort(key=lambda item: timed_message_sort_key(item[1], item[0]))
    if number is None:
        selected = indexed
    else:
        if number < 1:
            raise ValueError("timed message number must be at least 1")
        if not indexed:
            raise ValueError("no active timed messages are stored")
        if number > len(indexed):
            raise ValueError(
                f"timed message number {number} is outside the current list "
                f"(1-{len(indexed)})"
            )
        selected = [indexed[number - 1]]
    if not selected:
        return []

    removed_indices = {index for index, _ in selected}
    state.update(
        {
            "version": 1,
            "updated_ts": time.time() if now is None else now,
            "tasks": [
                task
                for index, task in enumerate(raw_tasks)
                if index not in removed_indices
            ],
        }
    )
    write_timed_message_state(state_path, state)
    return [task for _, task in selected]


def escape_commonmark_text(value: str) -> str:
    return _settings.COMMONMARK_PUNCTUATION_RE.sub(r"\\\1", value)


def timed_message_preview(task: dict[str, Any]) -> str:
    snapshot = task.get("message")
    text = str(snapshot.get("text") or "") if isinstance(snapshot, dict) else ""
    compact = " ".join(text.split()) or "(empty message)"
    if len(compact) > _settings.TIMED_MESSAGE_LIST_PREVIEW_CHARS:
        compact = (
            compact[: _settings.TIMED_MESSAGE_LIST_PREVIEW_CHARS - 1].rstrip() + "…"
        )
    return escape_commonmark_text(compact)


def format_timed_message_fired(task: dict[str, Any]) -> str:
    snapshot = task.get("message")
    text = str(snapshot.get("text") or "").strip() if isinstance(snapshot, dict) else ""
    text = text or "(empty message)"
    quoted = "\n".join(
        f"> {escape_commonmark_text(line)}" if line else ">"
        for line in text.splitlines()
    )
    return f"**Timed message fired**\n\n{quoted}"


def timed_message_due_label(task: dict[str, Any]) -> str:
    try:
        due_ts = float(task.get("due_ts"))
        if not math.isfinite(due_ts):
            raise ValueError
        return datetime.fromtimestamp(due_ts, _settings.SGT).strftime(
            "%Y-%m-%d %H:%M:%S SGT"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"


def timed_message_status_label(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "pending").strip().lower()
    labels = {
        "pending": "Pending",
        "delivering": "Delivering",
        "submitted": "Submitted",
        "delivered": "Delivered",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }
    return labels.get(status, status.replace("_", " ").title() or "Unknown")


def format_timed_message_entry(number: int, task: dict[str, Any]) -> str:
    return (
        f"**{number}. {timed_message_status_label(task)}**\n"
        f"Due: `{timed_message_due_label(task)}`\n"
        f"\n**Message**\n> {timed_message_preview(task)}"
    )


def format_timed_message_list(tasks: list[dict[str, Any]]) -> list[str]:
    if not tasks:
        return ["**Active timed messages**\n\nNo active timed messages are scheduled."]

    chunks: list[str] = []
    current = f"**Active timed messages ({len(tasks)})**"
    for number, task in enumerate(tasks, start=1):
        entry = format_timed_message_entry(number, task)
        separator = "\n\n"
        if (
            len(current) + len(separator) + len(entry)
            > _settings.TIMED_MESSAGE_LIST_CHUNK_CHARS
        ):
            chunks.append(current)
            current = "**Active timed messages (continued)**\n\n" + entry
        else:
            current += separator + entry
    footer = (
        "\n\nUse `/timed remove N` to remove one, or `/timed remove` to remove all."
    )
    if len(current) + len(footer) > _settings.TIMED_MESSAGE_LIST_CHUNK_CHARS:
        chunks.append(current)
        current = "**Active timed messages**" + footer
    else:
        current += footer
    chunks.append(current)
    return chunks


def parse_timed_remove_number(payload: str) -> int | None:
    parts = payload.strip().split()
    if not parts or parts[0].lower() != "remove":
        raise ValueError("usage: /timed remove [NUMBER]")
    if len(parts) == 1:
        return None
    if len(parts) != 2:
        raise ValueError("usage: /timed remove [NUMBER]")
    try:
        number = int(parts[1])
    except ValueError:
        raise ValueError(f"invalid timed message number: {parts[1]}") from None
    if number < 1:
        raise ValueError("timed message number must be at least 1")
    return number


def timed_message_checkpoint(task: dict[str, Any]) -> tuple[Path, int] | None:
    session_path = task.get("session_path")
    if not isinstance(session_path, str) or not session_path:
        return None
    try:
        offset = max(0, int(task.get("session_offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    return Path(session_path), offset


def timed_message_is_confirmed(task: dict[str, Any]) -> bool:
    checkpoint = timed_message_checkpoint(task)
    marker = task.get("marker")
    return bool(
        checkpoint is not None
        and isinstance(marker, str)
        and marker
        and _submission.wait_for_codex_submission(checkpoint, marker, timeout=0)
    )


def process_due_timed_messages(
    args: argparse.Namespace,
    env: dict[str, str],
    token: str,
    allowed_chat_id: str,
    state_path: Path,
    log_path: Path,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Relay due timed messages through the normal confirmed Codex path."""
    checked_ts = time.time() if now is None else now
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    if not isinstance(raw_tasks, list):
        return []

    tasks = [task for task in raw_tasks if isinstance(task, dict)]
    changed = len(tasks) != len(raw_tasks)
    events: list[dict[str, Any]] = []
    auth_state_path = Path(
        getattr(
            args,
            "codex_auth_state_path",
            Path(args.codex_usage_state_path).with_name(
                "telegram_codex_auth.state.json"
            ),
        )
    )
    relay_confirmation_path = (
        Path(args.relay_confirmation_state_path)
        if getattr(args, "relay_confirmation_state_path", None)
        else None
    )

    def send_timed_notice(text: str) -> None:
        try:
            _transport.send_reply(token, allowed_chat_id, text)
        except Exception as exc:
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "timed_message_notice_failed",
                    "error": _transport.short_error(exc, env),
                },
            )

    def send_visible_timed_message(
        task: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> bool:
        if task.get("visible_echo_sent_ts") is not None:
            return True
        reply_to_message_id = snapshot.get("message_id")
        try:
            reply_to_message_id = int(reply_to_message_id)
        except (TypeError, ValueError):
            reply_to_message_id = None
        try:
            result = _transport.send_reply(
                token,
                timed_message_task_chat_id(task) or allowed_chat_id,
                format_timed_message_fired(task),
                **(
                    {"message_thread_id": snapshot["message_thread_id"]}
                    if snapshot.get("message_thread_id") is not None
                    else {}
                ),
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:
            task.update(
                {
                    "next_attempt_ts": checked_ts
                    + _settings.TIMED_MESSAGE_RETRY_SECONDS,
                    "last_error": f"visible timed-message echo failed: {_transport.short_error(exc, env)}",
                }
            )
            event = {
                "ts": int(time.time()),
                "event": "timed_message_visible_echo_failed",
                "timed_message_id": task.get("id"),
                "message_id": snapshot.get("message_id"),
                "error": _transport.short_error(exc, env),
            }
            _state.append_jsonl(log_path, event)
            events.append(event)
            return False

        task["visible_echo_sent_ts"] = time.time()
        if isinstance(result, dict) and result.get("message_id") is not None:
            task["visible_echo_message_id"] = result["message_id"]
        event = {
            "ts": int(time.time()),
            "event": "timed_message_visible_echo_sent",
            "timed_message_id": task.get("id"),
            "message_id": snapshot.get("message_id"),
            "visible_echo_message_id": task.get("visible_echo_message_id"),
        }
        _state.append_jsonl(log_path, event)
        events.append(event)
        state.update({"version": 1, "updated_ts": time.time(), "tasks": tasks})
        write_timed_message_state(state_path, state)
        return True

    for task in tasks:
        status = str(task.get("status") or "pending")
        if status in {"delivered", "failed", "cancelled"}:
            continue

        if status in {"delivering", "submitted"} and timed_message_is_confirmed(task):
            snapshot = task.get("message")
            if not isinstance(snapshot, dict):
                task.update(
                    {
                        "status": "failed",
                        "failed_ts": checked_ts,
                        "last_error": "invalid persisted Telegram message snapshot",
                    }
                )
                changed = True
                continue
            if not send_visible_timed_message(task, snapshot):
                changed = True
                continue
            task.update({"status": "delivered", "confirmed_ts": checked_ts})
            events.append({"id": task.get("id"), "event": "confirmed"})
            changed = True
            continue

        if status == "submitted":
            continue
        if status == "delivering":
            try:
                attempt_started_ts = float(task.get("attempt_started_ts", checked_ts))
            except (TypeError, ValueError):
                attempt_started_ts = checked_ts
            if (
                checked_ts - attempt_started_ts
                < _settings.TIMED_MESSAGE_DELIVERY_GRACE_SECONDS
            ):
                continue
            task.update(
                {
                    "status": "failed",
                    "failed_ts": checked_ts,
                    "last_error": "delivery interrupted before confirmation; inspect before resending",
                }
            )
            changed = True
            continue

        try:
            due_ts = float(task.get("due_ts"))
            next_attempt_ts = float(task.get("next_attempt_ts", due_ts))
        except (TypeError, ValueError):
            task.update(
                {
                    "status": "failed",
                    "failed_ts": checked_ts,
                    "last_error": "invalid persisted timed-message timestamp",
                }
            )
            changed = True
            continue
        if checked_ts < due_ts or checked_ts < next_attempt_ts:
            continue

        active_checkpoint = _submission.codex_session_checkpoint(args.target_pane)
        if active_checkpoint and _submission.codex_session_turn_active(
            active_checkpoint[0]
        ):
            continue
        if relay_confirmation_path and _submission.current_pending_codex_submissions(
            relay_confirmation_path, args.target_pane
        ):
            continue

        auth_failure = _auth.active_codex_auth_failure(auth_state_path)
        if auth_failure:
            task["next_attempt_ts"] = checked_ts + _settings.TIMED_MESSAGE_RETRY_SECONDS
            if not task.get("auth_failure_notice_sent"):
                send_timed_notice(
                    "A timed message is due, but Codex needs sign-in. It remains queued; "
                    "run /reauth and it will retry automatically."
                )
                task["auth_failure_notice_sent"] = True
            changed = True
            continue

        relay_record: dict[str, Any] = {}
        target_error = _lifecycle.ensure_codex_target_for_agent_message(
            args, relay_record
        )
        if target_error is not None:
            task.update(
                {
                    "next_attempt_ts": checked_ts
                    + _settings.TIMED_MESSAGE_RETRY_SECONDS,
                    "last_error": target_error,
                }
            )
            if not task.get("failure_notice_sent"):
                send_timed_notice(
                    "A timed message is due but could not reach Codex yet. It remains queued and will retry automatically.",
                )
                task["failure_notice_sent"] = True
            changed = True
            continue

        snapshot = task.get("message")
        if not isinstance(snapshot, dict):
            task.update(
                {
                    "status": "failed",
                    "failed_ts": checked_ts,
                    "last_error": "invalid persisted Telegram message snapshot",
                }
            )
            changed = True
            continue
        if not send_visible_timed_message(task, snapshot):
            changed = True
            continue
        message_text = str(snapshot.get("text") or "").strip()
        timed_route_id = re.sub(
            r"[^A-Za-z0-9_.:-]",
            "_",
            f"timed_{task.get('id') or ''}",
        )
        relay_text = _messages.format_agent_message(
            snapshot,
            message_text,
            route_id=timed_route_id,
        )
        marker_match = _settings.TELEGRAM_USER_MESSAGE_MARKER_RE.search(relay_text)
        if marker_match is None:
            task.update(
                {
                    "status": "failed",
                    "failed_ts": checked_ts,
                    "last_error": "timed relay marker could not be constructed",
                }
            )
            changed = True
            continue
        marker = marker_match.group(0)
        checkpoint = _submission.codex_session_checkpoint(args.target_pane)
        task.update(
            {
                "status": "delivering",
                "attempt_started_ts": checked_ts,
                "attempts": int(task.get("attempts", 0)) + 1,
                "marker": marker,
                "session_path": str(checkpoint[0]) if checkpoint is not None else None,
                "session_offset": checkpoint[1] if checkpoint is not None else 0,
                "target_pane": args.target_pane,
            }
        )
        state.update({"version": 1, "updated_ts": checked_ts, "tasks": tasks})
        write_timed_message_state(state_path, state)

        route_state_text = getattr(args, "reply_route_state_path", None)
        if route_state_text:
            snapshot_chat = snapshot.get("chat") or {}
            _routing.set_reply_route_chat_id(
                Path(route_state_text),
                timed_message_task_chat_id(task) or allowed_chat_id,
                is_group=str(snapshot_chat.get("type") or "")
                in _settings.GROUP_CHAT_TYPES,
                route_id=timed_route_id,
                message_thread_id=snapshot.get("message_thread_id"),
                source_message_id=snapshot.get("message_id"),
            )

        try:
            relay_result = _submission.paste_to_tmux(
                args.target_pane,
                relay_text,
                press_enter=True,
                allow_shell_pane=args.allow_shell_pane,
                submit_delay=args.submit_delay,
                pending_state_path=relay_confirmation_path,
            )
        except Exception as exc:
            relay_result = f"not relayed: {_transport.short_error(exc, env)}"

        task["relay_result"] = relay_result
        if relay_result.startswith("relayed to "):
            if task.get("session_path") is None:
                post_submit_checkpoint = _submission.codex_session_checkpoint(
                    args.target_pane
                )
                if post_submit_checkpoint is not None:
                    task.update(
                        {
                            "session_path": str(post_submit_checkpoint[0]),
                            "session_offset": 0,
                        }
                    )
            if "submission confirmed" in relay_result or timed_message_is_confirmed(
                task
            ):
                task.update({"status": "delivered", "confirmed_ts": time.time()})
                event_name = "delivered"
            else:
                task.update({"status": "submitted", "submitted_ts": time.time()})
                event_name = "submitted_pending_confirmation"
            event = {
                "ts": int(time.time()),
                "event": f"timed_message_{event_name}",
                "timed_message_id": task.get("id"),
                "message_id": snapshot.get("message_id"),
                "due_ts": due_ts,
                "attempts": task.get("attempts"),
                "relay_result": relay_result,
            }
            _state.append_jsonl(log_path, event)
            events.append(event)
        else:
            task.update(
                {
                    "status": "pending",
                    "next_attempt_ts": checked_ts
                    + _settings.TIMED_MESSAGE_RETRY_SECONDS,
                    "last_error": relay_result,
                }
            )
            if not task.get("failure_notice_sent"):
                send_timed_notice(
                    "A timed message is due but could not reach Codex yet. It remains queued and will retry automatically.",
                )
                task["failure_notice_sent"] = True
            event = {
                "ts": int(time.time()),
                "event": "timed_message_retry_scheduled",
                "timed_message_id": task.get("id"),
                "message_id": snapshot.get("message_id"),
                "due_ts": due_ts,
                "attempts": task.get("attempts"),
                "relay_result": relay_result,
            }
            _state.append_jsonl(log_path, event)
            events.append(event)
        changed = True

    if changed:
        state.update({"version": 1, "updated_ts": time.time(), "tasks": tasks})
        write_timed_message_state(state_path, state)
    return events
