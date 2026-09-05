"""App services for the Telegram relay."""

from __future__ import annotations


import argparse
import notify as _notify
import fcntl
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402
from notify import assert_safe_local_path, env_value

from . import settings as _settings
from . import service as _service
from . import ui as _ui
from . import routing as _routing
from . import auth as _auth
from . import delivery as _delivery
from . import identity as _identity
from . import lifecycle as _lifecycle
from . import models as _models
from . import processes as _processes
from . import queue as _queue
from . import schedule as _schedule
from . import sessions as _sessions
from . import state as _state
from . import submission as _submission
from . import transport as _transport
from . import usage as _usage


def get_updates(
    token: str, offset: int | None, timeout: int, limit: int
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": timeout,
        "limit": limit,
        "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    return _transport.telegram_api(token, "getUpdates", params, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll Telegram and relay safe operator messages to tmux."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--secret-env",
        type=Path,
        default=os.environ.get("TELEAGENT_SECRET_ENV"),
    )
    parser.add_argument(
        "--session", default=os.environ.get("TELEAGENT_TMUX_SESSION", "tele-agent")
    )
    parser.add_argument(
        "--target-pane",
        default=os.environ.get("TELEAGENT_INBOX_TARGET", "tele-agent:0.0"),
    )
    parser.add_argument(
        "--codex-window", default=os.environ.get("TELEAGENT_CODEX_WINDOW", "codex")
    )
    parser.add_argument(
        "--relay-mode",
        choices=["log", "tmux-paste", "tmux-enter"],
        default="tmux-enter",
    )
    parser.add_argument(
        "--allow-shell-pane",
        action="store_true",
        help="allow /agent to press Enter in shell panes",
    )
    parser.add_argument("--poll-timeout", type=int, default=5)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--tmux-lines", type=int, default=60)
    parser.add_argument("--max-log-chars", type=int, default=4000)
    parser.add_argument("--submit-delay", type=float, default=2.0)
    bridge_ack_group = parser.add_mutually_exclusive_group()
    bridge_ack_group.add_argument(
        "--bridge-ack",
        dest="bridge_ack",
        action="store_true",
        help="send an immediate generic listener ACK after successful normal-message relays",
    )
    bridge_ack_group.add_argument(
        "--no-bridge-ack",
        dest="bridge_ack",
        action="store_false",
        help="do not send immediate listener ACKs for successful normal-message relays",
    )
    parser.set_defaults(bridge_ack=False)
    parser.add_argument(
        "--state-file",
        default=str(_settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_inbox.offset"),
    )
    parser.add_argument(
        "--log-jsonl",
        default=str(_settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_inbox.jsonl"),
    )
    parser.add_argument(
        "--inbound-documents-dir",
        default=str(_settings.DEFAULT_INBOUND_DOCUMENTS_DIR),
        help="private local directory for accepted Telegram PDF/TXT/MD/HTML documents",
    )
    parser.add_argument(
        "--max-inbound-document-bytes",
        type=int,
        default=_settings.DEFAULT_MAX_INBOUND_DOCUMENT_BYTES,
        help="maximum inbound document size; Telegram's hosted Bot API caps downloads at 20 MiB",
    )
    parser.add_argument(
        "--inbound-voice-dir",
        default=str(_settings.DEFAULT_INBOUND_VOICE_DIR),
        help="private local directory for downloaded Telegram voice notes",
    )
    parser.add_argument(
        "--max-inbound-voice-bytes",
        type=int,
        default=_settings.DEFAULT_MAX_INBOUND_VOICE_BYTES,
        help="maximum inbound voice note size",
    )
    parser.add_argument(
        "--inbound-photo-dir",
        default=str(_settings.DEFAULT_INBOUND_PHOTO_DIR),
        help="private local directory for downloaded Telegram photos",
    )
    parser.add_argument(
        "--max-inbound-photo-bytes",
        type=int,
        default=_settings.DEFAULT_MAX_INBOUND_PHOTO_BYTES,
        help="maximum inbound photo size",
    )
    parser.add_argument(
        "--whisper-bin",
        default=str(_settings.DEFAULT_WHISPER_BIN),
        help="path to the whisper-cli binary for local transcription",
    )
    parser.add_argument(
        "--whisper-model",
        default=str(_settings.DEFAULT_WHISPER_MODEL),
        help="path to a whisper ggml model file",
    )
    parser.add_argument(
        "--opusdec-bin",
        default=str(_settings.DEFAULT_OPUSDEC_BIN),
        help="path to opusdec for decoding Telegram voice notes",
    )
    parser.add_argument(
        "--opusdec-lib-dir",
        default=str(_settings.DEFAULT_OPUSDEC_LIB_DIR),
        help="library directory containing opusdec dependencies",
    )
    parser.add_argument(
        "--agent-outbox",
        default=str(_settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_agent_outbox.jsonl"),
    )
    parser.add_argument(
        "--agent-outbox-offset",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_agent_outbox.offset"
        ),
    )
    parser.add_argument(
        "--max-agent-ack-age",
        type=int,
        default=120,
        help="skip queued agent ACK replies older than this many seconds; use -1 to disable",
    )
    parser.add_argument(
        "--max-agent-progress-age",
        type=int,
        default=300,
        help="skip queued agent PROGRESS replies older than this many seconds; use -1 to disable",
    )
    parser.add_argument(
        "--agent-message-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_agent_messages.state.json"
        ),
        help="persistent byte offset for automatic Codex agent_message forwarding",
    )
    parser.add_argument(
        "--codex-usage-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_codex_usage.state.json"
        ),
        help="structured Codex limit-event audit/notification state; never used as a relay gate",
    )
    parser.add_argument(
        "--codex-auth-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_codex_auth.state.json"
        ),
        help="persistent revoked-credential marker maintained from structured session events",
    )
    parser.add_argument(
        "--codex-reauth-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_codex_reauth.state.json"
        ),
        help="persistent progress for the Telegram-triggered Codex device login",
    )
    parser.add_argument(
        "--codex-reset-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_codex_reset.state.json"
        ),
        help="persistent two-step confirmation state for manual banked Codex resets",
    )
    parser.add_argument(
        "--agent-lifecycle-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_agent_lifecycle.state.json"
        ),
        help="persistent operator-requested running/stopped state for the managed Codex agent",
    )
    parser.add_argument(
        "--relay-confirmation-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR
            / "telegram_relay_confirmation.state.json"
        ),
        help="persistent pending confirmations for Telegram messages submitted to Codex",
    )
    parser.add_argument(
        "--relay-queue-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_relay_queue.state.json"
        ),
        help="persistent FIFO for normal Telegram messages waiting on a safe composer",
    )
    parser.add_argument(
        "--reply-route-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_reply_route.state.json"
        ),
        help="persistent chat routing used when a group message starts an agent turn",
    )
    parser.add_argument(
        "--people-memory-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_people_memory.state.json"
        ),
        help="bounded per-Telegram-user identity and conversation continuity state",
    )
    parser.add_argument(
        "--timed-message-state",
        default=str(
            _settings.DEFAULT_TELEGRAM_LOG_DIR / "telegram_timed_messages.state.json"
        ),
        help="persistent schedule and delivery state for /timed messages",
    )
    parser.add_argument(
        "--max-agent-message-chars",
        type=int,
        default=1200,
        help="maximum characters for each forwarded non-final agent_message",
    )
    parser.add_argument(
        "--max-final-message-chars",
        type=int,
        default=3600,
        help="maximum characters for the forwarded final answer, including the trailing ∎ marker",
    )
    parser.add_argument(
        "--max-group-final-message-chars",
        type=int,
        default=900,
        help="maximum characters for final answers posted to a Telegram group",
    )
    watchdog_group = parser.add_mutually_exclusive_group()
    watchdog_group.add_argument(
        "--agent-watchdog",
        dest="agent_watchdog",
        action="store_true",
        help="proactively recreate the managed Codex agent when it is missing",
    )
    watchdog_group.add_argument(
        "--no-agent-watchdog",
        dest="agent_watchdog",
        action="store_false",
        help="disable proactive managed-agent recovery",
    )
    parser.set_defaults(agent_watchdog=True)
    parser.add_argument(
        "--agent-recovery-wait",
        type=float,
        default=75.0,
        help="wait this many seconds for a supervised Codex restart before rejecting a message",
    )
    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="process old pending Telegram updates",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    args.repo_root = repo_root
    assert_safe_local_path(repo_root)
    env = _notify.load_env(repo_root, args.secret_env)
    token = env_value(env, "TELEAGENT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id = env_value(env, "TELEAGENT_CHAT_ID", "TELEGRAM_CHAT_ID")
    if chat_id and str(chat_id).startswith("-"):
        raise SystemExit(
            "Configure the owner’s private chat as TELEAGENT_CHAT_ID; groups use per-request routes."
        )
    if not token or not chat_id:
        raise SystemExit(
            "Telegram is not configured. Run scripts/setup_telegram_notify.py first."
        )
    owner_user_id = str(
        env_value(env, "TELEAGENT_OWNER_USER_ID", "TELEAGENT_OWNER_ID") or chat_id
    ).strip()
    bot_username = (
        str(env_value(env, "TELEAGENT_BOT_USERNAME", "TELEGRAM_BOT_USERNAME") or "")
        .strip()
        .lstrip("@")
    )
    group_member_cache: dict[str, tuple[float, bool]] = {}
    route_state_path = _state.state_path(repo_root, args.reply_route_state)
    args.reply_route_state_path = str(route_state_path)
    people_state_path = _state.state_path(repo_root, args.people_memory_state)
    args.people_memory_state_path = str(people_state_path)

    offset_path = _state.state_path(repo_root, args.state_file)
    log_path = _state.state_path(repo_root, args.log_jsonl)

    def refresh_bot_identity():
        nonlocal owner_user_id, bot_username
        if bot_username:
            return
        try:
            owner_user_id, bot_username = _routing.resolve_bot_identity(
                token, chat_id, owner_user_id, bot_username
            )
            _state.append_jsonl(
                log_path, {"ts": int(time.time()), "event": "bot_identity_discovered"}
            )
        except Exception as exc:
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "bot_identity_discovery_failed",
                    "error": _transport.short_error(exc, env),
                },
            )

    refresh_bot_identity()
    _identity.backfill_per_chat_inbox_records(log_path)
    inbound_documents_dir = _state.state_path(repo_root, args.inbound_documents_dir)
    args.inbound_documents_dir = str(inbound_documents_dir)
    if args.max_inbound_document_bytes <= 0:
        raise SystemExit("--max-inbound-document-bytes must be positive")
    inbound_voice_dir = _state.state_path(repo_root, args.inbound_voice_dir)
    args.inbound_voice_dir = str(inbound_voice_dir)
    if args.max_inbound_voice_bytes <= 0:
        raise SystemExit("--max-inbound-voice-bytes must be positive")
    inbound_photo_dir = _state.state_path(repo_root, args.inbound_photo_dir)
    args.inbound_photo_dir = str(inbound_photo_dir)
    if args.max_inbound_photo_bytes <= 0:
        raise SystemExit("--max-inbound-photo-bytes must be positive")
    args.whisper_bin = str(Path(args.whisper_bin).expanduser())
    args.whisper_model = str(Path(args.whisper_model).expanduser())
    args.opusdec_bin = str(Path(args.opusdec_bin).expanduser())
    args.opusdec_lib_dir = str(Path(args.opusdec_lib_dir).expanduser())
    agent_outbox_path = _state.state_path(repo_root, args.agent_outbox)
    agent_outbox_offset_path = _state.state_path(repo_root, args.agent_outbox_offset)
    agent_message_state_path = _state.state_path(repo_root, args.agent_message_state)
    args.agent_message_state_path = str(agent_message_state_path)
    args.ingress_state_path = str(offset_path.with_name("telegram_ingress.state.json"))
    args.health_state_path = str(offset_path.with_name("telegram_health.state.json"))
    codex_usage_state_path = _state.state_path(repo_root, args.codex_usage_state)
    args.codex_usage_state_path = str(codex_usage_state_path)
    codex_auth_state_path = _state.state_path(repo_root, args.codex_auth_state)
    args.codex_auth_state_path = str(codex_auth_state_path)
    codex_reauth_state_path = _state.state_path(repo_root, args.codex_reauth_state)
    args.codex_reauth_state_path = str(codex_reauth_state_path)
    codex_reset_state_path = _state.state_path(repo_root, args.codex_reset_state)
    args.codex_reset_state_path = str(codex_reset_state_path)
    lifecycle_state_path = _state.state_path(repo_root, args.agent_lifecycle_state)
    args.agent_lifecycle_state_path = str(lifecycle_state_path)
    relay_confirmation_state_path = _state.state_path(
        repo_root, args.relay_confirmation_state
    )
    args.relay_confirmation_state_path = str(relay_confirmation_state_path)
    relay_queue_path = _state.state_path(repo_root, args.relay_queue_state)
    args.relay_queue_state_path = str(relay_queue_path)
    timed_message_state_path = _state.state_path(repo_root, args.timed_message_state)
    args.timed_message_state_path = str(timed_message_state_path)
    # One poller for each bot runtime, even if two launchers race.
    listener_lock = offset_path.with_name("telegram_listener.lock").open("a")
    os.chmod(listener_lock.name, 0o600)
    try:
        fcntl.flock(listener_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A listener already owns this bot runtime.") from None
    offset = _state.read_offset(offset_path)
    active_meta = agent_registry.active_agent_for_pane(args.target_pane)
    if active_meta:
        active_meta = agent_registry.refresh_codex_session_link(
            active_meta, target_pane=args.target_pane
        )

    if offset is None and not args.process_existing:
        while True:
            try:
                existing = get_updates(token, None, timeout=0, limit=100)
            except _transport.TransientTelegramError as exc:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "initial_get_updates_transient_failure",
                        "error_type": type(exc).__name__,
                        "error": _transport.short_error(exc, env, args.max_log_chars),
                    },
                )
                if args.once:
                    return 1
                time.sleep(max(args.poll_interval, 5.0))
                continue
            if existing:
                offset = max(int(update["update_id"]) for update in existing) + 1
                _state.write_offset(offset_path, offset)
            break

    _state.append_jsonl(
        log_path,
        {
            "ts": int(time.time()),
            "event": "listener_started",
            "host": socket.gethostname(),
            "session": args.session,
            "target_pane": args.target_pane,
            "relay_mode": args.relay_mode,
            "allow_shell_pane": args.allow_shell_pane,
            "process_existing": args.process_existing,
            "agent_id": active_meta.get("agent_id") if active_meta else None,
            "agent_jsonl": active_meta.get("agent_jsonl") if active_meta else None,
            "codex_session_path": active_meta.get("codex_session_path")
            if active_meta
            else None,
            "codex_usage_state_path": str(codex_usage_state_path),
            "codex_auth_state_path": str(codex_auth_state_path),
            "codex_reauth_state_path": str(codex_reauth_state_path),
            "codex_reset_state_path": str(codex_reset_state_path),
            "agent_lifecycle_state_path": str(lifecycle_state_path),
            "agent_desired_state": _lifecycle.agent_desired_state(args),
            "relay_confirmation_state_path": str(relay_confirmation_state_path),
            "relay_queue_state_path": str(relay_queue_path),
            "people_memory_state_path": str(people_state_path),
            "people_memory_enabled": _identity.people_memory_enabled(),
            "timed_message_state_path": str(timed_message_state_path),
            "inbound_documents_dir": str(inbound_documents_dir),
            "max_inbound_document_bytes": min(
                args.max_inbound_document_bytes,
                _settings.TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES,
            ),
            "inbound_voice_dir": str(inbound_voice_dir),
            "whisper_bin": args.whisper_bin,
            "whisper_model": args.whisper_model,
            "opusdec_bin": args.opusdec_bin,
            "opusdec_lib_dir": args.opusdec_lib_dir,
            "max_inbound_voice_bytes": min(
                args.max_inbound_voice_bytes,
                _settings.TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES,
            ),
            "inbound_photo_dir": str(inbound_photo_dir),
            "max_inbound_photo_bytes": min(
                args.max_inbound_photo_bytes,
                _settings.TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES,
            ),
        },
    )
    if active_meta:
        agent_registry.append_agent_event(
            active_meta,
            {
                "event": "listener_started",
                "relay_mode": args.relay_mode,
                "session": args.session,
                "target_pane": args.target_pane,
            },
        )
    print(
        f"Telegram inbox listening for chat {chat_id} target={args.target_pane} "
        f"mode={args.relay_mode}"
        + (
            f" group_mode=owner:{owner_user_id} bot:@{bot_username}"
            if owner_user_id and bot_username
            else ""
        ),
        flush=True,
    )

    def drain_current_codex_messages() -> None:
        if not _processes.codex_target_ready(args.target_pane):
            return
        meta = agent_registry.active_agent_for_pane(args.target_pane)
        if not meta:
            return
        meta = agent_registry.refresh_codex_session_link(
            meta, target_pane=args.target_pane
        )
        drain_state = _state.read_json_object(agent_message_state_path)
        try:
            anchor_age = time.time() - float(drain_state.get("anchor_ts") or 0)
        except (TypeError, ValueError):
            anchor_age = float("inf")
        anchored_path = drain_state.get("session_path")
        if (
            anchor_age <= 3600
            and isinstance(anchored_path, str)
            and anchored_path
            and _sessions.valid_codex_session_for_agent(meta, Path(anchored_path))
        ):
            meta["codex_session_path"] = anchored_path
        _delivery.drain_codex_agent_messages(
            token,
            chat_id,
            meta,
            agent_message_state_path,
            log_path,
            env,
            max_commentary_chars=args.max_agent_message_chars,
            max_final_chars=args.max_final_message_chars,
            route_state_path=route_state_path,
            # A missing/expired binding must fail closed to the owner private
            # chat; the immutable marker in the Codex session selects groups.
            is_group_route=False,
            max_group_final_chars=args.max_group_final_message_chars,
        )

    def refresh_current_codex_usage() -> dict[str, Any]:
        meta = agent_registry.active_agent_for_pane(args.target_pane)
        if not meta:
            return _state.read_json_object(codex_usage_state_path)
        # Scan the registered session before refreshing its process link. If
        # the supervisor has just restarted Codex after a quota failure, this
        # closes the race where switching to the new rollout could skip the
        # final structured error in the old rollout.
        state = _usage.refresh_codex_usage_state(meta, codex_usage_state_path)
        refreshed_meta = agent_registry.refresh_codex_session_link(
            meta, target_pane=args.target_pane
        )
        if refreshed_meta.get("codex_session_path") != meta.get("codex_session_path"):
            state = _usage.refresh_codex_usage_state(
                refreshed_meta, codex_usage_state_path
            )
        return state

    def maintain_current_codex_usage() -> dict[str, Any]:
        state = refresh_current_codex_usage()
        _usage.notify_codex_usage_failure(
            token,
            chat_id,
            codex_usage_state_path,
            log_path,
            env,
            args.max_log_chars,
        )
        return state

    def refresh_current_codex_auth() -> dict[str, Any]:
        meta = agent_registry.active_agent_for_pane(args.target_pane)
        if not meta:
            return _state.read_json_object(codex_auth_state_path)
        # Scan the old rollout before accepting a newly linked process, for
        # the same supervisor-restart race handled by usage tracking above.
        state = _auth.refresh_codex_auth_state(meta, codex_auth_state_path)
        refreshed_meta = agent_registry.refresh_codex_session_link(
            meta, target_pane=args.target_pane
        )
        if refreshed_meta.get("codex_session_path") != meta.get("codex_session_path"):
            state = _auth.refresh_codex_auth_state(
                refreshed_meta, codex_auth_state_path
            )
        return state

    def notify_current_codex_auth_failure() -> None:
        state = _state.read_json_object(codex_auth_state_path)
        if not state.get("blocked") or not state.get("alert_pending"):
            return
        try:
            _transport.send_reply(
                token,
                chat_id,
                "**Codex authentication failed**\n\n"
                "The agent’s refresh credential was revoked, so its last task could not run. "
                "The Telegram listener is still alive. New agent messages will be rejected "
                "mechanically rather than disappearing.\n\n"
                "Run `/reauth` to sign in here. `/agent_status` now includes the auth and "
                "recovery state.",
            )
        except Exception as exc:
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "codex_auth_failure_notice_failed",
                    "error": _transport.short_error(exc, env, args.max_log_chars),
                },
            )
            return
        state.update(
            {
                "alert_pending": False,
                "alert_sent_ts": int(time.time()),
                "updated_ts": int(time.time()),
            }
        )
        _state.write_json_object(codex_auth_state_path, state)
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "codex_auth_failure_notice_sent",
                "reason": state.get("reason"),
                "agent_id": state.get("agent_id"),
            },
        )

    def reconcile_current_codex_reauth() -> None:
        state = _state.read_json_object(codex_reauth_state_path)
        phase = str(state.get("phase") or "")
        if not phase:
            return

        if phase in {"requesting_code", "awaiting_user"} and state.get("worker_pid"):
            if not _auth.process_is_alive(state.get("worker_pid")):
                # The worker writes its terminal phase before exiting. Re-read
                # once so a just-completed atomic update wins this race.
                state = _state.read_json_object(codex_reauth_state_path)
                phase = str(state.get("phase") or "")
                if phase in {"requesting_code", "awaiting_user"}:
                    state.update(
                        {
                            "phase": "failed",
                            "failed_ts": int(time.time()),
                            "error": "Codex device authentication stopped unexpectedly.",
                        }
                    )
                    _state.write_json_object(codex_reauth_state_path, state)
                    phase = "failed"

        if phase == "awaiting_user" and not state.get("instructions_sent_ts"):
            try:
                _transport.send_reply(
                    token, chat_id, _auth.format_codex_reauth_instructions(state)
                )
            except Exception as exc:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "codex_reauth_instructions_failed",
                        "error": _transport.short_error(exc, env, args.max_log_chars),
                    },
                )
                return
            state["instructions_sent_ts"] = int(time.time())
            _state.write_json_object(codex_reauth_state_path, state)
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "codex_reauth_instructions_sent",
                    "attempt_id": state.get("attempt_id"),
                },
            )
            return

        if phase in {"authenticated", "restarting"}:
            authenticated_ts = int(state.get("authenticated_ts") or 0)
            if _lifecycle.agent_desired_state(args) == _settings.AGENT_DESIRED_STOPPED:
                _auth.clear_codex_auth_failure(
                    codex_auth_state_path, "device_auth_completed"
                )
                state.update(
                    {
                        "phase": "completed",
                        "completed_ts": int(time.time()),
                        "agent_restarted": False,
                    }
                )
                _state.write_json_object(codex_reauth_state_path, state)
                phase = "completed"
            if phase == "restarting":
                active = agent_registry.active_agent_for_pane(args.target_pane)
                try:
                    active_started = int(float((active or {}).get("created_ts")))
                except (TypeError, ValueError):
                    active_started = 0
                if active_started >= authenticated_ts:
                    state["phase"] = "completed"
                    state["completed_ts"] = int(time.time())
                    state["agent_id"] = (active or {}).get("agent_id")
                    _auth.clear_codex_auth_failure(
                        codex_auth_state_path, "device_auth_completed"
                    )
                    _state.write_json_object(codex_reauth_state_path, state)
                    phase = "completed"
                else:
                    state["phase"] = "authenticated"
                    _state.write_json_object(codex_reauth_state_path, state)
                    phase = "authenticated"

            if phase == "authenticated":
                previous_effort = (
                    _models.current_codex_reasoning_effort(args.target_pane)
                    or _settings.DEFAULT_CODEX_AGENT_REASONING_EFFORT
                )
                if phase == "authenticated":
                    state["phase"] = "restarting"
                    state["restart_started_ts"] = int(time.time())
                    _state.write_json_object(codex_reauth_state_path, state)
                    try:
                        target_pane, result, meta = _lifecycle.start_codex_agent(
                            repo_root=args.repo_root,
                            session=args.session,
                            window=args.codex_window,
                            restart=True,
                            reasoning_effort=previous_effort,
                        )
                        args.target_pane = target_pane
                        _auth.clear_codex_auth_failure(
                            codex_auth_state_path, "device_auth_completed"
                        )
                        state.update(
                            {
                                "phase": "completed",
                                "completed_ts": int(time.time()),
                                "target_pane": target_pane,
                                "agent_id": meta.get("agent_id") if meta else None,
                                "restart_result": result,
                                "restored_reasoning_effort": previous_effort,
                                "agent_restarted": True,
                            }
                        )
                        _state.write_json_object(codex_reauth_state_path, state)
                        phase = "completed"
                    except Exception as exc:
                        state.update(
                            {
                                "phase": "restart_failed",
                                "failed_ts": int(time.time()),
                                "error": _transport.short_error(exc, env),
                            }
                        )
                        _state.write_json_object(codex_reauth_state_path, state)
                        phase = "restart_failed"

        if phase == "completed" and not state.get("completion_sent_ts"):
            try:
                _transport.send_reply(
                    token,
                    chat_id,
                    (
                        "Codex sign-in succeeded and the Telegram agent was restarted. "
                        "Queued work will resume. Check /queue before resending a task."
                        if state.get("agent_restarted", True)
                        else (
                            "Codex sign-in succeeded. The Telegram agent remains stopped; "
                            "use /start_agent when wanted."
                        )
                    ),
                )
            except Exception as exc:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "codex_reauth_completion_notice_failed",
                        "error": _transport.short_error(exc, env, args.max_log_chars),
                    },
                )
                return
            state["completion_sent_ts"] = int(time.time())
            _state.write_json_object(codex_reauth_state_path, state)
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "codex_reauth_completed",
                    "attempt_id": state.get("attempt_id"),
                    "agent_id": state.get("agent_id"),
                },
            )
        elif phase in {"failed", "restart_failed"} and not state.get("failure_sent_ts"):
            detail = str(state.get("error") or "unknown error")[:500]
            try:
                _transport.send_reply(
                    token,
                    chat_id,
                    f"Codex sign-in recovery failed: {detail} "
                    "The listener is still alive; run /reauth to try again.",
                )
            except Exception as exc:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "codex_reauth_failure_notice_failed",
                        "error": _transport.short_error(exc, env, args.max_log_chars),
                    },
                )
                return
            state["failure_sent_ts"] = int(time.time())
            _state.write_json_object(codex_reauth_state_path, state)
            _state.append_jsonl(
                log_path,
                {
                    "ts": int(time.time()),
                    "event": "codex_reauth_failed",
                    "attempt_id": state.get("attempt_id"),
                    "phase": phase,
                    "error": detail,
                },
            )

    def reconcile_current_relay_confirmations() -> None:
        result = _submission.reconcile_pending_codex_submissions(
            relay_confirmation_state_path,
            log_path=log_path,
        )
        for item in result["stalled"]:
            message_id = item.get("message_id")
            try:
                _transport.send_reply(
                    token,
                    chat_id,
                    (
                        f"Automatic recovery for Telegram message {message_id} "
                        "failed. The listener stopped retrying to avoid duplicate "
                        "input; inspect /agent_status before retrying manually."
                    ),
                    reply_to_message_id=(
                        message_id if isinstance(message_id, int) else None
                    ),
                )
            except RuntimeError as exc:
                _state.append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "telegram_relay_stalled_notice_failed",
                        "message_id": message_id,
                        "error": _transport.short_error(exc, env, args.max_log_chars),
                    },
                )

    def drain_current_relay_queue() -> None:
        _queue.drain_telegram_relay_queue(
            args,
            env,
            token,
            chat_id,
            log_path,
            owner_user_id=owner_user_id,
            bot_username=bot_username,
            group_member_cache=group_member_cache,
            route_state_path=route_state_path,
        )

    def deliver_due_timed_messages() -> None:
        _schedule.process_due_timed_messages(
            args,
            env,
            token,
            chat_id,
            timed_message_state_path,
            log_path,
        )

    def maintain_current_codex_auth() -> None:
        refresh_current_codex_auth()
        notify_current_codex_auth_failure()
        reconcile_current_codex_reauth()

    inbox = _service.Inbox(Path(args.ingress_state_path))
    interrupted = inbox.recover()
    if interrupted:
        _transport.send_reply(
            token,
            chat_id,
            f"Listener recovered. {interrupted} interrupted delivery attempt(s) need review in /queue.",
        )

    def report_error(event, exc, update):
        detail = _transport.short_error(exc, env, args.max_log_chars)
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": event,
                "update_id": (update or {}).get("update_id"),
                "error": detail,
            },
        )
        if update:
            source, _group, _owner = _routing.resolve_source_chat(
                update, chat_id, owner_user_id, bot_username, token, group_member_cache
            )
            if source:
                message = update.get("message") or update.get("edited_message") or {}
                try:
                    _transport.send_reply(
                        token,
                        source,
                        "This message could not be delivered. Check /queue before resending.",
                        message_thread_id=message.get("message_thread_id"),
                    )
                except Exception:
                    pass
        return detail

    def dispatch(update):
        _queue.dispatch_telegram_update(
            update,
            args,
            env,
            token,
            chat_id,
            log_path,
            owner_user_id=owner_user_id,
            bot_username=bot_username,
            group_member_cache=group_member_cache,
            route_state_path=route_state_path,
        )

    maintenance_due = {}

    def maintain():
        for name, interval, action in (
            ("confirmations", 2, reconcile_current_relay_confirmations),
            ("queue", 1, drain_current_relay_queue),
            ("usage", 10, maintain_current_codex_usage),
            ("identity", 60, refresh_bot_identity),
            ("auth", 10, maintain_current_codex_auth),
            ("timers", 2, deliver_due_timed_messages),
        ):
            if time.monotonic() < maintenance_due.get(name, 0):
                continue
            maintenance_due[name] = time.monotonic() + interval
            try:
                action()
            except Exception as exc:
                report_error(name + "_failed", exc, None)
        if not args.once and time.monotonic() >= maintenance_due.get("watchdog", 0):
            maintenance_due["watchdog"] = time.monotonic() + 10
            notice = _lifecycle.maintain_managed_codex_agent(args, log_path)
            if notice:
                _transport.send_reply(token, chat_id, notice)

    def deliver():
        drain_current_codex_messages()
        _delivery.drain_agent_outbox(
            token,
            chat_id,
            agent_outbox_path,
            agent_outbox_offset_path,
            log_path,
            max_ack_age_seconds=args.max_agent_ack_age,
            max_progress_age_seconds=args.max_agent_progress_age,
            route_state_path=route_state_path,
            is_group_route=False,
        )

    workers = _service.RelayWorkers(
        inbox, dispatch, maintain, deliver, report_error, Path(args.health_state_path)
    )
    revision = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        text=True,
        capture_output=True,
        timeout=3,
    ).stdout.strip()
    workers.mark(revision=revision or "unversioned")
    if not args.once:
        workers.start()
        try:
            _transport.telegram_api(
                token,
                "setMyCommands",
                {
                    "commands": json.dumps(
                        [
                            {"command": command, "description": description}
                            for command, description in _ui.MENU
                        ]
                    )
                },
            )
        except Exception as exc:
            report_error("command_menu_failed", exc, None)
    try:
        while True:
            if not args.once and any(
                not thread.is_alive() for thread in workers.threads
            ):
                raise RuntimeError(
                    "Relay worker exited; restarting the listener to recover delivery."
                )
            try:
                updates = get_updates(
                    token,
                    offset,
                    timeout=0 if args.once else args.poll_timeout,
                    limit=args.limit,
                )
            except _transport.TransientTelegramError as exc:
                report_error("get_updates_transient_failure", exc, None)
                if args.once:
                    return 1
                workers.stop.wait(max(args.poll_interval, 2))
                continue
            for update in updates:
                try:
                    update_id = int(update["update_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    report_error("malformed_update_skipped", exc, None)
                    continue
                # Quick controls never enter the agent worker. Their authorization
                # is identical to normal messages, including group mentions.
                try:
                    quick = _ui.handle_quick_update(
                        update,
                        args,
                        token,
                        chat_id,
                        owner_user_id,
                        bot_username,
                        group_member_cache,
                        env=env,
                    )
                    if not quick:
                        inbox.accept(update)
                        workers.wake.set()
                except Exception as exc:
                    report_error("accept_update_failed", exc, update)
                    # Do not acknowledge updates that could not be persisted.
                    break
                offset = update_id + 1
                _state.write_offset(offset_path, offset)
            if args.once:
                while _state.read_json_object(inbox.path).get("pending"):
                    workers.control_once()
                maintain()
                deliver()
                return 0
    finally:
        workers.close()
        listener_lock.close()
