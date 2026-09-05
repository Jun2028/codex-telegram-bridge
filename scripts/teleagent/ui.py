"""Small Telegram controls that stay available while the agent is busy."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from notify import redact

from . import replies
from . import messages, routing, state, status, transport
from .settings import SGT

QUICK_COMMANDS = {"/start", "/help", "/ping", "/status", "/queue", "/cancel", "/models"}
MENU = [
    ("status", "Agent state and reply delivery"),
    ("queue", "Waiting messages and failed deliveries"),
    ("help", "How to use this bot"),
    ("models", "Choose a model and reasoning level"),
    ("interrupt", "Stop the current turn and submit a new instruction"),
    ("start_agent", "Start the stopped agent"),
    ("kill_agent", "Stop the agent; keep Telegram online"),
    ("restart_agent", "Start a fresh conversation"),
    ("timed", "Schedule a message"),
]
BUTTONS = {
    "inline_keyboard": [
        [
            {"text": "Refresh", "callback_data": "relay:status"},
            {"text": "Queue", "callback_data": "relay:queue"},
            {"text": "Help", "callback_data": "relay:help"},
        ]
    ]
}


def help_text() -> str:
    return (
        "Send a task as text, code, a document, photo or voice note.\n"
        "One bot = one agent and one conversation.\n\n"
        "/status — working, idle, stopped or recovering\n"
        "/queue — waiting messages and delivery problems\n"
        "/cancel ID — remove a waiting message\n"
        "/interrupt NEW TASK — stop this turn and submit NEW TASK\n"
        "/models — model choices; /reasoning LEVEL changes effort\n"
        "/timed HOURS MESSAGE — send a task later\n\n"
        "/kill_agent stops the agent; /start_agent starts it.\n"
        "/restart_agent starts a fresh conversation.\n"
        "/recent_messages and /replay_last recover earlier requests.\n"
        "/codex_usage and /reauth manage sign-in in private.\n\n"
        "In groups: @mention this bot or reply to it. Replies stay in the "
        "originating chat/topic. Other chats wait while it is working. "
        "Only the owner can change or stop the agent."
    )


def models_text() -> str:
    return (
        "Send /model NAME [LEVEL].\n\n"
        "astra / latest — GPT-6 Astra\nsol — GPT-5.6 Sol\n"
        "luna — GPT-5.6 Luna\nspark — GPT-5.3 Codex Spark\n"
        "ds-flash / ds-pro — DeepSeek V4\n\n"
        "Example: /model astra xhigh\n"
        "/reasoning high changes effort in the current chat.\n"
        "Changing provider starts a fresh conversation; changes within the "
        "same Codex home preserve it."
    )


def task_message(task: dict) -> dict:
    update = task.get("update") or {}
    return update.get("message") or update.get("edited_message") or {}


def visible_task(
    task: dict, chat_id: str, is_group: bool, topic_id: int | None
) -> bool:
    message = task_message(task)
    return not is_group or (
        str((message.get("chat") or {}).get("id")) == chat_id
        and message.get("message_thread_id") == topic_id
    )


def queue_text(args, chat_id: str, is_group: bool, topic_id: int | None) -> str:
    data = state.read_json_object(Path(args.relay_queue_state_path))
    visible = lambda item: visible_task(item, chat_id, is_group, topic_id)
    tasks = [item for item in data.get("tasks", []) if visible(item)]
    failures = [item for item in data.get("failed", []) if visible(item)]
    ingress_path = getattr(args, "ingress_state_path", "")
    ingress = state.read_json_object(Path(ingress_path)) if ingress_path else {}
    pending = [item for item in ingress.get("pending", []) if visible(item)]
    failures += [item for item in ingress.get("failed", []) if visible(item)]
    confirmation_path = getattr(args, "relay_confirmation_state_path", "")
    confirmation = (
        state.read_json_object(Path(confirmation_path)) if confirmation_path else {}
    )
    route_path = getattr(args, "reply_route_state_path", "")
    unconfirmed = []
    for phase in ("pending", "failed"):
        for item in confirmation.get(phase, []):
            route_id = messages.telegram_route_id_from_text(
                str(item.get("relay_text") or "")
            )
            route = (
                routing.reply_route_details(Path(route_path), route_id)
                if route_path and route_id
                else {}
            )
            converted = {
                **item,
                "update": {
                    "message": {
                        "message_id": item.get("message_id"),
                        "chat": {"id": (route or {}).get("chat_id")},
                        "message_thread_id": (route or {}).get("message_thread_id"),
                    }
                },
            }
            if visible(converted):
                (unconfirmed if phase == "pending" else failures).append(converted)
    lines = [f"Waiting: {len(tasks)} · incoming: {len(pending)}"]
    for task in tasks[:8]:
        message = task_message(task)
        text = " ".join(
            str(message.get("text") or message.get("caption") or "[attachment]").split()
        )[:100]
        age = status.format_uptime(
            time.time() - float(task.get("queued_ts") or time.time())
        )
        lines.append(f"{task['id']} · {age} · {text}")
    if unconfirmed:
        lines.append(
            f"Unconfirmed: {len(unconfirmed)} submitted message(s). Check before resending."
        )
    if failures:
        lines.append(f"Delivery problems: {len(failures)}")
    for task in failures[-3:]:
        message = task_message(task)
        detail = str(
            task.get("error")
            or task.get("last_error")
            or task.get("stalled_reason")
            or "delivery outcome unconfirmed"
        )
        detail = {
            "stale_pending_cleared": "receipt check expired; delivery unconfirmed",
            "marker_absent_unconfirmed_same_process": "receipt timed out; delivery unconfirmed",
        }.get(detail, detail)
        if is_group:
            detail = "delivery outcome unconfirmed; check /status before resending"
        detail = " ".join(detail.split())[:180]
        stamp = (
            message.get("date")
            or task.get("created_ts")
            or task.get("queued_ts")
            or task.get("received_ts")
            or task.get("failed_ts")
            or task.get("stalled_ts")
        )
        try:
            date = datetime.fromtimestamp(float(stamp), SGT).strftime("%d %b %H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            date = "date unavailable"
        lines.append(f"{date} · #{message.get('message_id', '?')}: {detail}")
    if tasks:
        lines.append(
            "/cancel ID removes a waiting message; /cancel all removes this visible queue."
        )
    else:
        lines.append(
            "No messages waiting for the agent in this chat."
            if is_group
            else "No messages waiting for the agent."
        )
    return "\n".join(lines)


def cancel_queued(
    args, payload: str, chat_id: str, is_group: bool, topic_id: int | None
) -> str:
    if not payload:
        return (
            "Use /cancel ID from /queue, or /cancel all. The running task is unchanged."
        )
    path = Path(args.relay_queue_state_path)
    with state.locked(path):
        data = state.read_json_object(path)
        removed = []
        kept = []
        for item in data.get("tasks", []):
            if (
                item.get("status") == "queued"
                and visible_task(item, chat_id, is_group, topic_id)
                and (payload == "all" or payload == str(item.get("id")))
            ):
                removed.append(item)
            else:
                kept.append(item)
        data["tasks"] = kept
        data["cancelled"] = (data.get("cancelled", []) + removed)[-100:]
        state.write_json_object(path, data)
    return (
        f"Removed {len(removed)} waiting message(s)."
        if removed
        else "No matching waiting message. Use /queue to check."
    )


def handle_quick_update(
    update: dict,
    args,
    token: str,
    owner_chat: str,
    owner_user: str,
    bot_username: str,
    member_cache: dict,
    *,
    env: dict | None = None,
) -> bool:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        action = str(callback.get("data") or "")
        # Previously sent History buttons open the current queue.
        if action == "relay:history":
            action = "relay:queue"
        if action not in {"relay:status", "relay:queue", "relay:help"}:
            return True
        message = dict(callback.get("message") or {})
        message["from"] = callback.get("from") or {}
        message["text"] = "/" + action.split(":")[1] + "@" + bot_username
        message.pop("entities", None)
        update = {"update_id": update.get("update_id"), "message": message}
    else:
        message = update.get("message") or update.get("edited_message") or {}
        # Captions describe an attachment, even when they start with a slash.
        if any(message.get(key) for key in ("document", "voice", "photo")):
            return False
    text = routing.strip_bot_mention(str(message.get("text") or ""), bot_username)
    command, payload = messages.normalize_command(text)
    if command not in QUICK_COMMANDS:
        return False
    chat_id, is_group, owner = routing.resolve_source_chat(
        update, owner_chat, owner_user, bot_username, token, member_cache
    )
    if callback:
        try:
            transport.telegram_api(
                token, "answerCallbackQuery", {"callback_query_id": callback.get("id")}
            )
        except RuntimeError as exc:
            # An expired spinner acknowledgement must not discard Refresh.
            health_path = getattr(args, "health_state_path", "")
            if health_path:
                state.append_jsonl(
                    Path(health_path).with_name("telegram_inbox.jsonl"),
                    {
                        "ts": int(time.time()),
                        "event": "callback_ack_failed",
                        "error": transport.short_error(exc, env),
                    },
                )
    if chat_id is None:
        return True
    topic = message.get("message_thread_id")
    if command == "/status":
        auth_path = getattr(args, "codex_auth_state_path", "")
        auth = state.read_json_object(Path(auth_path)) if auth_path else {}
        reply = status.format_system_status(
            args.session,
            args.target_pane,
            args.tmux_lines,
            args=args,
            auth_failure=auth if auth.get("blocked") else None,
            audience_chat_id=chat_id if is_group else None,
        )
    elif command == "/queue":
        reply = queue_text(args, chat_id, is_group, topic)
    elif command == "/cancel":
        reply = (
            cancel_queued(args, payload, chat_id, is_group, topic)
            if owner
            else "Only the bot owner can cancel queued tasks."
        )
    elif command == "/models":
        reply = models_text()
    elif command == "/ping":
        reply = "Online. /status shows the agent; /queue shows waiting work."
    else:
        reply = help_text()
    replies.send(
        args,
        token,
        chat_id,
        redact(reply, env or {}),
        message_thread_id=topic,
        reply_markup=BUTTONS
        if command in {"/status", "/help", "/start", "/queue"}
        else None,
    )
    return True
