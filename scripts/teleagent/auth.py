"""Auth services for the Telegram relay."""

from __future__ import annotations


import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
import codex_rate_limits  # noqa: E402

from . import settings as _settings
from . import processes as _processes
from . import sessions as _sessions
from . import state as _state


def _nested_codex_error_code(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.lower() in _settings.CODEX_AUTH_ERROR_CODES else None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in {"error_info", "codex_error_info", "codexerrorinfo"}:
                code = _nested_codex_error_code(item)
                if code:
                    return code
            if isinstance(item, (dict, list)):
                code = _nested_codex_error_code(item)
                if code:
                    return code
    elif isinstance(value, list):
        for item in value:
            code = _nested_codex_error_code(item)
            if code:
                return code
    return None


def _nested_error_messages(value: Any) -> list[str]:
    messages: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {
                "message",
                "error_message",
                "errormessage",
            } and isinstance(item, str):
                messages.append(item)
            elif isinstance(item, (dict, list)):
                messages.extend(_nested_error_messages(item))
    elif isinstance(value, list):
        for item in value:
            messages.extend(_nested_error_messages(item))
    return messages


def _codex_auth_failure_observation(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("type") != "event_msg" or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    if str(payload.get("type") or "") not in {
        "error",
        "stream_error",
        "turn_aborted",
        "task_complete",
    }:
        return None
    code = _nested_codex_error_code(payload)
    if code != "unauthorized":
        return None
    for message in _nested_error_messages(payload):
        if _settings.CODEX_AUTH_ERROR_MESSAGE_RE.search(message):
            return {
                "status": "reauth_required",
                "source": "codex_error_info",
                "error_code": code,
                "reason": "refresh_token_revoked",
            }
    return None


def refresh_codex_auth_state(
    meta: dict[str, Any] | None,
    state_path: Path,
    sessions_root: Path | None = None,
    *,
    now: float | None = None,
    tail_bytes: int = 4 * 1024 * 1024,
) -> dict[str, Any]:
    """Incrementally follow the managed Codex rollout for terminal auth failures."""
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
            handle.readline()
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
            observation = _codex_auth_failure_observation(record)
            if observation:
                was_blocked = bool(state.get("blocked"))
                state.update(
                    {
                        "blocked": True,
                        "last_detected_ts": int(now_value),
                        "source": observation["source"],
                        "error_code": observation["error_code"],
                        "reason": observation["reason"],
                    }
                )
                if not was_blocked:
                    state.update(
                        {
                            "detected_ts": int(now_value),
                            "alert_pending": True,
                            "alert_sent_ts": None,
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
    _state.write_json_object(state_path, state)
    return state


def active_codex_auth_failure(state_path: Path) -> dict[str, Any] | None:
    state = _state.read_json_object(state_path)
    return state if state.get("blocked") else None


def mark_codex_auth_blocked(state_path: Path, reason: str) -> None:
    state = _state.read_json_object(state_path)
    now_value = int(time.time())
    state.update(
        {
            "schema_version": 1,
            "blocked": True,
            "detected_ts": state.get("detected_ts") or now_value,
            "last_detected_ts": now_value,
            "reason": reason,
            "updated_ts": now_value,
        }
    )
    _state.write_json_object(state_path, state)


def clear_codex_auth_failure(state_path: Path, reason: str) -> None:
    state = _state.read_json_object(state_path)
    if not state:
        return
    now_value = int(time.time())
    state.update(
        {
            "blocked": False,
            "alert_pending": False,
            "cleared_ts": now_value,
            "clear_reason": reason,
            "updated_ts": now_value,
        }
    )
    _state.write_json_object(state_path, state)


def format_auth_failure_fallback(
    state: dict[str, Any],
    reauth_state: dict[str, Any] | None = None,
) -> str:
    phase = str((reauth_state or {}).get("phase") or "")
    if phase in {"starting", "requesting_code", "awaiting_user"}:
        action = "The /reauth device sign-in is in progress; finish it, then resend this message."
    else:
        action = (
            "Run /reauth to sign in from Telegram; /agent_status shows the diagnostics."
        )
    return (
        "Codex authentication is broken because its refresh credential was revoked. "
        "This is a mechanical reply from the Telegram listener: no Codex agent was invoked "
        f"and your message was not queued. {action}"
    )


def process_is_alive(pid: Any) -> bool:
    try:
        pid_value = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_value <= 0:
        return False
    try:
        os.kill(pid_value, 0)
    except OSError:
        return False
    return True


def codex_reauth_in_progress(state: dict[str, Any]) -> bool:
    phase = str(state.get("phase") or "")
    if phase not in {"starting", "requesting_code", "awaiting_user"}:
        return False
    if process_is_alive(state.get("worker_pid")):
        return True
    try:
        started_ts = float(state.get("started_ts"))
    except (TypeError, ValueError):
        return False
    return phase == "starting" and time.time() - started_ts < 10


def format_codex_reauth_instructions(state: dict[str, Any]) -> str:
    verification_url = str(state.get("verification_url") or "").strip()
    user_code = str(state.get("user_code") or "").strip()
    if not _settings.DEVICE_URL_RE.fullmatch(verification_url) or not re.fullmatch(
        r"[A-Z0-9]{4}-[A-Z0-9]{4,8}", user_code
    ):
        return "Codex device sign-in is preparing a code. Try /agent_status in a few seconds."
    try:
        expires_ts = int(state.get("code_expires_ts"))
        expires = datetime.fromtimestamp(expires_ts, _settings.SGT).strftime(
            "%H:%M:%S SGT"
        )
    except (TypeError, ValueError, OSError):
        expires = "about 15 minutes"
    return (
        "**Codex sign-in required**\n\n"
        f"1. Open {verification_url}\n"
        f"2. Enter code `{user_code}`\n\n"
        f"Expires: `{expires}`\n"
        "Continue only because you started `/reauth` in this chat. "
        "The listener will restart the broken Codex pane after sign-in succeeds."
    )


def start_codex_reauth(
    repo_root: Path,
    state_path: Path,
    chat_id: str,
    sender_id: str,
) -> dict[str, Any]:
    current = _state.read_json_object(state_path)
    if codex_reauth_in_progress(current):
        return current
    attempt_seed = f"{time.time_ns()}:{os.getpid()}:{chat_id}:{sender_id}".encode(
        "utf-8"
    )
    attempt_id = hashlib.sha256(attempt_seed).hexdigest()[:20]
    state = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "phase": "starting",
        "started_ts": int(time.time()),
        "chat_id": chat_id,
        "sender_id": sender_id,
        "instructions_sent_ts": None,
        "completion_sent_ts": None,
        "failure_sent_ts": None,
    }
    _state.write_json_object(state_path, state)
    os.chmod(state_path, 0o600)
    worker = repo_root / "scripts" / "codex_device_auth.py"
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(worker),
                "--state",
                str(state_path),
                "--attempt-id",
                attempt_id,
                "--codex-bin",
                _processes.codex_executable(),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        state.update(
            {
                "phase": "failed",
                "failed_ts": int(time.time()),
                "error": f"Could not start Codex device authentication: {str(exc)[:300]}",
            }
        )
        _state.write_json_object(state_path, state)
    return _state.read_json_object(state_path)


def codex_login_status_summary() -> str:
    try:
        completed = subprocess.run(
            [_processes.codex_executable(), "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return "unavailable"
    output = "\n".join((completed.stdout, completed.stderr))
    for line in output.splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned.startswith(
            ("Logged in using ", "Not logged in", "Error checking login status:")
        ):
            return cleaned[:300]
    return "unavailable"


def run_codex_reset_helper(
    repo_root: Path,
    mode: str,
    *,
    timeout: float,
) -> tuple[int, str, str]:
    helper = repo_root / "scripts" / "manual_codex_usage_reset.sh"
    if mode not in {"--inspect", "--list", "--redeem", "--test-traversal"}:
        raise ValueError(f"unsupported reset helper mode: {mode}")
    process = subprocess.Popen(
        [str(helper), mode],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return 124, stdout, (stderr + "\nreset helper timed out").strip()
    return process.returncode, stdout, stderr


def inspect_codex_live_usage(repo_root: Path) -> dict[str, Any]:
    """Fetch a new account/rateLimits/read response; never use TUI or rollout snapshots."""
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return codex_rate_limits.read_rate_limits(
        _processes.codex_executable(),
        codex_home=codex_home,
        workdir=repo_root,
    )


def live_codex_remaining_percentages(live: dict[str, Any]) -> list[float]:
    """Parse only percentages returned by this invocation's account read."""
    if live.get("method") == "account/rateLimits/read":
        return codex_rate_limits.remaining_percentages(live)
    remaining: list[float] = []
    for raw_line in live.get("status_lines", []):
        match = re.search(
            r"\blimit:.*?([0-9]+(?:\.[0-9]+)?)%\s+left\b",
            str(raw_line),
            re.IGNORECASE,
        )
        if match:
            remaining.append(float(match.group(1)))
    return remaining


def live_codex_rate_availability(live: dict[str, Any]) -> bool | None:
    remaining = live_codex_remaining_percentages(live)
    if not remaining:
        return None
    return all(value > 0 for value in remaining)


def reconcile_codex_usage_state_from_live_query(
    state_path: Path,
    live: dict[str, Any],
) -> dict[str, Any]:
    """Record a live query result without turning it into a relay gate."""
    state = _state.read_json_object(state_path)
    now = int(time.time())
    availability = live_codex_rate_availability(live)
    state.update(
        {
            "last_live_query_checked_at": live.get("checked_at"),
            "last_live_status_lines": list(live.get("status_lines", [])),
            "last_live_rate_available": availability,
            "updated_ts": now,
        }
    )
    if availability is True:
        state.update(
            {
                "depleted": False,
                "cleared_ts": now,
                "clear_reason": "live_status_query_available",
                "alert_pending": False,
            }
        )
    elif availability is False:
        state.update(
            {
                "depleted": True,
                "depletion_kind": "rate_window",
                "detected_ts": now,
                "source": "live_status_query",
                "reached_type": "rate_limit_reached",
                "error_code": None,
                "resets_at": None,
                "alert_pending": False,
            }
        )
    _state.write_json_object(state_path, state)
    return state


def list_codex_usage_resets(repo_root: Path) -> tuple[int, list[str]]:
    returncode, stdout, stderr = run_codex_reset_helper(
        repo_root, "--list", timeout=120
    )
    if returncode != 0:
        detail = " ".join((stderr or stdout or "reset listing failed").split())[:500]
        raise RuntimeError(detail)
    available: int | None = None
    entries: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("AVAILABLE="):
            try:
                available = int(line.split("=", 1)[1])
            except ValueError:
                raise RuntimeError("reset listing returned an invalid count") from None
        elif line.startswith("RESET="):
            entry = " ".join(line.split("=", 1)[1].split())
            if entry:
                entries.append(entry)
    if available is None or available < 0:
        raise RuntimeError("reset listing did not return an available count")
    if available != len(entries) and available != 0:
        raise RuntimeError(
            f"reset listing was incomplete: reported {available}, parsed {len(entries)} expiry entries"
        )
    return available, entries


def reset_confirmation_state(
    state_path: Path,
    chat_id: str,
    sender_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    state = _state.read_json_object(state_path)
    now_value = time.time() if now is None else now
    if state.get("phase") != "awaiting_confirmation":
        return None
    try:
        expires_ts = int(state.get("expires_ts"))
    except (TypeError, ValueError):
        expires_ts = 0
    if expires_ts <= now_value:
        state.update({"phase": "expired", "expired_ts": int(now_value)})
        _state.write_json_object(state_path, state)
        return None
    if state.get("chat_id") != chat_id or state.get("sender_id") != sender_id:
        return None
    return state


def format_codex_reset_confirmation(
    available: int, entries: list[str], expires_ts: int
) -> str:
    lines = [f"Banked Codex resets remaining: {available}"]
    lines.extend(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
    expires_text = datetime.fromtimestamp(expires_ts, _settings.SGT).strftime(
        "%H:%M:%S SGT"
    )
    lines.append(
        f"Send /Confirm before {expires_text} to spend one Full reset. Any other text does not confirm."
    )
    return "\n".join(lines)


def format_live_codex_limits(live: dict[str, Any]) -> str:
    """Format only this invocation's fresh account rate-limit result."""
    if live.get("method") == "account/rateLimits/read":
        return codex_rate_limits.format_rate_limits(live)
    checked_at = str(live.get("checked_at") or "unknown time")
    version = str(live.get("codex_version") or "unknown")
    lines = [f"Live Codex /status query ({checked_at}, CLI {version}):"]
    lines.extend(f"- {line}" for line in live.get("status_lines", []))
    lines.append(
        "- The percentages above are rate-limit windows, not a workspace "
        "credit/spend-control balance; Codex /status does not expose that balance."
    )
    availability = live_codex_rate_availability(live)
    if availability is True:
        lines.append(
            "- Live rate-window status: available. Prior limit errors are not "
            "used as a gate; the next Codex turn is the final live availability check."
        )
    elif availability is False:
        lines.append(
            "- Live rate-window status: depleted now. Future messages are still "
            "checked by attempting a new Codex turn, not blocked by this result."
        )
    else:
        lines.append(
            "- Live rate-window status: unavailable from this /status response."
        )
    return "\n".join(lines)
