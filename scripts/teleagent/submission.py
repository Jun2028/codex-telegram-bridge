"""Submission services for the Telegram relay."""

from __future__ import annotations


import notify as _notify
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402

from . import settings as _settings
from . import processes as _processes
from . import sessions as _sessions
from . import state as _state


def codex_session_checkpoint(target_pane: str) -> tuple[Path, int] | None:
    """Return the active Codex rollout and its current byte boundary."""
    meta = agent_registry.active_agent_for_pane(target_pane)
    if not meta:
        return None
    # The process-open rollout is authoritative. A recently written rollout
    # can belong to the previous supervised Codex process and must not become
    # the checkpoint for input sent to its replacement.
    meta = agent_registry.refresh_codex_session_link(meta, target_pane=target_pane)
    session_text = str(meta.get("codex_session_path") or "")
    session_path = Path(session_text) if session_text else None
    if session_path is not None and _sessions.valid_codex_session_for_agent(
        meta, session_path
    ):
        try:
            return session_path, session_path.stat().st_size
        except OSError:
            pass

    # Fall back to a recent Telegram-bearing root rollout only while process
    # FD discovery is temporarily unavailable.
    marker_path, _marker_method = agent_registry.codex_session_with_latest_user_message(
        target_pane, codex_home=meta.get("codex_home")
    )
    if marker_path is None or not _sessions.valid_codex_session_for_agent(
        meta, marker_path
    ):
        return None
    try:
        return marker_path, marker_path.stat().st_size
    except OSError:
        return None


def wait_for_codex_submission(
    checkpoint: tuple[Path, int],
    marker: str,
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> bool:
    """Confirm that Codex appended this Telegram message after the checkpoint."""
    session_path, offset = checkpoint
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            size = session_path.stat().st_size
            start = offset if size >= offset else 0
            with session_path.open("rb") as handle:
                handle.seek(start)
                for raw_line in handle:
                    if not raw_line.endswith(b"\n"):
                        break
                    try:
                        record = json.loads(raw_line)
                    except (ValueError, UnicodeError):
                        continue
                    payload = record.get("payload") or {}
                    text = ""
                    if (
                        record.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"
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
                    if text.startswith(marker):
                        return True
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(0.01, poll_interval), remaining))


def codex_session_bootstrap_allowed(target_pane: str) -> bool:
    """Return whether this pane is a registered new process awaiting its first rollout."""
    meta = agent_registry.active_agent_for_pane(target_pane)
    return bool(
        meta
        and meta.get("agent_id")
        and agent_registry.launch_requires_fresh_session(meta)
        and not meta.get("codex_session_path")
    )


def wait_for_codex_bootstrap_submission(
    target_pane: str,
    marker: str,
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> tuple[bool, tuple[Path, int] | None]:
    """Discover the lazily created first rollout and confirm the submitted marker."""
    deadline = time.monotonic() + max(0.0, timeout)
    discovered: tuple[Path, int] | None = None
    while True:
        checkpoint = codex_session_checkpoint(target_pane)
        if checkpoint is not None:
            # The first user event may already have been appended by the time
            # the rollout becomes discoverable, so scan it from byte zero.
            discovered = (checkpoint[0], 0)
            if wait_for_codex_submission(discovered, marker, timeout=0):
                return True, discovered
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, discovered
        time.sleep(min(max(0.01, poll_interval), remaining))


def codex_session_turn_active(session_path: Path, chunk_bytes: int = 64 * 1024) -> bool:
    """Return whether the rollout has an active or not-yet-started user turn."""
    user_message_after_task_event = False

    def classify(record: dict[str, Any]) -> bool | None:
        nonlocal user_message_after_task_event
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if record.get("type") == "event_msg":
            event_type = str(payload.get("type") or "")
            if event_type == "task_started":
                return True
            if event_type in {"task_complete", "turn_aborted"}:
                return user_message_after_task_event
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            user_message_after_task_event = True
        return None

    try:
        with session_path.open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            leading_fragment = b""
            while position > 0:
                read_size = min(max(1, chunk_bytes), position)
                position -= read_size
                handle.seek(position)
                data = handle.read(read_size) + leading_fragment
                lines = data.split(b"\n")
                leading_fragment = lines[0]
                for raw_line in reversed(lines[1:]):
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    result = classify(record)
                    if result is not None:
                        return result
            if leading_fragment:
                try:
                    record = json.loads(leading_fragment)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return False
                result = classify(record)
                if result is not None:
                    return result
    except OSError:
        return False
    return user_message_after_task_event


def wait_for_codex_turn_terminal(
    checkpoint: tuple[Path, int],
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> str | None:
    """Wait for an interrupted task to abort or finish in a completion race."""
    session_path, offset = checkpoint
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            size = session_path.stat().st_size
            start = offset if size >= offset else 0
            with session_path.open("rb") as handle:
                handle.seek(start)
                lines = handle.read().splitlines()
            for raw_line in lines:
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = record.get("payload")
                if record.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                event_type = str(payload.get("type") or "")
                if event_type in {"turn_aborted", "task_complete"}:
                    return event_type
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(max(0.01, poll_interval), remaining))


def register_pending_codex_submission(
    state_path: Path,
    checkpoint: tuple[Path, int] | None,
    marker: str,
    target_pane: str,
    *,
    now: float | None = None,
    relay_text: str | None = None,
    process_identity: str | None = None,
) -> dict[str, Any]:
    """Persist an accepted tmux submit whose Codex JSONL event is still pending."""
    session_path, offset = checkpoint if checkpoint is not None else (None, 0)
    created_ts = time.time() if now is None else now
    marker_match = _settings.TELEGRAM_USER_MESSAGE_MARKER_RE.search(marker)
    message_id = int(marker_match.group(1)) if marker_match is not None else None
    meta = agent_registry.active_agent_for_pane(target_pane)
    pending_record: dict[str, Any] = {
        "agent_id": meta.get("agent_id") if meta else None,
        "created_ts": created_ts,
        "marker": marker,
        "message_id": message_id,
        "offset": offset,
        "session_path": str(session_path) if session_path is not None else None,
        "target_pane": target_pane,
    }
    if isinstance(relay_text, str) and relay_text:
        pending_record["relay_text"] = relay_text
    if isinstance(process_identity, str) and process_identity:
        pending_record["codex_process_identity"] = process_identity
    state = _state.read_json_object(state_path)
    pending = state.get("pending")
    if not isinstance(pending, list):
        pending = []
    # Telegram update offsets normally make this unique. Preserve the earliest
    # checkpoint if an operator explicitly replays an item before confirmation.
    if not any(
        isinstance(item, dict)
        and item.get("marker") == marker
        and item.get("agent_id") == pending_record["agent_id"]
        for item in pending
    ):
        pending.append(pending_record)
    state.update({"pending": pending, "updated_ts": created_ts, "version": 1})
    _state.write_json_object(state_path, state)
    return pending_record


def pending_codex_submission_pane_location(item: dict[str, Any]) -> str:
    """Locate a pending marker in the active Codex pane."""
    marker = item.get("marker")
    target_pane = item.get("target_pane")
    if not isinstance(marker, str) or not marker:
        return "absent"
    if not isinstance(target_pane, str) or not target_pane:
        return "absent"

    meta = agent_registry.active_agent_for_pane(target_pane)
    expected_agent_id = item.get("agent_id")
    if not meta or (
        expected_agent_id is not None and meta.get("agent_id") != expected_agent_id
    ):
        return "absent"

    try:
        if not _processes.tmux_target_exists(
            target_pane
        ) or not _processes.tmux_pane_has_codex_process(target_pane):
            return "absent"
        pane_text = _notify.run_short(
            # A long Telegram reply can push the beginning of the active
            # multiline composer above the visible pane.  Search scrollback as
            # well; otherwise its marker looks absent and recovery never sends
            # the verified Enter retry.
            ["tmux", "capture-pane", "-p", "-J", "-S", "-", "-t", target_pane],
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return "absent"

    composer_prompts = [
        line for line in pane_text.splitlines() if line.lstrip().startswith("›")
    ]
    if composer_prompts and marker in composer_prompts[-1]:
        return "composer"
    if any(marker in line for line in composer_prompts):
        return "history"
    return "absent"


def pending_codex_submission_in_composer(item: dict[str, Any]) -> bool:
    """Return whether this pending marker is still in the active Codex composer."""
    return pending_codex_submission_pane_location(item) == "composer"


def current_pending_codex_submissions(
    state_path: Path,
    target_pane: str,
) -> list[dict[str, Any]]:
    """Return unresolved relays that can still collide with composer input."""
    state = _state.read_json_object(state_path)
    raw_pending = state.get("pending")
    if not isinstance(raw_pending, list):
        return []
    meta = agent_registry.active_agent_for_pane(target_pane)
    current_agent_id = meta.get("agent_id") if meta else None
    current_process_identity: str | None = None
    process_identity_checked = False
    blockers: list[dict[str, Any]] = []
    for raw_item in raw_pending:
        if not isinstance(raw_item, dict) or raw_item.get("target_pane") != target_pane:
            continue
        item = dict(raw_item)
        if not (
            item.get("agent_id") is None
            or current_agent_id is None
            or item.get("agent_id") == current_agent_id
        ):
            continue

        if item.get("pane_submission_observed_ts"):
            if not process_identity_checked:
                current_process_identity = _processes.codex_process_identity(
                    target_pane
                )
                process_identity_checked = True
            observed_process_identity = item.get(
                "replacement_codex_process_identity"
            ) or item.get("codex_process_identity")
            if (
                isinstance(observed_process_identity, str)
                and observed_process_identity == current_process_identity
            ):
                # The TUI already moved this complete payload out of the
                # composer. Its JSONL user event can legitimately wait behind
                # the active turn, so it cannot merge with newer input.
                continue
        blockers.append(item)
    return blockers


def cancel_pending_codex_submissions(
    state_path: Path | None,
    target_pane: str,
) -> list[dict[str, Any]]:
    """Remove unconfirmed composer submissions superseded by /interrupt."""
    if state_path is None:
        return []
    state = _state.read_json_object(state_path)
    raw_pending = state.get("pending")
    if not isinstance(raw_pending, list):
        return []
    cancelled = [
        dict(item)
        for item in raw_pending
        if isinstance(item, dict) and item.get("target_pane") == target_pane
    ]
    remaining = [
        item
        for item in raw_pending
        if not (isinstance(item, dict) and item.get("target_pane") == target_pane)
    ]
    if cancelled:
        state.update({"pending": remaining, "updated_ts": time.time(), "version": 1})
        _state.write_json_object(state_path, state)
    return cancelled


def tmux_paste_text_atomic(target_pane: str, text: str) -> None:
    """Paste one payload through a private bracketed tmux buffer."""
    buffer_name = f"tele-agent-telegram-{os.getpid()}-{time.time_ns()}"
    subprocess.run(
        ["tmux", "load-buffer", "-b", buffer_name, "-"],
        input=text,
        text=True,
        check=True,
        timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
    )
    try:
        subprocess.run(
            [
                "tmux",
                "paste-buffer",
                "-p",
                "-d",
                "-b",
                buffer_name,
                "-t",
                target_pane,
            ],
            check=True,
            timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
        )
    except BaseException:
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buffer_name],
            check=False,
            timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
        )
        raise


def reconcile_pending_codex_submissions(
    state_path: Path,
    *,
    log_path: Path | None = None,
    now: float | None = None,
    recovery_grace_seconds: float = _settings.RELAY_CONFIRMATION_RECOVERY_GRACE_SECONDS,
    recovery_interval_seconds: float = _settings.RELAY_CONFIRMATION_RECOVERY_INTERVAL_SECONDS,
    recovery_confirmation_timeout: float = _settings.RELAY_CONFIRMATION_RECOVERY_TIMEOUT_SECONDS,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve pending relays and retry only an intact, idle composer."""
    checked_ts = time.time() if now is None else now
    state = _state.read_json_object(state_path)
    raw_pending = state.get("pending")
    if not isinstance(raw_pending, list):
        return {"confirmed": [], "pending": [], "retried": [], "stalled": []}

    confirmed: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    retried: list[dict[str, Any]] = []
    stalled: list[dict[str, Any]] = []
    state_changed = False
    for raw_item in raw_pending:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        marker = item.get("marker")
        target_pane = item.get("target_pane")
        session_text = item.get("session_path")
        active_meta = (
            agent_registry.active_agent_for_pane(target_pane)
            if isinstance(target_pane, str) and target_pane
            else None
        )
        if not session_text:
            # Never attach a pending submit to a replacement agent. A new
            # rollout is eligible only while the same registered launch owns
            # the pane.
            if active_meta and active_meta.get("agent_id") == item.get("agent_id"):
                active_meta = agent_registry.refresh_codex_session_link(
                    active_meta, target_pane=target_pane
                )
                session_text = active_meta.get("codex_session_path")
                if session_text:
                    item = {**item, "session_path": session_text, "offset": 0}
                    state_changed = True
        try:
            offset = int(item.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if not isinstance(marker, str) or not marker:
            continue

        checkpoint = (
            (Path(session_text), max(0, offset))
            if isinstance(session_text, str) and session_text
            else None
        )
        confirmed_checkpoint = (
            checkpoint
            if checkpoint is not None
            and wait_for_codex_submission(checkpoint, marker, timeout=0)
            else None
        )

        # A supervised Codex replacement can create a new rollout between the
        # pre-submit checkpoint and the user event. Search the rollout that is
        # actually open by the same registered agent before treating the old
        # checkpoint as unresolved. If the marker is not there yet, anchor
        # future checks at its current end after this one full scan.
        same_agent = bool(
            active_meta
            and item.get("agent_id") is not None
            and active_meta.get("agent_id") == item.get("agent_id")
        )
        if confirmed_checkpoint is None and same_agent:
            active_meta = agent_registry.refresh_codex_session_link(
                active_meta, target_pane=target_pane
            )
            active_session_text = str(active_meta.get("codex_session_path") or "")
            if active_session_text and active_session_text != str(session_text or ""):
                active_path = Path(active_session_text)
                active_checkpoint = (active_path, 0)
                if wait_for_codex_submission(active_checkpoint, marker, timeout=0):
                    confirmed_checkpoint = active_checkpoint
                else:
                    try:
                        active_offset = active_path.stat().st_size
                    except OSError:
                        active_offset = 0
                    item.update(
                        {
                            "session_path": active_session_text,
                            "offset": active_offset,
                        }
                    )
                    session_text = active_session_text
                    checkpoint = (active_path, active_offset)
                    state_changed = True

        if confirmed_checkpoint is not None:
            session_text = str(confirmed_checkpoint[0])
            item.update(
                {
                    "session_path": session_text,
                    "offset": confirmed_checkpoint[1],
                }
            )
            resolved = dict(item)
            resolved["confirmed_ts"] = checked_ts
            try:
                resolved["latency_seconds"] = max(
                    0.0, checked_ts - float(item.get("created_ts", checked_ts))
                )
            except (TypeError, ValueError):
                resolved["latency_seconds"] = None
            confirmed.append(resolved)
            if log_path is not None:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(checked_ts),
                        "event": "telegram_relay_submission_confirmed",
                        "message_id": resolved.get("message_id"),
                        "session_path": session_text,
                        "latency_seconds": resolved.get("latency_seconds"),
                    },
                )
            state_changed = True
            continue

        try:
            created_ts = float(item.get("created_ts", checked_ts))
        except (TypeError, ValueError):
            created_ts = checked_ts
        try:
            last_recovery_ts = float(item.get("last_recovery_attempt_ts", 0.0))
        except (TypeError, ValueError):
            last_recovery_ts = 0.0
        try:
            recovery_attempts = max(0, int(item.get("recovery_attempts", 0)))
        except (TypeError, ValueError):
            recovery_attempts = 0
        if item.get("stalled_reason") in {
            "enter_retry_unconfirmed",
            "recovery_attempt_limit_reached",
        }:
            # Older listeners treated slow Codex JSONL persistence as a hard
            # failure. Reopen those records so the listener can confirm them
            # or retry the still-intact composer after the active turn ends.
            item.pop("stalled_ts", None)
            item.pop("stalled_reason", None)
            state_changed = True
        turn_active = bool(
            checkpoint is not None and codex_session_turn_active(checkpoint[0])
        )
        recovery_due = (
            not turn_active
            and checked_ts - created_ts >= max(0.0, recovery_grace_seconds)
            and checked_ts - last_recovery_ts >= max(0.0, recovery_interval_seconds)
        )

        pane_location = (
            pending_codex_submission_pane_location(item)
            if not item.get("stalled_ts") and recovery_due
            else "absent"
        )
        relay_text = item.get("relay_text")
        submitted_process_identity = item.get("codex_process_identity")
        try:
            replacement_replay_attempts = max(
                0, int(item.get("replacement_replay_attempts", 0))
            )
        except (TypeError, ValueError):
            replacement_replay_attempts = 0
        current_process_identity = (
            _processes.codex_process_identity(str(target_pane))
            if (
                pane_location == "absent"
                and isinstance(target_pane, str)
                and target_pane
                and isinstance(submitted_process_identity, str)
                and submitted_process_identity
            )
            else None
        )
        replacement_process = bool(
            current_process_identity
            and current_process_identity != submitted_process_identity
        )
        if (
            recovery_due
            and not item.get("stalled_ts")
            and active_meta
            and active_meta.get("agent_id") == item.get("agent_id")
            and replacement_process
            and replacement_replay_attempts == 0
            and isinstance(relay_text, str)
            and relay_text
        ):
            replacement_replay_attempts += 1
            item.pop("pane_submission_observed_ts", None)
            item.update(
                {
                    "last_recovery_attempt_ts": checked_ts,
                    "replacement_replay_attempts": replacement_replay_attempts,
                    "replacement_codex_process_identity": current_process_identity,
                }
            )
            retried.append(dict(item))
            state_changed = True
            try:
                _processes.tmux_send_keys(str(target_pane), "C-u")
                tmux_paste_text_atomic(str(target_pane), relay_text)
                time.sleep(0.2)
                _processes.tmux_send_keys(str(target_pane), "Enter")
            except (OSError, subprocess.SubprocessError) as exc:
                item.update(
                    {
                        "last_recovery_error": str(exc)[:500],
                        "stalled_ts": checked_ts,
                        "stalled_reason": "replacement_process_replay_failed",
                    }
                )
                stalled.append(dict(item))
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(checked_ts),
                            "event": "telegram_relay_replay_after_restart_failed",
                            "message_id": item.get("message_id"),
                            "target_pane": target_pane,
                            "error": item["last_recovery_error"],
                        },
                    )
                still_pending.append(item)
                continue

            if log_path is not None:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(checked_ts),
                        "event": "telegram_relay_replayed_after_codex_restart",
                        "message_id": item.get("message_id"),
                        "target_pane": target_pane,
                    },
                )
            recovered, discovered = wait_for_codex_bootstrap_submission(
                str(target_pane),
                marker,
                timeout=max(0.0, recovery_confirmation_timeout),
            )
            if discovered is not None:
                checkpoint = discovered
                item.update(
                    {
                        "session_path": str(discovered[0]),
                        "offset": discovered[1],
                    }
                )
            if recovered:
                resolved = dict(item)
                resolved["confirmed_ts"] = checked_ts
                resolved["recovered"] = True
                resolved["recovery_method"] = "replacement_process_replay"
                try:
                    resolved["latency_seconds"] = max(
                        0.0,
                        checked_ts - float(item.get("created_ts", checked_ts)),
                    )
                except (TypeError, ValueError):
                    resolved["latency_seconds"] = None
                confirmed.append(resolved)
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(checked_ts),
                            "event": "telegram_relay_submission_confirmed",
                            "message_id": resolved.get("message_id"),
                            "session_path": (
                                str(checkpoint[0]) if checkpoint is not None else None
                            ),
                            "latency_seconds": resolved.get("latency_seconds"),
                            "recovered": True,
                            "recovery_method": "replacement_process_replay",
                        },
                    )
                continue

            # The exact payload has now been replayed once.  Leave it pending:
            # a later pass can confirm delayed JSONL persistence or retry only
            # Enter if the marker is visibly still in the composer.
            still_pending.append(item)
            continue

        marker_in_composer = pane_location == "composer"
        if marker_in_composer:
            recovery_attempts += 1
            item.update(
                {
                    "last_recovery_attempt_ts": checked_ts,
                    "recovery_attempts": recovery_attempts,
                }
            )
            retried.append(dict(item))
            state_changed = True
            try:
                _processes.tmux_send_keys(str(target_pane), "Enter")
            except (OSError, subprocess.SubprocessError) as exc:
                item.update(
                    {
                        "last_recovery_error": str(exc)[:500],
                        "stalled_ts": checked_ts,
                        "stalled_reason": "enter_retry_failed",
                    }
                )
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(checked_ts),
                            "event": "telegram_relay_submission_retry_failed",
                            "message_id": item.get("message_id"),
                            "target_pane": target_pane,
                            "attempt": recovery_attempts,
                            "error": item["last_recovery_error"],
                        },
                    )
                stalled.append(dict(item))
            else:
                item.pop("last_recovery_error", None)
                if log_path is not None:
                    _state.append_jsonl(
                        log_path,
                        {
                            "ts": int(checked_ts),
                            "event": "telegram_relay_submission_retried",
                            "message_id": item.get("message_id"),
                            "target_pane": target_pane,
                            "attempt": recovery_attempts,
                        },
                    )

                recovered = False
                if checkpoint is not None:
                    recovered = wait_for_codex_submission(
                        checkpoint,
                        marker,
                        timeout=max(0.0, recovery_confirmation_timeout),
                    )
                elif isinstance(target_pane, str) and target_pane:
                    recovered, discovered = wait_for_codex_bootstrap_submission(
                        target_pane,
                        marker,
                        timeout=max(0.0, recovery_confirmation_timeout),
                    )
                    if discovered is not None:
                        checkpoint = discovered
                        item.update(
                            {
                                "session_path": str(discovered[0]),
                                "offset": discovered[1],
                            }
                        )

                if recovered:
                    resolved = dict(item)
                    resolved["confirmed_ts"] = checked_ts
                    resolved["recovered"] = True
                    try:
                        resolved["latency_seconds"] = max(
                            0.0,
                            checked_ts - float(item.get("created_ts", checked_ts)),
                        )
                    except (TypeError, ValueError):
                        resolved["latency_seconds"] = None
                    confirmed.append(resolved)
                    if log_path is not None:
                        _state.append_jsonl(
                            log_path,
                            {
                                "ts": int(checked_ts),
                                "event": "telegram_relay_submission_confirmed",
                                "message_id": resolved.get("message_id"),
                                "session_path": (
                                    str(checkpoint[0])
                                    if checkpoint is not None
                                    else None
                                ),
                                "latency_seconds": resolved.get("latency_seconds"),
                                "recovered": True,
                            },
                        )
                    continue
                # A submitted prompt can move into pane history while its
                # user event waits for JSONL persistence. Recheck the pane,
                # but never turn persistence latency into a hard failure.
                retry_location = pending_codex_submission_pane_location(item)
                if retry_location == "history":
                    item["pane_submission_observed_ts"] = checked_ts
                    if log_path is not None:
                        _state.append_jsonl(
                            log_path,
                            {
                                "ts": int(checked_ts),
                                "event": "telegram_relay_submission_observed_in_history",
                                "message_id": item.get("message_id"),
                                "target_pane": target_pane,
                            },
                        )

            if item.get("stalled_ts") and log_path is not None:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(checked_ts),
                        "event": "telegram_relay_submission_stalled",
                        "message_id": item.get("message_id"),
                        "target_pane": target_pane,
                        "attempt": recovery_attempts,
                        "reason": item.get("stalled_reason"),
                    },
                )
        elif (
            pane_location == "history"
            and not item.get("stalled_ts")
            and not item.get("pane_submission_observed_ts")
        ):
            # Codex displays queued steering input in pane history before it
            # appends the corresponding user event to JSONL. Keep following
            # the checkpoint, but do not report a healthy queued prompt as
            # stuck or block later input from the same process.
            item["pane_submission_observed_ts"] = checked_ts
            state_changed = True
            if log_path is not None:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(checked_ts),
                        "event": "telegram_relay_submission_observed_in_history",
                        "message_id": item.get("message_id"),
                        "target_pane": target_pane,
                    },
                )

        still_pending.append(item)

    if state_changed or confirmed or len(still_pending) != len(raw_pending):
        state.update({"pending": still_pending, "updated_ts": checked_ts, "version": 1})
        _state.write_json_object(state_path, state)
    return {
        "confirmed": confirmed,
        "pending": still_pending,
        "retried": retried,
        "stalled": stalled,
    }


def paste_to_tmux(
    target_pane: str,
    text: str,
    press_enter: bool,
    allow_shell_pane: bool,
    submit_delay: float,
    confirmation_timeout: float = 2.0,
    pending_state_path: Path | None = None,
) -> str:
    codex_running = _processes.registered_codex_process_running(target_pane)
    if codex_running:
        current_command = "codex"
    else:
        if not _processes.tmux_target_exists(target_pane):
            return f"not relayed: tmux target not found: {target_pane}"
        current_command = _processes.tmux_pane_command(target_pane)
        codex_running = _processes.tmux_pane_has_codex_process(target_pane)
    if (
        press_enter
        and current_command in _settings.SHELL_COMMANDS
        and not allow_shell_pane
        and not codex_running
    ):
        return (
            f"not relayed: target {target_pane} is a shell pane ({current_command}). "
            "Start Codex/agent in that pane first, then send the Telegram message again."
        )

    marker_match = (
        _settings.TELEGRAM_USER_MESSAGE_MARKER_RE.search(text) if press_enter else None
    )
    marker = marker_match.group(0) if marker_match is not None else None
    checkpoint = codex_session_checkpoint(target_pane) if marker is not None else None
    bootstrap_session = bool(
        marker is not None
        and checkpoint is None
        and codex_running
        and codex_session_bootstrap_allowed(target_pane)
    )
    if marker is not None and checkpoint is None and not bootstrap_session:
        return "not relayed: the active Codex session could not be verified"

    if marker is not None and pending_state_path is not None:
        # Never clear or append to an older Telegram payload that is still in
        # the composer.  First give that exact, verified marker its recovery
        # Enter; if it still cannot be confirmed, leave the composer untouched
        # and fail closed for the newer message.
        reconcile_pending_codex_submissions(
            pending_state_path,
            recovery_grace_seconds=0,
        )
        blockers = current_pending_codex_submissions(
            pending_state_path,
            target_pane,
        )
        if blockers:
            blocked_ids = ", ".join(
                str(item.get("message_id"))
                for item in blockers
                if item.get("message_id") is not None
            )
            suffix = f" {blocked_ids}" if blocked_ids else ""
            return (
                f"not relayed: previous Telegram message{suffix} is still pending "
                "confirmation; the Codex composer was left unchanged"
            )

    submitting_process_identity = (
        _processes.codex_process_identity(target_pane) if marker is not None else None
    )

    # Clear any stale text in the Codex input line before injecting the Telegram
    # message. Without this, an unsent prompt can be concatenated with the relay.
    subprocess.run(
        ["tmux", "send-keys", "-t", target_pane, "C-u"],
        check=True,
        timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
    )
    tmux_paste_text_atomic(target_pane, text)
    if press_enter:
        # Codex TUI treats very fast text+Enter injection as a paste/edit event.
        # A short pause makes the following Enter behave like a user submit.
        if submit_delay > 0:
            time.sleep(submit_delay)
        subprocess.run(
            ["tmux", "send-keys", "-t", target_pane, "Enter"],
            check=True,
            timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
        )
        confirmed = True
        pending_checkpoint = checkpoint
        if bootstrap_session and marker is not None:
            confirmed, pending_checkpoint = wait_for_codex_bootstrap_submission(
                target_pane,
                marker,
                timeout=max(0.0, confirmation_timeout),
            )
        elif checkpoint is not None and marker is not None:
            confirmed = wait_for_codex_submission(
                checkpoint,
                marker,
                timeout=max(0.0, confirmation_timeout),
            )
        if marker is not None and not confirmed and confirmation_timeout > 0:
            # Enter can land during the brief transition after a Codex turn
            # finishes and leave the complete message in the composer.  Retry
            # the submit key once, without clearing or repasting any text.  If
            # the first Enter actually submitted and JSONL persistence is only
            # delayed, Enter on the empty/busy composer is a no-op; the marker
            # check still prevents a duplicate relay payload.
            subprocess.run(
                ["tmux", "send-keys", "-t", target_pane, "Enter"],
                check=True,
                timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
            )
            if bootstrap_session:
                confirmed, pending_checkpoint = wait_for_codex_bootstrap_submission(
                    target_pane,
                    marker,
                    timeout=max(0.0, confirmation_timeout),
                )
            elif checkpoint is not None:
                confirmed = wait_for_codex_submission(
                    checkpoint,
                    marker,
                    timeout=max(0.0, confirmation_timeout),
                )
        if marker is not None and not confirmed:
            if pending_state_path is not None:
                register_pending_codex_submission(
                    pending_state_path,
                    pending_checkpoint,
                    marker,
                    target_pane,
                    relay_text=text,
                    process_identity=submitting_process_identity,
                )
            effective_command = (
                "codex" if codex_running else (current_command or "unknown")
            )
            return (
                f"relayed to {target_pane} (pane command: {effective_command}; "
                "submission pending confirmation)"
            )
    effective_command = "codex" if codex_running else (current_command or "unknown")
    if checkpoint is not None or bootstrap_session:
        return (
            f"relayed to {target_pane} (pane command: {effective_command}; "
            "submission confirmed)"
        )
    return f"relayed to {target_pane} (pane command: {effective_command})"


def interrupt_codex_with_prompt(
    target_pane: str,
    relay_text: str,
    *,
    submit_delay: float,
    pending_state_path: Path | None,
    timeout: float = _settings.INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """Abort the active managed turn, then submit one replacement prompt."""
    if not _processes.codex_target_ready(target_pane):
        raise RuntimeError(f"Codex is not running in {target_pane}")
    checkpoint = codex_session_checkpoint(target_pane)
    if checkpoint is None:
        raise RuntimeError("the active Codex session could not be verified")

    was_active = codex_session_turn_active(checkpoint[0])
    terminal_event: str | None = None
    if was_active:
        _processes.tmux_send_keys(target_pane, "Escape")
        terminal_event = wait_for_codex_turn_terminal(
            checkpoint,
            timeout=max(0.0, timeout),
        )
        if terminal_event is None:
            raise RuntimeError("Codex did not confirm the interrupt before timeout")

    cancelled = cancel_pending_codex_submissions(pending_state_path, target_pane)
    result = paste_to_tmux(
        target_pane,
        relay_text,
        press_enter=True,
        allow_shell_pane=False,
        submit_delay=submit_delay,
        pending_state_path=pending_state_path,
    )
    return result, {
        "cancelled_pending_message_ids": [
            item.get("message_id")
            for item in cancelled
            if item.get("message_id") is not None
        ],
        "terminal_event": terminal_event,
        "was_active": was_active,
    }
