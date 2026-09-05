#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in teleagent services."""


import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402
import codex_rate_limits  # noqa: E402
from notify import assert_safe_local_path, env_value, load_env, redact, run_short, tls_context  # noqa: E402
from telegram_format import render_telegram_html, telegram_final_marker_suffix  # noqa: E402

from teleagent.app import (
    get_updates,
    main,
)
from teleagent.attachments import (
    safe_inbound_document_name,
    validate_inbound_document,
    download_telegram_document,
    format_inbound_document_text,
    largest_telegram_photo,
    validate_inbound_photo,
    download_telegram_photo,
    format_inbound_photo_text,
    voice_tool_problem,
    download_telegram_voice,
    transcribe_voice_ogg,
    format_voice_text,
)
from teleagent.auth import (
    _nested_codex_error_code,
    _nested_error_messages,
    _codex_auth_failure_observation,
    refresh_codex_auth_state,
    active_codex_auth_failure,
    mark_codex_auth_blocked,
    clear_codex_auth_failure,
    format_auth_failure_fallback,
    process_is_alive,
    codex_reauth_in_progress,
    format_codex_reauth_instructions,
    start_codex_reauth,
    codex_login_status_summary,
    run_codex_reset_helper,
    inspect_codex_live_usage,
    live_codex_remaining_percentages,
    live_codex_rate_availability,
    reconcile_codex_usage_state_from_live_query,
    list_codex_usage_resets,
    reset_confirmation_state,
    format_codex_reset_confirmation,
    format_live_codex_limits,
)
from teleagent.commands import (
    handle_update,
)
from teleagent.delivery import (
    drain_agent_outbox,
    format_forwarded_agent_message,
    codex_agent_message,
    codex_agent_message_id,
    instance_suppresses_final_marker,
    drain_codex_agent_messages,
    _persist_agent_message_state,
)
from teleagent.identity import (
    per_chat_inbox_records_enabled,
    people_memory_enabled,
    people_memory_state_path,
    _compact_person_memory_text,
    remember_telegram_person,
    telegram_person_context,
    per_chat_inbox_record_path,
    backfill_per_chat_inbox_records,
)
from teleagent.lifecycle import (
    agent_lifecycle_state_path,
    agent_desired_state,
    set_agent_desired_state,
    agent_lifecycle_operation_in_progress,
    build_codex_agent_command,
    start_codex_agent,
    managed_codex_agent_present,
    stop_codex_agent,
    ensure_codex_target_for_agent_message,
    maintain_managed_codex_agent,
)
from teleagent.messages import (
    normalize_command,
    sender_label,
    visible_message_text,
    telegram_message_time,
    telegram_update_route_id,
    telegram_route_id_from_text,
    telegram_route_id_from_codex_record,
    format_reply_context,
    format_agent_message,
    parse_message_ids,
    parse_count,
    record_message_text,
    iter_agent_message_records,
    load_agent_records_by_message_id,
    load_recent_agent_records,
    recent_messages_text,
    format_replayed_messages,
    parse_agent_launch_payload,
)
from teleagent.models import (
    parse_live_reasoning_effort,
    normalize_codex_agent_model,
    validate_model_reasoning_effort,
    parse_live_model_payload,
    current_codex_model_and_reasoning_effort,
    codex_session_model_and_reasoning_effort,
    current_codex_reasoning_effort,
    select_codex_reasoning_effort,
    restore_codex_composer,
    set_codex_model,
    set_codex_reasoning_effort,
)
from teleagent.processes import (
    restore_tmux_socket_from_env,
    tmux_value,
    tmux_pane_command,
    tmux_pane_has_codex_supervisor,
    tmux_pane_has_codex_process,
    codex_process_identity,
    process_has_codex_session,
    registered_codex_process_running,
    codex_target_ready,
    wait_for_supervised_codex,
    tmux_target_exists,
    tmux_window_exists,
    ensure_tmux_session,
    codex_executable,
    codex_home_for_model,
    tmux_bash_shell_command,
    tmux_tail,
    codex_goal_blocked,
    codex_goal_active,
    codex_tui_idle,
    relay_codex_control,
    tmux_send_keys,
    wait_for_tmux_text,
    resume_blocked_goal_if_needed,
)
from teleagent.queue import (
    relay_queue_state_path,
    telegram_update_is_agent_message,
    enqueue_telegram_relay,
    telegram_relay_queue_tasks,
    remove_telegram_relay_queue_task,
    block_telegram_relay_queue_task,
    dispatch_telegram_update,
    _fail_stale_pending_codex_submissions,
    drain_telegram_relay_queue,
)
from teleagent.routing import (
    message_mentions_bot,
    require_group_mention,
    strip_bot_mention,
    group_owner_is_member,
    resolve_source_chat,
    read_reply_route_state,
    reply_route_for_id,
    set_reply_route_chat_id,
    clear_reply_route_chat_id,
)
from teleagent.schedule import (
    parse_timed_payload,
    timed_message_snapshot,
    write_timed_message_state,
    schedule_timed_message,
    timed_message_task_chat_id,
    timed_message_is_active,
    timed_message_sort_key,
    timed_messages_for_chat,
    remove_timed_messages,
    escape_commonmark_text,
    timed_message_preview,
    format_timed_message_fired,
    timed_message_due_label,
    timed_message_status_label,
    format_timed_message_entry,
    format_timed_message_list,
    parse_timed_remove_number,
    timed_message_checkpoint,
    timed_message_is_confirmed,
    process_due_timed_messages,
)
from teleagent.sessions import (
    codex_session_metadata,
    iso_timestamp_epoch,
    valid_codex_session_for_agent,
)
from teleagent.settings import (
    SCRIPT_DIR,
    SHELL_COMMANDS,
    CODEX_COMMANDS,
    DEFAULT_SCRATCH,
    DEFAULT_TELEGRAM_LOG_DIR,
    DEFAULT_INBOUND_DOCUMENTS_DIR,
    DEFAULT_INBOUND_VOICE_DIR,
    DEFAULT_INBOUND_PHOTO_DIR,
    DEFAULT_WHISPER_BIN,
    DEFAULT_WHISPER_MODEL,
    DEFAULT_OPUSDEC_BIN,
    DEFAULT_OPUSDEC_LIB_DIR,
    TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES,
    DEFAULT_MAX_INBOUND_DOCUMENT_BYTES,
    DEFAULT_MAX_INBOUND_VOICE_BYTES,
    DEFAULT_MAX_INBOUND_PHOTO_BYTES,
    SUPPORTED_INBOUND_DOCUMENT_SUFFIXES,
    QUICK_ACTIONS_KEYBOARD,
    TRANSIENT_HTTP_CODES,
    LEGACY_REPLY_PREFIX_RE,
    TELEGRAM_USER_MESSAGE_MARKER_RE,
    TELEGRAM_ROUTE_ID_RE,
    DEFAULT_CODEX_AGENT_MODEL,
    DEFAULT_CODEX_AGENT_REASONING_EFFORT,
    ASTRA_CODEX_AGENT_MODEL,
    SOL_CODEX_AGENT_MODEL,
    LATEST_OPENAI_CODEX_AGENT_MODEL,
    LATEST_OPENAI_CODEX_AGENT_REASONING_EFFORT,
    SPARK_CODEX_AGENT_MODEL,
    LUNA_CODEX_AGENT_MODEL,
    DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
    DEEPSEEK_PRO_CODEX_AGENT_MODEL,
    SUPPORTED_CODEX_AGENT_MODELS,
    CODEX_AGENT_MODEL_ALIASES,
    LIVE_CODEX_AGENT_MODELS,
    LIVE_CODEX_AGENT_MODEL_ALIASES,
    SUPPORTED_CODEX_REASONING_EFFORTS,
    ASTRA_CODEX_REASONING_EFFORTS,
    LIVE_CODEX_REASONING_EFFORTS,
    LIVE_CODEX_REASONING_ALIASES,
    CODEX_RESET_CONFIRM_TTL_SECONDS,
    RELAY_CONFIRMATION_RECOVERY_GRACE_SECONDS,
    RELAY_CONFIRMATION_RECOVERY_INTERVAL_SECONDS,
    RELAY_CONFIRMATION_RECOVERY_TIMEOUT_SECONDS,
    INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS,
    TMUX_MUTATION_TIMEOUT_SECONDS,
    TIMED_MESSAGE_RETRY_SECONDS,
    TIMED_MESSAGE_DELIVERY_GRACE_SECONDS,
    TIMED_MESSAGE_LIST_CHUNK_CHARS,
    TIMED_MESSAGE_LIST_PREVIEW_CHARS,
    ACTIVE_TIMED_MESSAGE_STATUSES,
    AGENT_DESIRED_RUNNING,
    AGENT_DESIRED_STOPPED,
    USAGE_LIMIT_REACHED_TYPES,
    USAGE_LIMIT_ERROR_CODES,
    CODEX_AUTH_ERROR_CODES,
    CODEX_AUTH_ERROR_MESSAGE_RE,
    DEVICE_URL_RE,
    CODEX_RELAY_COMMANDS,
    CODEX_AUTH_BLOCKED_COMMANDS,
    RELAYED_AGENT_ACTIONS,
    SGT,
    REGISTERED_CODEX_PID_CACHE,
    COMMONMARK_PUNCTUATION_RE,
    GROUP_CHAT_TYPES,
    GROUP_OWNER_MEMBER_TTL_SECONDS,
    REPLY_ROUTE_MAX_AGE_SECONDS,
    REPLY_ROUTE_RECORD_MAX_AGE_SECONDS,
    REPLY_ROUTE_RECORD_MAX_COUNT,
    PER_CHAT_INBOX_RECORDS_ENV,
    PEOPLE_MEMORY_ENV,
    PEOPLE_MEMORY_MAX_OBSERVATIONS,
    PEOPLE_MEMORY_PROMPT_OBSERVATIONS,
    PEOPLE_MEMORY_MAX_TEXT_CHARS,
    RELAY_QUEUE_GOAL_PAUSE_GRACE_SECONDS,
    RELAY_QUEUE_GOAL_PAUSE_MAX_ATTEMPTS,
    RELAY_PENDING_WEDGE_SECONDS,
)
from teleagent.state import (
    read_json_object,
    write_json_object,
    state_path,
    read_offset,
    write_offset,
    append_jsonl,
)
from teleagent.status import (
    format_uptime,
    codex_session_start_epoch,
    codex_session_context_snapshot,
    latest_agent_message_text,
    format_system_status,
)
from teleagent.submission import (
    codex_session_checkpoint,
    wait_for_codex_submission,
    codex_session_bootstrap_allowed,
    wait_for_codex_bootstrap_submission,
    codex_session_turn_active,
    wait_for_codex_turn_terminal,
    register_pending_codex_submission,
    pending_codex_submission_pane_location,
    pending_codex_submission_in_composer,
    current_pending_codex_submissions,
    cancel_pending_codex_submissions,
    tmux_paste_text_atomic,
    reconcile_pending_codex_submissions,
    paste_to_tmux,
    interrupt_codex_with_prompt,
)
from teleagent.transport import (
    TransientTelegramError,
    short_error,
    telegram_api,
    send_reply,
)
from teleagent.usage import (
    _usage_error_code,
    _rate_limit_summary,
    _usage_limit_observation,
    _telegram_message_id_from_record,
    _usage_depletion_kind,
    refresh_codex_usage_state,
    clear_codex_usage_depletion,
    notify_codex_usage_failure,
)

if __name__ == "__main__":
    raise SystemExit(main())
