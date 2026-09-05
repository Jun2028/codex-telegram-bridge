"""Commands services for the Telegram relay."""

from __future__ import annotations


import argparse
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402
from notify import redact

from . import settings as _settings
from . import attachments as _attachments
from . import auth as _auth
from . import identity as _identity
from . import lifecycle as _lifecycle
from . import messages as _messages
from . import models as _models
from . import processes as _processes
from . import routing as _routing
from . import schedule as _schedule
from . import state as _state
from . import status as _status
from . import submission as _submission
from . import transport as _transport
from . import replies as _replies
from . import usage as _usage


def handle_update(
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
) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    route_id = _messages.telegram_update_route_id(update)
    source_chat_id, is_group, group_owner = _routing.resolve_source_chat(
        update,
        allowed_chat_id,
        owner_user_id,
        bot_username,
        token,
        group_member_cache if group_member_cache is not None else {},
    )
    if source_chat_id is None:
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "update_id": update.get("update_id"),
                "ignored": True,
                "reason": (
                    "chat_id_mismatch" if not is_group else "group_not_authorized"
                ),
                "chat_id": chat_id,
                "chat_type": str(chat.get("type") or ""),
            },
        )
        return
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    chat_type = str(chat.get("type") or "")
    chat_record_log_path = log_path
    if source_chat_id is not None and (
        is_group or _identity.per_chat_inbox_records_enabled()
    ):
        split_path = _identity.per_chat_inbox_record_path(
            log_path,
            source_chat_id,
            chat_type,
            force=is_group,
        )
        if split_path is not None:
            chat_record_log_path = split_path

    raw_document = message.get("document")
    document = raw_document if isinstance(raw_document, dict) else None
    raw_voice = message.get("voice")
    voice = raw_voice if isinstance(raw_voice, dict) else None
    photo = _attachments.largest_telegram_photo(message)
    has_attachment = document is not None or voice is not None or photo is not None
    text = (
        str(message.get("caption") or "").strip()
        if has_attachment
        else str(message.get("text") or "").strip()
    )
    if is_group:
        text = _routing.strip_bot_mention(text, bot_username)
    command, payload = (
        ("(agent-message)", text)
        if has_attachment
        else _messages.normalize_command(text)
    )
    if (
        is_group
        and not group_owner
        and command not in {"", "(agent-message)", "/status", "/ping", "/help"}
    ):
        _replies.send(
            args,
            token,
            source_chat_id,
            "Only the bot owner can use agent controls.",
            message_thread_id=message.get("message_thread_id"),
        )
        return None
    if is_group and command in {
        "/reauth",
        "/codex_reset",
        "/codex_usage",
        "/confirm",
        "/agent_status",
    }:
        _replies.send(
            args,
            token,
            source_chat_id,
            "Open the bot's private chat for account controls.",
            message_thread_id=message.get("message_thread_id"),
        )
        return None
    if route_state_path is not None and route_id:
        _routing.set_reply_route_chat_id(
            route_state_path,
            source_chat_id,
            is_group=is_group,
            route_id=route_id,
            message_thread_id=message.get("message_thread_id"),
            source_message_id=message.get("message_id"),
        )
    if is_group and not text and not has_attachment:
        _state.append_jsonl(
            chat_record_log_path,
            {
                "ts": int(time.time()),
                "update_id": update.get("update_id"),
                "ignored": True,
                "reason": "group_empty_message",
                "chat_id": str(chat.get("id", "")),
                "chat_type": chat_type,
                "message_id": message.get("message_id"),
            },
        )
        return
    telegram_date = message.get("date")
    redacted_text = redact(text, env)
    record = {
        "ts": int(time.time()),
        "ts_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telegram_date": telegram_date,
        "telegram_iso": (
            datetime.fromtimestamp(int(telegram_date), timezone.utc).isoformat(
                timespec="seconds"
            )
            if telegram_date
            else None
        ),
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "sender": _messages.sender_label(message),
        "chat_id": source_chat_id,
        "chat_type": str(chat.get("type") or ""),
        "group_owner": bool(group_owner),
        "command": command or "(empty)",
        "text": redacted_text[: args.max_log_chars],
        "text_full": redacted_text,
        "text_truncated": len(redacted_text) > args.max_log_chars,
    }
    if document is not None:
        record["document"] = {
            "file_name": redact(str(document.get("file_name") or ""), env),
            "mime_type": str(document.get("mime_type") or ""),
            "file_size": document.get("file_size"),
        }
    if voice is not None:
        record["voice"] = {
            "duration_seconds": voice.get("duration"),
            "mime_type": str(voice.get("mime_type") or "audio/ogg"),
            "file_size": voice.get("file_size"),
        }
    if photo is not None:
        record["photo"] = {
            "width": photo.get("width"),
            "height": photo.get("height"),
            "file_size": photo.get("file_size"),
        }
    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        replied_text = redact(_messages.visible_message_text(replied), env)
        record["reply_to_message_id"] = replied.get("message_id")
        record["reply_to_sender"] = _messages.sender_label(replied)
        record["reply_to_text"] = replied_text[: args.max_log_chars]
        record["reply_to_text_full"] = replied_text
        record["reply_to_text_truncated"] = len(replied_text) > args.max_log_chars

    reply_chunks: list[str] | None = None
    usage_state_path = Path(args.codex_usage_state_path)
    reset_state_path = Path(args.codex_reset_state_path)
    auth_state_path = Path(
        getattr(
            args,
            "codex_auth_state_path",
            usage_state_path.with_name("telegram_codex_auth.state.json"),
        )
    )
    reauth_state_path = Path(
        getattr(
            args,
            "codex_reauth_state_path",
            usage_state_path.with_name("telegram_codex_reauth.state.json"),
        )
    )
    auth_failure = _auth.active_codex_auth_failure(auth_state_path)
    reauth_state = _state.read_json_object(reauth_state_path)
    sender_id = str((message.get("from") or {}).get("id", ""))
    pending_reset = _auth.reset_confirmation_state(reset_state_path, chat_id, sender_id)
    if command == "/codex_usage":
        try:
            live_usage = _auth.inspect_codex_live_usage(args.repo_root)
            _auth.reconcile_codex_usage_state_from_live_query(
                usage_state_path,
                live_usage,
            )
            reply = _auth.format_live_codex_limits(live_usage)
            record["action"] = "codex_usage_live"
            record["codex_live_usage_checked_at"] = live_usage.get("checked_at")
            record["codex_live_status_lines"] = live_usage.get("status_lines")
        except Exception as exc:
            reply = (
                "Fresh Codex account/rateLimits/read failed; no cached usage value was "
                f"substituted. Detail: {_transport.short_error(exc, env)}"
            )
            record["action"] = "codex_usage_live_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/codex_reset":
        live_usage: dict[str, Any] | None = None
        live_error: str | None = None
        try:
            live_usage = _auth.inspect_codex_live_usage(args.repo_root)
            _auth.reconcile_codex_usage_state_from_live_query(
                usage_state_path,
                live_usage,
            )
            record["codex_live_usage_checked_at"] = live_usage.get("checked_at")
            record["codex_live_status_lines"] = live_usage.get("status_lines")
        except Exception as exc:
            live_error = _transport.short_error(exc, env)
            record["codex_live_usage_error"] = live_error
        try:
            available, entries = _auth.list_codex_usage_resets(args.repo_root)
            if live_usage is not None:
                live_status_text = _auth.format_live_codex_limits(live_usage)
            else:
                live_status_text = (
                    "Fresh Codex account/rateLimits/read failed; no cached usage percentage "
                    f"was substituted. Detail: {live_error or 'unknown error'}"
                )
            if available == 0:
                reply = (
                    live_status_text
                    + "\n\nBanked Codex resets remaining: 0. No reset was redeemed."
                )
                _state.write_json_object(
                    reset_state_path,
                    {
                        "phase": "unavailable",
                        "checked_ts": int(time.time()),
                        "chat_id": chat_id,
                        "sender_id": sender_id,
                    },
                )
                record["action"] = "codex_reset_unavailable"
            else:
                requested_ts = int(time.time())
                expires_ts = requested_ts + _settings.CODEX_RESET_CONFIRM_TTL_SECONDS
                _state.write_json_object(
                    reset_state_path,
                    {
                        "phase": "awaiting_confirmation",
                        "requested_ts": requested_ts,
                        "expires_ts": expires_ts,
                        "chat_id": chat_id,
                        "sender_id": sender_id,
                        "available": available,
                        "entries": entries,
                    },
                )
                reply = (
                    live_status_text
                    + "\n\n"
                    + _auth.format_codex_reset_confirmation(
                        available, entries, expires_ts
                    )
                )
                record["action"] = "codex_reset_confirmation_requested"
                record["available_resets"] = available
                record["confirmation_expires_ts"] = expires_ts
        except Exception as exc:
            if live_usage is not None:
                live_prefix = _auth.format_live_codex_limits(live_usage) + "\n\n"
            else:
                live_prefix = (
                    "Fresh Codex account/rateLimits/read failed; no cached usage percentage "
                    f"was substituted. Detail: {live_error or 'unknown error'}\n\n"
                )
            reply = (
                live_prefix
                + "Could not inspect banked Codex resets: "
                + _transport.short_error(exc, env)
            )
            record["action"] = "codex_reset_list_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/confirm":
        raw_reset_state = _state.read_json_object(reset_state_path)
        if pending_reset:
            pending_reset.update(
                {"phase": "executing", "confirmed_ts": int(time.time())}
            )
            _state.write_json_object(reset_state_path, pending_reset)
            _replies.send(
                args,
                token,
                source_chat_id,
                "Confirmed. Mechanically redeeming one Full reset; the automatic reset watchdog will be restored afterward.",
            )
            record["action"] = "codex_reset_confirmed"
            redeemed = False
            reset_stage = "redeem the reset"
            try:
                returncode, stdout, stderr = _auth.run_codex_reset_helper(
                    args.repo_root, "--redeem", timeout=180
                )
                redeemed = "RESET_SUCCESS" in stdout.splitlines()
                if redeemed:
                    pending_reset.update(
                        phase="redeemed", redeemed=True, redeemed_ts=int(time.time())
                    )
                    _state.write_json_object(reset_state_path, pending_reset)
                if returncode != 0 or not redeemed:
                    detail = " ".join(
                        (stderr or stdout or "redemption failed").split()
                    )[:500]
                    raise RuntimeError(detail)
                reset_stage = "refresh usage state"
                _usage.clear_codex_usage_depletion(
                    usage_state_path, "manual_usage_reset_redeemed"
                )
                restart_agent = (
                    _lifecycle.agent_desired_state(args)
                    == _settings.AGENT_DESIRED_RUNNING
                )
                target_pane = args.target_pane
                start_result = "agent remained stopped by operator request"
                meta = None
                if restart_agent:
                    reset_stage = "restart the agent"
                    target_pane, start_result, meta = _lifecycle.start_codex_agent(
                        repo_root=args.repo_root,
                        session=args.session,
                        window=args.codex_window,
                        restart=True,
                    )
                    args.target_pane = target_pane
                completed_state = {
                    **pending_reset,
                    "phase": "completed",
                    "completed_ts": int(time.time()),
                    "target_pane": target_pane,
                    "agent_id": meta.get("agent_id") if meta else None,
                    "agent_restarted": restart_agent,
                }
                _state.write_json_object(reset_state_path, completed_state)
                reply = (
                    "One Full reset was redeemed successfully. The Telegram Codex agent "
                    "was restarted; resend your task now."
                    if restart_agent
                    else (
                        "One Full reset was redeemed successfully. The Telegram Codex "
                        "agent remains stopped; use /start_agent when wanted."
                    )
                )
                record["relay_result"] = start_result
                record["target_pane"] = target_pane
                record["agent_id"] = meta.get("agent_id") if meta else None
            except Exception as exc:
                failed_state = {
                    **pending_reset,
                    "phase": "redeemed_followup_failed"
                    if redeemed
                    else "redemption_unconfirmed",
                    "redeemed": redeemed,
                    "failed_stage": reset_stage,
                    "failed_ts": int(time.time()),
                    "error": _transport.short_error(exc, env),
                }
                _state.write_json_object(reset_state_path, failed_state)
                reply = (
                    f"The reset was redeemed, but the listener could not {reset_stage}. Do not redeem another reset for this failure. "
                    if redeemed
                    else "Reset redemption could not be confirmed. Check /codex_usage and /codex_reset before trying again. "
                ) + _transport.short_error(exc, env)
                record["action"] = (
                    "codex_reset_followup_failed"
                    if redeemed
                    else "codex_reset_unconfirmed"
                )
                record["error"] = _transport.short_error(exc, env, args.max_log_chars)
        elif raw_reset_state.get("phase") == "executing":
            reply = "A confirmed Codex reset is already running."
            record["action"] = "codex_reset_already_running"
        else:
            reply = "No unexpired /codex_reset confirmation is pending. Nothing was changed."
            record["action"] = "codex_reset_confirmation_missing"
    elif text == "Confirm" and pending_reset:
        reply = "Use /Confirm (with the slash) to approve the pending Codex reset. Nothing was changed."
        record["action"] = "codex_reset_plain_confirm_rejected"
    elif command == "/timed":
        try:
            timed_state_path = Path(
                getattr(
                    args,
                    "timed_message_state_path",
                    _settings.DEFAULT_TELEGRAM_LOG_DIR
                    / "telegram_timed_messages.state.json",
                )
            )
            subcommand = payload.strip()
            if subcommand.lower() == "list":
                tasks = _schedule.timed_messages_for_chat(timed_state_path, chat_id)
                reply_chunks = _schedule.format_timed_message_list(tasks)
                reply = None
                record["action"] = "timed_message_listed"
                record["timed_message_count"] = len(tasks)
            elif subcommand.partition(" ")[0].lower() == "remove":
                number = _schedule.parse_timed_remove_number(subcommand)
                removed = _schedule.remove_timed_messages(
                    timed_state_path,
                    chat_id,
                    number,
                )
                in_flight = sum(
                    str(task.get("status") or "").lower() in {"delivering", "submitted"}
                    for task in removed
                )
                if not removed:
                    reply = (
                        "**Active timed messages**\n\n"
                        "No active timed messages were scheduled, so nothing was removed."
                    )
                elif number is None:
                    noun = "message" if len(removed) == 1 else "messages"
                    reply = (
                        "**Timed messages removed**\n\n"
                        f"Removed {len(removed)} timed {noun}."
                    )
                else:
                    task = removed[0]
                    reply = (
                        f"**Timed message {number} removed**\n\n"
                        f"Status: **{_schedule.timed_message_status_label(task)}**\n"
                        f"Due: `{_schedule.timed_message_due_label(task)}`\n"
                        f"\n**Message**\n> {_schedule.timed_message_preview(task)}"
                    )
                if in_flight:
                    noun = "message was" if in_flight == 1 else "messages were"
                    reply += (
                        f"\n\nWarning: {in_flight} {noun} already submitted to Codex "
                        "and cannot be recalled."
                    )
                record["action"] = (
                    "timed_messages_removed_all"
                    if number is None
                    else "timed_message_removed"
                )
                record["timed_message_number"] = number
                record["timed_message_removed_count"] = len(removed)
                record["timed_message_removed_ids"] = [
                    task.get("id") for task in removed
                ]
            else:
                hours, timed_text = _schedule.parse_timed_payload(payload)
                task, created = _schedule.schedule_timed_message(
                    timed_state_path,
                    message,
                    timed_text,
                    hours,
                )
                due = datetime.fromtimestamp(float(task["due_ts"]), _settings.SGT)
                heading = (
                    "**Timed message scheduled**"
                    if created
                    else "**Timed message already scheduled**"
                )
                reply = (
                    f"{heading}\n\n"
                    f"Due: `{due.strftime('%Y-%m-%d %H:%M:%S SGT')}`\n"
                    f"Delay: `{hours:g} hours`\n"
                    f"\n**Message**\n> {_schedule.timed_message_preview(task)}"
                )
                record["action"] = (
                    "timed_message_scheduled" if created else "timed_message_duplicate"
                )
                record["timed_message_id"] = task.get("id")
                record["timed_message_due_ts"] = task.get("due_ts")
                record["timed_message_hours"] = task.get("hours")
        except Exception as exc:
            reply = (
                f"Could not process /timed command: {_transport.short_error(exc, env)}"
            )
            record["action"] = "timed_message_rejected"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/reauth":
        if str(reauth_state.get("phase") or "") == "authenticated":
            reply = "Codex sign-in succeeded; the listener is finishing the agent restart now."
            record["action"] = "codex_reauth_authenticated"
        elif _auth.codex_reauth_in_progress(reauth_state):
            reply = (
                _auth.format_codex_reauth_instructions(reauth_state)
                if reauth_state.get("phase") == "awaiting_user"
                else "Codex device sign-in is already starting. The code will appear here shortly."
            )
            record["action"] = "codex_reauth_already_running"
        else:
            _auth.mark_codex_auth_blocked(auth_state_path, "device_auth_in_progress")
            reauth_state = _auth.start_codex_reauth(
                args.repo_root,
                reauth_state_path,
                chat_id,
                sender_id,
            )
            if reauth_state.get("phase") == "failed":
                reply = f"Could not start Codex sign-in: {reauth_state.get('error') or 'unknown error'}"
                record["action"] = "codex_reauth_start_failed"
                reauth_state["failure_sent_ts"] = int(time.time())
                _state.write_json_object(reauth_state_path, reauth_state)
            elif reauth_state.get("phase") == "awaiting_user":
                reply = _auth.format_codex_reauth_instructions(reauth_state)
                record["action"] = "codex_reauth_code_ready"
                reauth_state["instructions_sent_ts"] = int(time.time())
                _state.write_json_object(reauth_state_path, reauth_state)
            else:
                reply = (
                    "Starting Codex device sign-in. I’ll send the browser link and one-time code "
                    "here as soon as Codex issues them."
                )
                record["action"] = "codex_reauth_started"
            record["reauth_attempt_id"] = reauth_state.get("attempt_id")
    elif command in _settings.CODEX_AUTH_BLOCKED_COMMANDS and auth_failure:
        reply = (
            "Agent sign-in is required. The owner can use /reauth in private."
            if is_group
            else _auth.format_auth_failure_fallback(auth_failure, reauth_state)
        )
        record["action"] = "auth_failure_fallback"
        record["auth_failure_reason"] = auth_failure.get("reason")
        record["relay_result"] = "not relayed: cached Codex authentication failure"
    elif command == "/help":
        from .ui import help_text

        reply = help_text()
        record["action"] = "help"
    elif command == "/ping":
        reply = f"pong from {socket.gethostname()} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        record["action"] = "ping"
    elif command == "/status":
        reply = _status.format_system_status(
            args.session,
            args.target_pane,
            args.tmux_lines,
            args=args,
            auth_failure=auth_failure,
            **({"audience_chat_id": source_chat_id} if is_group else {}),
        )
        record["action"] = "status"
    elif command == "/agent_status":
        pane_command = (
            "codex (supervised)"
            if _processes.codex_target_ready(args.target_pane)
            else (
                _processes.tmux_pane_command(args.target_pane)
                if _processes.tmux_target_exists(args.target_pane)
                else "(missing)"
            )
        )
        meta = agent_registry.active_agent_for_pane(args.target_pane)
        if meta:
            meta = agent_registry.refresh_codex_session_link(
                meta, target_pane=args.target_pane
            )
        auth_detected = ""
        if auth_failure:
            try:
                auth_detected = datetime.fromtimestamp(
                    int(auth_failure.get("detected_ts")), _settings.SGT
                ).strftime(" at %Y-%m-%d %H:%M:%S SGT")
            except (TypeError, ValueError, OSError):
                auth_detected = ""
        try:
            live_usage = _auth.inspect_codex_live_usage(args.repo_root)
            _auth.reconcile_codex_usage_state_from_live_query(
                usage_state_path,
                live_usage,
            )
            live_usage_text = _auth.format_live_codex_limits(live_usage)
            record["codex_live_usage_checked_at"] = live_usage.get("checked_at")
            record["codex_live_status_lines"] = live_usage.get("status_lines")
        except Exception as exc:
            live_usage_text = (
                "Fresh Codex account/rateLimits/read failed; no cached usage value was "
                f"substituted. Detail: {_transport.short_error(exc, env)}"
            )
            record["codex_live_usage_error"] = _transport.short_error(
                exc, env, args.max_log_chars
            )
        reply_lines = [
            f"desired agent state: {_lifecycle.agent_desired_state(args)}",
            f"target pane: {args.target_pane}",
            f"target process: {pane_command}",
            (
                "Codex auth: REAUTH REQUIRED"
                + auth_detected
                + " (refresh credential revoked; run /reauth)"
                if auth_failure
                else "Codex auth: no active failure detected"
            ),
            f"credential storage: {_auth.codex_login_status_summary()} (presence check only)",
            f"reauth flow: {reauth_state.get('phase') or 'idle'}",
            live_usage_text,
        ]
        if meta:
            reply_lines.extend(
                [
                    f"agent id: {meta.get('agent_id')}",
                    f"agent jsonl: {meta.get('agent_jsonl')}",
                    f"codex session: {meta.get('codex_session_path') or '(not detected yet)'}",
                ]
            )
            record["agent_id"] = meta.get("agent_id")
            record["agent_jsonl"] = meta.get("agent_jsonl")
            record["codex_session_path"] = meta.get("codex_session_path")
        reply = "\n".join(reply_lines)
        if reauth_state.get("phase") == "awaiting_user":
            reply += "\n\n" + _auth.format_codex_reauth_instructions(reauth_state)
        record["action"] = "agent_status"
    elif command in {
        "/start_agent",
        "/restart_agent",
    } and _auth.codex_reauth_in_progress(reauth_state):
        reply = (
            "Codex device sign-in is still in progress. Finish it first, then run this "
            "lifecycle command again."
        )
        record["action"] = command.lstrip("/") + "_blocked_by_reauth"
    elif command == "/kill_agent":
        try:
            _lifecycle.set_agent_desired_state(
                args,
                _settings.AGENT_DESIRED_STOPPED,
                "telegram_kill_agent",
            )
            result, meta = _lifecycle.stop_codex_agent(args.session, args.codex_window)
            reply = (
                result
                + "\nThe listener remains online. Use /start_agent to start a new agent."
            )
            record["action"] = "kill_agent"
            record["target_pane"] = args.target_pane
            record["relay_result"] = result
            record["agent_id"] = meta.get("agent_id") if meta else None
        except Exception as exc:
            reply = (
                f"Failed to stop Codex agent safely: {_transport.short_error(exc, env)}"
            )
            record["action"] = "kill_agent_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command in {"/start_agent", "/restart_agent"}:
        try:
            model, reasoning_effort, _settings_explicit = (
                _messages.parse_agent_launch_payload(payload)
            )
            agent_present = _lifecycle.managed_codex_agent_present(args.target_pane)
            if command == "/start_agent" and agent_present:
                raise RuntimeError(
                    "the agent is already running; use /restart_agent to replace it"
                )
            if command == "/restart_agent" and not agent_present:
                raise RuntimeError(
                    "the agent is stopped; use /start_agent instead of /restart_agent"
                )
            _lifecycle.set_agent_desired_state(
                args,
                _settings.AGENT_DESIRED_RUNNING,
                "telegram_start_agent"
                if command == "/start_agent"
                else "telegram_restart_agent",
            )
            _replies.send(
                args,
                token,
                source_chat_id,
                (
                    f"Starting Codex agent in tmux window {args.codex_window} "
                    f"with model={model}, reasoning={reasoning_effort}..."
                )
                if command == "/start_agent"
                else (
                    f"Restarting Codex agent in tmux window {args.codex_window} "
                    f"with model={model}, reasoning={reasoning_effort}..."
                ),
            )
            target_pane, result, meta = _lifecycle.start_codex_agent(
                repo_root=args.repo_root,
                session=args.session,
                window=args.codex_window,
                restart=command == "/restart_agent",
                model=model,
                reasoning_effort=reasoning_effort,
            )
            args.target_pane = target_pane
            reply = (
                result + "\nNormal Telegram text will now be relayed to this target."
            )
            record["action"] = command.lstrip("/")
            record["target_pane"] = target_pane
            record["relay_result"] = result
            record["agent_model"] = model
            record["agent_reasoning_effort"] = reasoning_effort
            if meta:
                reply += (
                    f"\nAgent id: {meta.get('agent_id')}"
                    f"\nAgent JSONL: {meta.get('agent_jsonl')}"
                    f"\nCodex session: {meta.get('codex_session_path') or '(not detected yet)'}"
                )
                record["agent_id"] = meta.get("agent_id")
                record["agent_jsonl"] = meta.get("agent_jsonl")
                record["codex_session_path"] = meta.get("codex_session_path")
                agent_registry.append_agent_event(
                    meta,
                    {
                        "event": "telegram_control_command",
                        "command": command,
                        "message_id": message.get("message_id"),
                        "relay_result": result,
                        "sender": _messages.sender_label(message),
                        "update_id": update.get("update_id"),
                    },
                )
            if command == "/restart_agent" and meta:
                _usage.clear_codex_usage_depletion(
                    usage_state_path, "manual_agent_restart"
                )
                _auth.clear_codex_auth_failure(auth_state_path, "manual_agent_restart")
        except Exception as exc:
            verb = "start" if command == "/start_agent" else "restart"
            reply = (
                _transport.short_error(exc, env)
                if isinstance(exc, _lifecycle.AgentStartUnconfirmed)
                else f"Failed to {verb} Codex agent: {_transport.short_error(exc, env)}"
            )
            record["action"] = command.lstrip("/") + "_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/model":
        try:
            model, reasoning_effort = _models.parse_live_model_payload(payload)
            actual_model, actual_effort = _models.set_codex_model(
                args.target_pane,
                model,
                reasoning_effort,
            )
            reply = (
                f"Codex model switched to {actual_model} with reasoning={actual_effort}; "
                "current chat preserved."
            )
            record["action"] = "model"
            record["target_pane"] = args.target_pane
            record["agent_model"] = actual_model
            record["agent_reasoning_effort"] = actual_effort
            meta = agent_registry.active_agent_for_pane(args.target_pane)
            if meta:
                record["agent_id"] = meta.get("agent_id")
                agent_registry.append_agent_event(
                    meta,
                    {
                        "event": "telegram_model_changed",
                        "message_id": message.get("message_id"),
                        "model": actual_model,
                        "reasoning_effort": actual_effort,
                        "sender": _messages.sender_label(message),
                        "update_id": update.get("update_id"),
                    },
                )
        except Exception as exc:
            reply = f"Model change not confirmed: {_transport.short_error(exc, env)}"
            record["action"] = "model_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/reasoning":
        try:
            reasoning_effort = _models.parse_live_reasoning_effort(payload)
            actual_effort = _models.set_codex_reasoning_effort(
                args.target_pane, reasoning_effort
            )
            reply = f"Codex reasoning switched to {actual_effort} in the current chat; session preserved."
            record["action"] = "reasoning"
            record["target_pane"] = args.target_pane
            record["agent_reasoning_effort"] = actual_effort
            meta = agent_registry.active_agent_for_pane(args.target_pane)
            if meta:
                record["agent_id"] = meta.get("agent_id")
                agent_registry.append_agent_event(
                    meta,
                    {
                        "event": "telegram_reasoning_changed",
                        "message_id": message.get("message_id"),
                        "reasoning_effort": actual_effort,
                        "sender": _messages.sender_label(message),
                        "update_id": update.get("update_id"),
                    },
                )
        except Exception as exc:
            reply = (
                f"Reasoning change not confirmed: {_transport.short_error(exc, env)}"
            )
            record["action"] = "reasoning_failed"
            record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/interrupt":
        interrupt_text = payload.strip()
        if not interrupt_text:
            reply = "Usage: /interrupt PROMPT"
            record["action"] = "interrupt_empty"
        else:
            try:
                relay_text = _messages.format_agent_message(
                    message,
                    interrupt_text,
                    route_id=route_id,
                )
                pending_path = (
                    Path(args.relay_confirmation_state_path)
                    if getattr(args, "relay_confirmation_state_path", None)
                    else None
                )
                result, interrupt_state = _submission.interrupt_codex_with_prompt(
                    args.target_pane,
                    relay_text,
                    submit_delay=args.submit_delay,
                    pending_state_path=pending_path,
                )
                record["action"] = "interrupt"
                record["relay_mode"] = args.relay_mode
                record["relay_result"] = result
                record["target_pane"] = args.target_pane
                record.update(interrupt_state)
                meta = agent_registry.active_agent_for_pane(args.target_pane)
                if meta:
                    record["agent_id"] = meta.get("agent_id")
                    agent_registry.append_agent_event(
                        meta,
                        {
                            "event": "telegram_interrupt",
                            "message_id": message.get("message_id"),
                            "relay_result": result,
                            **interrupt_state,
                        },
                    )
                reply = None if result.startswith("relayed to ") else result
            except Exception as exc:
                reply = f"Could not interrupt Codex: {_transport.short_error(exc, env)}"
                record["action"] = "interrupt_failed"
                record["error"] = _transport.short_error(exc, env, args.max_log_chars)
    elif command == "/resume_goal":
        reply = _processes.relay_codex_control(
            args.target_pane, "/goal resume", args.submit_delay
        )
        record["action"] = "resume_goal"
        record["relay_mode"] = args.relay_mode
        record["target_pane"] = args.target_pane
        record["relay_result"] = reply
        if reply.startswith("relayed to "):
            reply = "Sent /goal resume to Codex. Now send normal Telegram text with the next instruction."
    elif command == "/codex":
        codex_text = payload.strip()
        if not codex_text:
            reply = "Usage: /codex /goal resume"
            record["action"] = "codex_empty"
        else:
            if not codex_text.startswith("/"):
                codex_text = "/" + codex_text
            reply = _processes.relay_codex_control(
                args.target_pane, codex_text, args.submit_delay
            )
            record["action"] = "codex_control"
            record["relay_mode"] = args.relay_mode
            record["target_pane"] = args.target_pane
            record["relay_text"] = codex_text
            record["relay_result"] = reply
            if reply.startswith("relayed to "):
                reply = f"Sent Codex command: {codex_text}"
    elif command == "/replay_messages":
        try:
            message_ids = _messages.parse_message_ids(payload)
            if not message_ids:
                raise ValueError("missing message id")
            records = _messages.load_agent_records_by_message_id(
                chat_record_log_path,
                message_ids,
            )
            relay_text = _messages.format_replayed_messages(
                message,
                records,
                chat_record_log_path,
                route_id=route_id,
            )
            reply = _submission.paste_to_tmux(
                args.target_pane,
                relay_text,
                press_enter=True,
                allow_shell_pane=args.allow_shell_pane,
                submit_delay=args.submit_delay,
                pending_state_path=Path(args.relay_confirmation_state_path)
                if getattr(args, "relay_confirmation_state_path", None)
                else None,
            )
            record["action"] = "replay_messages"
            record["message_ids"] = message_ids
            record["relay_mode"] = args.relay_mode
            record["target_pane"] = args.target_pane
            record["relay_result"] = reply
            if reply.startswith("relayed to "):
                reply = "Replayed Telegram message id(s) to Codex: " + ", ".join(
                    str(i) for i in message_ids
                )
        except Exception as exc:
            reply = f"Failed to replay messages: {exc}"
            record["action"] = "replay_messages_failed"
            record["error"] = str(exc)
    elif command in {"/replay_last", "/replay_last_long"}:
        try:
            count = _messages.parse_count(payload, default=2, maximum=10)
            min_chars = 1000 if command == "/replay_last_long" else 0
            records = _messages.load_recent_agent_records(
                chat_record_log_path,
                count,
                min_chars=min_chars,
            )
            relay_text = _messages.format_replayed_messages(
                message,
                records,
                chat_record_log_path,
                route_id=route_id,
            )
            reply = _submission.paste_to_tmux(
                args.target_pane,
                relay_text,
                press_enter=True,
                allow_shell_pane=args.allow_shell_pane,
                submit_delay=args.submit_delay,
                pending_state_path=Path(args.relay_confirmation_state_path)
                if getattr(args, "relay_confirmation_state_path", None)
                else None,
            )
            message_ids = [record.get("message_id") for record in records]
            record["action"] = command.lstrip("/")
            record["message_ids"] = message_ids
            record["relay_mode"] = args.relay_mode
            record["target_pane"] = args.target_pane
            record["relay_result"] = reply
            if reply.startswith("relayed to "):
                reply = "Replayed recent Telegram message id(s) to Codex: " + ", ".join(
                    str(message_id) for message_id in message_ids
                )
        except Exception as exc:
            reply = f"Failed to replay recent messages: {exc}"
            record["action"] = command.lstrip("/") + "_failed"
            record["error"] = str(exc)
    elif command == "/recent_messages":
        try:
            count = _messages.parse_count(payload, default=8, maximum=20)
            reply = _messages.recent_messages_text(chat_record_log_path, count)
            record["action"] = "recent_messages"
            record["count"] = count
        except Exception as exc:
            reply = f"Failed to list recent messages: {exc}"
            record["action"] = "recent_messages_failed"
            record["error"] = str(exc)
    elif command == "/agent" or command == "(agent-message)":
        agent_text = payload if command == "/agent" else text
        document_error = False
        voice_error = False
        photo_error = False
        if document is not None:
            try:
                received = _attachments.download_telegram_document(
                    token,
                    document,
                    Path(
                        getattr(
                            args,
                            "inbound_documents_dir",
                            _settings.DEFAULT_INBOUND_DOCUMENTS_DIR,
                        )
                    ),
                    message.get("message_id"),
                    max_bytes=int(
                        getattr(
                            args,
                            "max_inbound_document_bytes",
                            _settings.DEFAULT_MAX_INBOUND_DOCUMENT_BYTES,
                        )
                    ),
                )
                record["document"] = {
                    **received,
                    "original_name": redact(str(received["original_name"]), env),
                }
                agent_text = _attachments.format_inbound_document_text(received, text)
            except Exception as exc:
                reply = (
                    f"Could not receive document: {_transport.short_error(exc, env)}"
                )
                record["action"] = "document_rejected"
                record["error"] = _transport.short_error(exc, env, args.max_log_chars)
                document_error = True
        if voice is not None and not document_error:
            try:
                received = _attachments.download_telegram_voice(
                    token,
                    voice,
                    Path(
                        getattr(
                            args,
                            "inbound_voice_dir",
                            str(_settings.DEFAULT_INBOUND_VOICE_DIR),
                        )
                    ),
                    message.get("message_id"),
                    max_bytes=int(
                        getattr(
                            args,
                            "max_inbound_voice_bytes",
                            _settings.DEFAULT_MAX_INBOUND_VOICE_BYTES,
                        )
                    ),
                )
                transcript = _attachments.transcribe_voice_ogg(
                    Path(received["path"]),
                    Path(
                        getattr(args, "whisper_bin", str(_settings.DEFAULT_WHISPER_BIN))
                    ),
                    Path(
                        getattr(
                            args, "whisper_model", str(_settings.DEFAULT_WHISPER_MODEL)
                        )
                    ),
                    Path(
                        getattr(args, "opusdec_bin", str(_settings.DEFAULT_OPUSDEC_BIN))
                    ),
                    Path(
                        getattr(
                            args,
                            "opusdec_lib_dir",
                            str(_settings.DEFAULT_OPUSDEC_LIB_DIR),
                        )
                    ),
                )
                record["voice"] = {**received, "transcript": transcript}
                agent_text = _attachments.format_voice_text(transcript, text)
            except Exception as exc:
                reply = f"Voice note not relayed: {_transport.short_error(exc, env)}"
                record["action"] = "voice_failed"
                record["error"] = _transport.short_error(exc, env, args.max_log_chars)
                voice_error = True
        if photo is not None and not document_error and not voice_error:
            try:
                received = _attachments.download_telegram_photo(
                    token,
                    photo,
                    Path(
                        getattr(
                            args,
                            "inbound_photo_dir",
                            str(_settings.DEFAULT_INBOUND_PHOTO_DIR),
                        )
                    ),
                    message.get("message_id"),
                    max_bytes=int(
                        getattr(
                            args,
                            "max_inbound_photo_bytes",
                            _settings.DEFAULT_MAX_INBOUND_PHOTO_BYTES,
                        )
                    ),
                )
                record["photo"] = {
                    **received,
                    **record.get("photo", {}),
                }
                agent_text = _attachments.format_inbound_photo_text(received, text)
            except Exception as exc:
                reply = f"Could not receive photo: {_transport.short_error(exc, env)}"
                record["action"] = "photo_rejected"
                record["error"] = _transport.short_error(exc, env, args.max_log_chars)
                photo_error = True
        if document_error or voice_error or photo_error:
            pass
        elif not agent_text:
            reply = "Send normal text for the agent, or use /status, /ping, or /help."
            record["action"] = "agent_empty"
        else:
            reply = _lifecycle.ensure_codex_target_for_agent_message(args, record)
            if reply is None:
                memory_state_path = _identity.people_memory_state_path(args, log_path)
                person_context = (
                    ""
                    if is_group
                    else _identity.telegram_person_context(memory_state_path, message)
                )
                relay_text = _messages.format_agent_message(
                    message,
                    agent_text,
                    person_context=person_context,
                    route_id=route_id,
                )
                if is_group:
                    relay_text = (
                        relay_text
                        + "\n\n[This came from a Telegram group. Keep your final reply concise.]"
                    )
                if args.relay_mode == "log":
                    reply = f"logged for agent: {log_path}"
                elif args.relay_mode == "tmux-paste":
                    reply = _submission.paste_to_tmux(
                        args.target_pane,
                        relay_text,
                        press_enter=False,
                        allow_shell_pane=True,
                        submit_delay=args.submit_delay,
                    )
                else:
                    reply = _submission.paste_to_tmux(
                        args.target_pane,
                        relay_text,
                        press_enter=True,
                        allow_shell_pane=args.allow_shell_pane,
                        submit_delay=args.submit_delay,
                        pending_state_path=Path(args.relay_confirmation_state_path)
                        if getattr(args, "relay_confirmation_state_path", None)
                        else None,
                    )
            if voice is not None:
                record["action"] = "agent_voice"
            elif document is not None:
                record["action"] = "agent_document"
            elif photo is not None:
                record["action"] = "agent_photo"
            else:
                record["action"] = "agent"
            record["relay_mode"] = args.relay_mode
            record["target_pane"] = args.target_pane
            record["relay_result"] = reply
            meta = agent_registry.active_agent_for_pane(args.target_pane)
            if meta:
                meta = agent_registry.refresh_codex_session_link(
                    meta, target_pane=args.target_pane
                )
                record["agent_id"] = meta.get("agent_id")
                record["agent_jsonl"] = meta.get("agent_jsonl")
                record["codex_session_path"] = meta.get("codex_session_path")
                agent_registry.append_agent_event(
                    meta,
                    {
                        "command": command,
                        "event": "telegram_message_relay",
                        "message_id": message.get("message_id"),
                        "chat_id": source_chat_id,
                        "chat_type": str(chat.get("type") or ""),
                        "group_owner": bool(group_owner),
                        "relay_mode": args.relay_mode,
                        "relay_result": reply,
                        "sender": _messages.sender_label(message),
                        "text": redact(agent_text, env)[: args.max_log_chars],
                        "update_id": update.get("update_id"),
                    },
                )
            if reply.startswith("relayed to "):
                memory_text = text
                if voice is not None:
                    memory_text = str(
                        (record.get("voice") or {}).get("transcript") or text
                    )
                record["person_memory_updated"] = _identity.remember_telegram_person(
                    _identity.people_memory_state_path(args, log_path),
                    message,
                    memory_text,
                    env,
                )
                if args.bridge_ack:
                    try:
                        _replies.send(
                            args,
                            token,
                            source_chat_id,
                            "BRIDGE DEBUG: delivered to Codex.",
                        )
                        record["bridge_ack"] = True
                    except Exception as exc:
                        record["bridge_ack"] = False
                        record["bridge_ack_error"] = str(exc)
                reply = None
    elif not command:
        reply = "Empty message. Send normal text for the agent, or use /status, /ping, or /help."
        record["action"] = "empty"
    else:
        reply = "Unknown command. Send normal text for the agent, or use /status, /ping, or /help."
        record["action"] = "unknown"

    relay_result = record.get("relay_result")
    if (
        route_state_path is not None
        and isinstance(relay_result, str)
        and relay_result.startswith("relayed to ")
        and route_id
    ):
        # Register every prompt-bearing command, not only ordinary text. This
        # covers /interrupt and replay commands as well as normal messages.
        _routing.set_reply_route_chat_id(
            route_state_path,
            source_chat_id,
            is_group=is_group,
            route_id=route_id,
            message_thread_id=message.get("message_thread_id"),
            source_message_id=message.get("message_id"),
        )
        record["reply_route_id"] = route_id

    _state.append_jsonl(chat_record_log_path, record)
    if reply_chunks:
        for reply_chunk in reply_chunks:
            _replies.send(
                args,
                token,
                source_chat_id,
                reply_chunk,
                message_thread_id=message.get("message_thread_id"),
            )
    elif reply:
        _replies.send(
            args,
            token,
            source_chat_id,
            reply,
            message_thread_id=message.get("message_thread_id"),
        )
    return record
