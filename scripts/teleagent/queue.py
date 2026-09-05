"""Queue services for the Telegram relay."""

from __future__ import annotations


import argparse
import time
from pathlib import Path
from typing import Any

from . import settings as _settings
from . import commands as _commands
from . import auth as _auth
from . import lifecycle as _lifecycle
from . import messages as _messages
from . import processes as _processes
from . import routing as _routing
from . import state as _state
from . import submission as _submission
from . import transport as _transport


def relay_queue_state_path(args: argparse.Namespace) -> Path | None:
    configured = getattr(args, "relay_queue_state_path", None)
    if configured:
        return Path(configured)
    confirmation = getattr(args, "relay_confirmation_state_path", None)
    if confirmation:
        return Path(confirmation).with_name("telegram_relay_queue.state.json")
    return None


def telegram_update_is_agent_message(
    update: dict[str, Any], bot_username: str = ""
) -> bool:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return False
    if (
        isinstance(message.get("document"), dict)
        or isinstance(message.get("voice"), dict)
        or isinstance(message.get("photo"), list)
    ):
        return True
    command, _ = _messages.normalize_command(
        _routing.strip_bot_mention(str(message.get("text") or ""), bot_username)
    )
    return command in {
        "(agent-message)",
        "/agent",
        "/replay_last",
        "/replay_last_long",
        "/replay_messages",
    }


@_state.serialized
def enqueue_telegram_relay(
    state_path: Path,
    update: dict[str, Any],
    target_pane: str,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist one normal Telegram update for FIFO delivery."""
    message = update.get("message") or update.get("edited_message") or {}
    update_id = update.get("update_id")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    queue_id = f"{update_id}:{message_id}"
    queued_ts = time.time() if now is None else now
    task = {
        "id": queue_id,
        "message_id": message_id,
        "queued_ts": queued_ts,
        "status": "queued",
        "target_pane": target_pane,
        "update": update,
        "update_id": update_id,
    }
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    tasks = (
        [item for item in raw_tasks if isinstance(item, dict)]
        if isinstance(raw_tasks, list)
        else []
    )
    existing = next((item for item in tasks if item.get("id") == queue_id), None)
    if existing is not None:
        return existing, False
    tasks.append(task)
    state.update({"tasks": tasks, "updated_ts": queued_ts, "version": 1})
    _state.write_json_object(state_path, state)
    return task, True


def telegram_relay_queue_tasks(state_path: Path | None) -> list[dict[str, Any]]:
    if state_path is None:
        return []
    raw_tasks = _state.read_json_object(state_path).get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    return [dict(item) for item in raw_tasks if isinstance(item, dict)]


@_state.serialized
def remove_telegram_relay_queue_task(state_path: Path, queue_id: str) -> None:
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    tasks = (
        [item for item in raw_tasks if isinstance(item, dict)]
        if isinstance(raw_tasks, list)
        else []
    )
    remaining = [item for item in tasks if item.get("id") != queue_id]
    state.update({"tasks": remaining, "updated_ts": time.time(), "version": 1})
    _state.write_json_object(state_path, state)


@_state.serialized
def block_telegram_relay_queue_task(
    state_path: Path,
    queue_id: str,
    error: str,
) -> dict[str, Any] | None:
    """Quarantine one queue item after unsafe goal-pause delivery fails.

    A blocked item must not remain at the head of the live FIFO: the drain
    loop deliberately refuses to retry the Escape/Goal-pause path, so leaving
    it in ``tasks`` would starve every later Telegram message forever.  Keep a
    copy in the state file's ``failed`` archive for inspection/replay while
    removing it from the active queue.
    """
    state = _state.read_json_object(state_path)
    raw_tasks = state.get("tasks")
    tasks = (
        [item for item in raw_tasks if isinstance(item, dict)]
        if isinstance(raw_tasks, list)
        else []
    )
    raw_failed = state.get("failed")
    failed = (
        [item for item in raw_failed if isinstance(item, dict)]
        if isinstance(raw_failed, list)
        else []
    )
    blocked: dict[str, Any] | None = None
    now = time.time()
    remaining: list[dict[str, Any]] = []
    for item in tasks:
        if item.get("id") != queue_id:
            remaining.append(item)
            continue
        blocked = dict(item)
        blocked["attempts"] = int(blocked.get("attempts", 0)) + 1
        blocked["blocked_ts"] = now
        blocked["failed_ts"] = now
        blocked["last_error"] = error
        blocked["status"] = "blocked"
        continue
    if blocked is not None:
        # Replace an existing archived copy rather than duplicating it when a
        # listener restarts while migrating an older blocked head item.
        failed = [item for item in failed if item.get("id") != queue_id]
        failed.append(blocked)
    state.update(
        {"tasks": remaining, "failed": failed, "updated_ts": now, "version": 1}
    )
    _state.write_json_object(state_path, state)
    return blocked


def same_chat_can_steer(args, update: dict[str, Any]) -> bool:
    """Only an already-bound chat may steer an ordinary active turn."""
    state_text = getattr(args, "agent_message_state_path", None)
    if not state_text:
        return False
    active = _state.read_json_object(Path(state_text))
    message = update.get("message") or update.get("edited_message") or {}
    source = str((message.get("chat") or {}).get("id") or "")
    if (
        not active.get("route_locked")
        or active.get("turn_conflicted")
        or active.get("active_chat_id") != source
        or active.get("active_topic_id") != message.get("message_thread_id")
    ):
        return False
    checkpoint = _submission.codex_session_checkpoint(args.target_pane)
    return bool(
        checkpoint
        and str(checkpoint[0].resolve()) == active.get("session_path")
        and _submission.codex_session_turn_active(checkpoint[0])
        and not _processes.codex_goal_active(args.target_pane)
    )


def dispatch_telegram_update(
    update: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
    token: str,
    allowed_chat_id: str,
    log_path: Path,
    *,
    owner_user_id: str = "",
    bot_username: str = "",
    group_member_cache: dict[str, tuple[float, bool]] | None = None,
    route_state_path: Path | None = None,
) -> str:
    """Handle one update or persist a normal message behind an unsafe composer."""
    queue_path = relay_queue_state_path(args)
    confirmation_text = getattr(args, "relay_confirmation_state_path", None)
    confirmation_path = Path(confirmation_text) if confirmation_text else None
    should_queue = False
    source_chat_id, _is_group, _group_owner = _routing.resolve_source_chat(
        update,
        allowed_chat_id,
        owner_user_id,
        bot_username,
        token,
        group_member_cache if group_member_cache is not None else {},
    )
    authorized_chat = source_chat_id is not None
    if (
        authorized_chat
        and telegram_update_is_agent_message(update, bot_username=bot_username)
        and queue_path is not None
    ):
        steering = same_chat_can_steer(args, update)
        should_queue = _lifecycle.agent_lifecycle_operation_in_progress(args)
        if not should_queue and not steering:
            should_queue = bool(telegram_relay_queue_tasks(queue_path))
        if not should_queue and confirmation_path is not None:
            should_queue = bool(
                _submission.current_pending_codex_submissions(
                    confirmation_path,
                    args.target_pane,
                )
            )
        if not should_queue and not steering:
            checkpoint = _submission.codex_session_checkpoint(args.target_pane)
            should_queue = bool(
                checkpoint is not None
                and _submission.codex_session_turn_active(checkpoint[0])
            )
    if should_queue and queue_path is not None:
        task, created = enqueue_telegram_relay(
            queue_path,
            update,
            args.target_pane,
        )
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": (
                    "telegram_relay_queued"
                    if created
                    else "telegram_relay_already_queued"
                ),
                "message_id": task.get("message_id"),
                "queue_id": task.get("id"),
                "target_pane": args.target_pane,
                "update_id": task.get("update_id"),
            },
        )
        if created:
            message = update.get("message") or update.get("edited_message") or {}
            _transport.send_reply(
                token,
                source_chat_id,
                f"Queued ({task['id']}). /queue shows waiting work; /cancel {task['id']} removes it.",
                message_thread_id=message.get("message_thread_id"),
            )
        return "queued"

    _commands.handle_update(
        update,
        args,
        env,
        token,
        allowed_chat_id,
        log_path,
        owner_user_id=owner_user_id,
        bot_username=bot_username,
        group_member_cache=group_member_cache,
        route_state_path=route_state_path,
    )
    return "handled"


def _fail_stale_pending_codex_submissions(
    state_path: Path,
    target_pane: str,
    *,
    reason: str,
    older_than: float,
) -> int:
    """Move lost, idle submissions out of the confirmation blocker state.

    A pending marker can be absent because the prompt was accepted but its
    JSONL event is delayed, so only retire it when the linked turn is idle and
    the marker is absent from the pane.  Composer/history entries remain
    pending so recovery can still press Enter or wait for JSONL persistence.
    """
    state = _state.read_json_object(state_path)
    raw_pending = state.get("pending")
    if not isinstance(raw_pending, list):
        return 0
    now = time.time()
    kept: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = state.get("failed")
    if not isinstance(failed, list):
        failed = []
    moved = 0
    current_process_identity: str | None = None
    process_identity_checked = False
    for raw_item in raw_pending:
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("target_pane") != target_pane:
            kept.append(raw_item)
            continue
        try:
            created_ts = float(raw_item.get("created_ts") or now)
        except (TypeError, ValueError):
            created_ts = now
        if now - created_ts < older_than:
            kept.append(raw_item)
            continue

        session_text = raw_item.get("session_path")
        if isinstance(session_text, str) and session_text:
            try:
                if _submission.codex_session_turn_active(Path(session_text)):
                    kept.append(raw_item)
                    continue
            except (OSError, ValueError):
                # A missing/unreadable old rollout cannot prove that a live
                # turn is active; pane and process checks below still apply.
                pass

        submitted_process_identity = raw_item.get("codex_process_identity")
        if isinstance(submitted_process_identity, str) and submitted_process_identity:
            if not process_identity_checked:
                current_process_identity = _processes.codex_process_identity(
                    target_pane
                )
                process_identity_checked = True
            if (
                current_process_identity
                and current_process_identity != submitted_process_identity
            ):
                # Let the normal replacement-process replay path get one
                # chance to recover the exact payload before retiring it.
                kept.append(raw_item)
                continue

        if _submission.pending_codex_submission_pane_location(raw_item) != "absent":
            kept.append(raw_item)
            continue

        item = dict(raw_item)
        item["stalled_reason"] = reason
        item["stalled_ts"] = now
        item["failed_ts"] = now
        marker = item.get("marker")
        message_id = item.get("message_id")
        failed = [
            archived
            for archived in failed
            if not (
                isinstance(archived, dict)
                and (
                    (marker and archived.get("marker") == marker)
                    or (
                        message_id is not None
                        and archived.get("message_id") == message_id
                    )
                )
            )
        ]
        failed.append(item)
        moved += 1
    state.update(
        {
            "pending": kept,
            "failed": failed,
            "updated_ts": int(now),
        }
    )
    _state.write_json_object(state_path, state)
    return moved


def _serialize_drain(function):
    from functools import wraps

    @wraps(function)
    def wrapped(args, *positional, **kwargs):
        path = relay_queue_state_path(args)
        if path is None:
            return []
        with _state.locked(path):
            return function(args, *positional, **kwargs)

    return wrapped


@_serialize_drain
def drain_telegram_relay_queue(
    args: argparse.Namespace,
    env: dict[str, str],
    token: str,
    allowed_chat_id: str,
    log_path: Path,
    *,
    owner_user_id: str = "",
    bot_username: str = "",
    group_member_cache: dict[str, tuple[float, bool]] | None = None,
    route_state_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Deliver at most one persisted normal update while Codex is idle."""
    queue_path = relay_queue_state_path(args)
    tasks = telegram_relay_queue_tasks(queue_path)
    if queue_path is None or not tasks:
        return []
    if _lifecycle.agent_lifecycle_operation_in_progress(args):
        return []
    if _lifecycle.agent_desired_state(args) == _settings.AGENT_DESIRED_STOPPED:
        return []
    auth_path = getattr(args, "codex_auth_state_path", None)
    if auth_path and _auth.active_codex_auth_failure(Path(auth_path)):
        return []

    task = tasks[0]
    update = task.get("update")
    queue_id = str(task.get("id") or "")
    if not isinstance(update, dict) or not queue_id:
        state = _state.read_json_object(queue_path)
        raw_tasks = state.get("tasks")
        remaining = list(raw_tasks[1:]) if isinstance(raw_tasks, list) else []
        state.update({"tasks": remaining, "updated_ts": time.time(), "version": 1})
        _state.write_json_object(queue_path, state)
        return [{"id": queue_id, "event": "invalid_removed"}]
    if task.get("status") == "blocked":
        # Older listeners left failed goal-pause items in ``tasks``.  Migrate
        # such a head item out of the live FIFO so it cannot starve messages
        # behind it; the original payload remains in ``failed``.
        blocked = block_telegram_relay_queue_task(
            queue_path,
            queue_id,
            str(task.get("last_error") or "goal-pause delivery was blocked"),
        )
        if blocked is None:
            return []
        event = {
            "ts": int(time.time()),
            "event": "telegram_relay_queue_blocked_quarantined",
            "message_id": blocked.get("message_id"),
            "queue_id": queue_id,
            "target_pane": args.target_pane,
        }
        _state.append_jsonl(log_path, event)
        return [event]

    marker = task.get("marker")
    session_text = task.get("delivery_session_path")
    try:
        delivery_offset = max(0, int(task.get("delivery_session_offset", 0)))
    except (TypeError, ValueError):
        delivery_offset = 0
    if (
        task.get("status") == "delivering"
        and isinstance(marker, str)
        and marker
        and isinstance(session_text, str)
        and session_text
        and _submission.wait_for_codex_submission(
            (Path(session_text), delivery_offset), marker, timeout=0
        )
    ):
        remove_telegram_relay_queue_task(queue_path, queue_id)
        event = {
            "ts": int(time.time()),
            "event": "telegram_relay_queue_delivery_recovered",
            "message_id": task.get("message_id"),
            "queue_id": queue_id,
            "target_pane": args.target_pane,
        }
        _state.append_jsonl(log_path, event)
        return [event]

    if task.get("status") == "delivering":
        block_telegram_relay_queue_task(
            queue_path,
            queue_id,
            "Delivery interrupted before confirmation; inspect /queue before resending.",
        )
        return []

    confirmation_text = getattr(args, "relay_confirmation_state_path", None)
    if confirmation_text:
        confirmation_path = Path(confirmation_text)
        blockers = _submission.current_pending_codex_submissions(
            confirmation_path, args.target_pane
        )
        if blockers:
            # A pending relay whose marker will never be confirmed must not
            # wedge the queue forever. After the grace window, retire it to
            # failed instead of failing closed for everything behind it.
            moved = _fail_stale_pending_codex_submissions(
                confirmation_path,
                args.target_pane,
                reason="stale_pending_cleared",
                older_than=_settings.RELAY_PENDING_WEDGE_SECONDS,
            )
            if moved:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "telegram_relay_stale_pending_cleared",
                        "cleared": moved,
                        "target_pane": args.target_pane,
                    },
                )
                blockers = _submission.current_pending_codex_submissions(
                    confirmation_path, args.target_pane
                )
        if blockers:
            return []
    checkpoint = _submission.codex_session_checkpoint(args.target_pane)
    if checkpoint is not None and _submission.codex_session_turn_active(checkpoint[0]):
        # Waiting work never interrupts an active turn, including Goal mode.
        # The explicit /interrupt command is the only abort-and-replace path.
        return []

    message_id = task.get("message_id")
    marker = (
        f"[TELEGRAM USER MESSAGE message_id={message_id}"
        if isinstance(message_id, int)
        else None
    )

    state = _state.read_json_object(queue_path)
    raw_tasks = state.get("tasks")
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if isinstance(item, dict) and item.get("id") == queue_id:
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["delivery_started_ts"] = time.time()
                item["status"] = "delivering"
                item["marker"] = marker
                if checkpoint is not None:
                    item["delivery_session_path"] = str(checkpoint[0])
                    item["delivery_session_offset"] = checkpoint[1]
                break
        state.update({"tasks": raw_tasks, "updated_ts": time.time(), "version": 1})
        _state.write_json_object(queue_path, state)

    try:
        record = _commands.handle_update(
            update,
            args,
            env,
            token,
            allowed_chat_id,
            log_path,
            owner_user_id=owner_user_id,
            bot_username=bot_username,
            group_member_cache=group_member_cache,
            route_state_path=route_state_path,
        )
    except Exception as exc:
        block_telegram_relay_queue_task(
            queue_path, queue_id, _transport.short_error(exc, env)
        )
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "telegram_relay_queue_delivery_failed",
                "message_id": task.get("message_id"),
                "queue_id": queue_id,
                "error": _transport.short_error(exc, env),
            },
        )
        return []

    remove_telegram_relay_queue_task(queue_path, queue_id)
    relay_result = record.get("relay_result") if isinstance(record, dict) else None
    event = {
        "ts": int(time.time()),
        "event": (
            "telegram_relay_queue_delivered"
            if isinstance(relay_result, str) and relay_result.startswith("relayed to ")
            else "telegram_relay_queue_processed"
        ),
        "message_id": task.get("message_id"),
        "queue_id": queue_id,
        "target_pane": args.target_pane,
    }
    _state.append_jsonl(log_path, event)
    return [event]
