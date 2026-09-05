"""Usage services for the Telegram relay."""

from __future__ import annotations


import json
import time
from pathlib import Path
from typing import Any

from . import settings as _settings
from . import sessions as _sessions
from . import state as _state
from . import transport as _transport


def _usage_error_code(value: Any) -> str | None:
    """Return an exact structured Codex usage-limit code, if present."""
    if isinstance(value, str):
        return value if value in _settings.USAGE_LIMIT_ERROR_CODES else None
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _settings.USAGE_LIMIT_ERROR_CODES:
                return str(key)
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in {"error_info", "codex_error_info", "codexerrorinfo"}:
                code = _usage_error_code(item)
                if code:
                    return code
            if isinstance(item, (dict, list)):
                code = _usage_error_code(item)
                if code:
                    return code
    elif isinstance(value, list):
        for item in value:
            code = _usage_error_code(item)
            if code:
                return code
    return None


def _rate_limit_summary(rate_limits: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "limit_id": rate_limits.get("limit_id") or rate_limits.get("limitId"),
        "reached_type": rate_limits.get("rate_limit_reached_type")
        or rate_limits.get("rateLimitReachedType"),
    }
    windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        window = rate_limits.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent", window.get("usedPercent"))
        resets_at = window.get("resets_at", window.get("resetsAt"))
        window_minutes = window.get("window_minutes", window.get("windowDurationMins"))
        try:
            used_value = float(used)
        except (TypeError, ValueError):
            continue
        try:
            reset_value = int(resets_at) if resets_at is not None else None
        except (TypeError, ValueError):
            reset_value = None
        try:
            window_minutes_value = (
                int(window_minutes) if window_minutes is not None else None
            )
        except (TypeError, ValueError):
            window_minutes_value = None
        windows.append(
            {
                "name": name,
                "used_percent": used_value,
                "resets_at": reset_value,
                "window_minutes": window_minutes_value,
            }
        )
    summary["windows"] = windows
    return summary


def _usage_limit_observation(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("type") != "event_msg" or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    payload_type = str(payload.get("type") or "")
    if payload_type == "token_count" and isinstance(payload.get("rate_limits"), dict):
        summary = _rate_limit_summary(payload["rate_limits"])
        reached_type = summary.get("reached_type")
        if reached_type in _settings.USAGE_LIMIT_REACHED_TYPES:
            saturated_resets = [
                window.get("resets_at")
                for window in summary["windows"]
                if window.get("used_percent", 0) >= 100
                and window.get("resets_at") is not None
            ]
            # Workspace credit/spend-control exhaustion does not necessarily
            # share the ordinary Codex window reset. Only report a reset time
            # when the generic rate limit is explicit and a saturated window
            # supplies that timestamp.
            resets_at = (
                max(saturated_resets, default=None)
                if reached_type == "rate_limit_reached"
                else None
            )
            return {
                "status": "depleted",
                "source": "rate_limit_reached_type",
                "reached_type": reached_type,
                "resets_at": resets_at,
                "rate_summary": summary,
            }
        windows = summary["windows"]
        if windows and all(window.get("used_percent", 100) < 100 for window in windows):
            return {
                "status": "available",
                "source": "rate_limit_snapshot",
                "rate_summary": summary,
            }
        return {
            "status": "unknown",
            "source": "rate_limit_snapshot",
            "rate_summary": summary,
        }

    # Codex serializes this as a structured error code. Restrict the recursive
    # check to error-bearing event kinds so user prompt text can never trigger it.
    if payload_type in {"error", "stream_error", "turn_aborted", "task_complete"}:
        code = _usage_error_code(payload)
        if code:
            return {
                "status": "depleted",
                "source": "codex_error_info",
                "error_code": code,
            }
    return None


def _telegram_message_id_from_record(record: dict[str, Any]) -> int | None:
    """Recover the latest relayed Telegram message id from a Codex event."""
    if record.get("type") != "event_msg" or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    if payload.get("type") != "user_message":
        return None
    raw_message = payload.get("message")
    if isinstance(raw_message, str):
        text = raw_message
    else:
        try:
            text = json.dumps(raw_message, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    match = _settings.TELEGRAM_USER_MESSAGE_MARKER_RE.search(text)
    return int(match.group(1)) if match else None


def _usage_depletion_kind(value: dict[str, Any]) -> str | None:
    """Separate ordinary rate windows from credit/spend-control failures."""
    source = value.get("source")
    reached_type = value.get("reached_type")
    if source == "codex_error_info" or (
        reached_type in _settings.USAGE_LIMIT_REACHED_TYPES
        and reached_type != "rate_limit_reached"
    ):
        return "usage_or_credit"
    if source == "rate_limit_reached_type" and reached_type == "rate_limit_reached":
        return "rate_window"
    kind = value.get("depletion_kind")
    return str(kind) if kind in {"usage_or_credit", "rate_window"} else None


def refresh_codex_usage_state(
    meta: dict[str, Any] | None,
    state_path: Path,
    sessions_root: Path | None = None,
    *,
    now: float | None = None,
    tail_bytes: int = 4 * 1024 * 1024,
) -> dict[str, Any]:
    """Incrementally follow one managed Codex rollout for exact quota signals."""
    state = _state.read_json_object(state_path)
    now_value = time.time() if now is None else now
    if not meta or not meta.get("codex_session_path"):
        return state
    session_path = Path(str(meta["codex_session_path"]))
    if not _sessions.valid_codex_session_for_agent(
        meta, session_path, sessions_root=sessions_root
    ):
        return state

    session_text = str(session_path.resolve())
    size = session_path.stat().st_size
    discard_partial_line = False
    if state.get("session_path") == session_text:
        try:
            offset = int(state.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0 or offset > size:
            offset = max(0, size - max(1, tail_bytes))
            discard_partial_line = offset > 0
    else:
        offset = max(0, size - max(1, tail_bytes))
        discard_partial_line = offset > 0

    with session_path.open("rb") as handle:
        handle.seek(offset)
        if discard_partial_line:
            handle.readline()  # discard a possible partial first line
            offset = handle.tell()
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
            telegram_message_id = _telegram_message_id_from_record(record)
            if telegram_message_id is not None:
                state["last_telegram_message_id"] = telegram_message_id
            observation = _usage_limit_observation(record)
            if observation:
                rate_summary = observation.get("rate_summary")
                if isinstance(rate_summary, dict):
                    state["last_rate_summary"] = rate_summary
                if observation["status"] == "depleted":
                    failed_message_id = state.get("last_telegram_message_id")
                    newly_depleted = (
                        not bool(state.get("depleted"))
                        or failed_message_id != state.get("failed_message_id")
                        or state.get("session_path") != session_text
                    )
                    observation_kind = _usage_depletion_kind(observation)
                    current_kind = _usage_depletion_kind(state)
                    # A later ordinary rate-window event must never downgrade a
                    # structured credit/spend-control failure.
                    if not (
                        current_kind == "usage_or_credit"
                        and observation_kind == "rate_window"
                    ):
                        reset_value = (
                            observation.get("resets_at")
                            if observation_kind == "rate_window"
                            else None
                        )
                        state.update(
                            {
                                "depleted": True,
                                "depletion_kind": observation_kind,
                                "detected_ts": int(now_value),
                                "source": observation.get("source"),
                                "reached_type": observation.get("reached_type"),
                                "error_code": observation.get("error_code"),
                                "resets_at": reset_value,
                            }
                        )
                    if newly_depleted:
                        state.update(
                            {
                                "alert_pending": True,
                                "failed_message_id": failed_message_id,
                            }
                        )
                elif observation["status"] == "available" and state.get("depleted"):
                    state.update(
                        {
                            "depleted": False,
                            "cleared_ts": int(now_value),
                            "clear_reason": "new_turn_available_rate_event",
                            "alert_pending": False,
                        }
                    )
            offset = next_offset

    state.update(
        {
            "schema_version": 1,
            "agent_id": str(meta.get("agent_id") or ""),
            "session_path": session_text,
            "offset": offset,
            "updated_ts": int(now_value),
        }
    )
    try:
        resets_at = int(state.get("resets_at"))
    except (TypeError, ValueError):
        resets_at = None
    if (
        state.get("depleted")
        and _usage_depletion_kind(state) == "rate_window"
        and resets_at is not None
        and now_value >= resets_at
    ):
        state.update(
            {
                "depleted": False,
                "cleared_ts": int(now_value),
                "clear_reason": "reset_time_reached",
                "alert_pending": False,
            }
        )
    _state.write_json_object(state_path, state)
    return state


def clear_codex_usage_depletion(state_path: Path, reason: str) -> None:
    state = _state.read_json_object(state_path)
    if not state:
        return
    state.update(
        {
            "depleted": False,
            "cleared_ts": int(time.time()),
            "clear_reason": reason,
            "alert_pending": False,
            "updated_ts": int(time.time()),
        }
    )
    _state.write_json_object(state_path, state)


def notify_codex_usage_failure(
    token: str,
    chat_id: str,
    state_path: Path,
    log_path: Path,
    env: dict[str, str],
    max_log_chars: int,
) -> bool:
    """Tell Telegram exactly once when a relayed turn hits a structured limit."""
    state = _state.read_json_object(state_path)
    if not state.get("depleted") or not state.get("alert_pending"):
        return False
    reply_to_message_id = state.get("failed_message_id")
    try:
        reply_to_message_id = int(reply_to_message_id)
    except (TypeError, ValueError):
        reply_to_message_id = None
    source = str(
        state.get("reached_type")
        or state.get("error_code")
        or state.get("source")
        or "structured usage error"
    )
    try:
        _transport.send_reply(
            token,
            chat_id,
            "Codex could not run that relayed task: the live turn reported "
            f"{source}. The Telegram listener is still alive. This failure is "
            "recorded only for notification/audit; it will not block your next "
            "message from trying Codex again. Use /codex_usage for a fresh "
            "Codex /status query, or /codex_reset to inspect banked resets.",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as exc:
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "codex_usage_failure_notice_failed",
                "error": _transport.short_error(exc, env, max_log_chars),
                "reply_to_message_id": reply_to_message_id,
            },
        )
        return False
    state.update(
        {
            "alert_pending": False,
            "alert_sent_ts": int(time.time()),
            "updated_ts": int(time.time()),
        }
    )
    _state.write_json_object(state_path, state)
    _state.append_jsonl(
        log_path,
        {
            "ts": int(time.time()),
            "event": "codex_usage_failure_notice_sent",
            "source": source,
            "agent_id": state.get("agent_id"),
            "reply_to_message_id": reply_to_message_id,
        },
    )
    return True
