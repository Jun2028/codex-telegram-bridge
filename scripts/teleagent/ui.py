"""Small Telegram controls that stay available while the agent is busy."""

from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from notify import redact

from . import replies
from . import messages, provider_commands, routing, state, status, transport
from .settings import SGT

QUICK_COMMANDS = {"/start", "/help", "/ping", "/status", "/queue", "/cancel", "/models", "/files"}


@dataclass(frozen=True)
class Command:
    """One Telegram command the listener handles.

    This table is the single list of commands: the Telegram menu, `/help all`
    and the unknown-command suggestion are generated from it, and a test fails
    when a handler and this table disagree.
    """

    name: str
    summary: str
    args: str = ""  # Argument syntax shown in `/help all`.
    menu: bool = False  # Offered in Telegram's command menu.
    same_as: str = ""  # The command this is another spelling of.


PROVIDERS = provider_commands.PROVIDER_CHOICES
COMMANDS: tuple[Command, ...] = (
    # Everyday work.
    Command("status", "Agent state and reply delivery", menu=True),
    Command("queue", "Waiting messages and failed deliveries", menu=True),
    Command("cancel", "Remove a waiting message", "[NUMBER|all]"),
    Command("interrupt", "Stop the current turn and submit a new instruction", "NEW TASK", menu=True),
    Command("timed", "Schedule a message", "HOURS MESSAGE | list | remove N", menu=True),
    Command("recent_messages", "List earlier requests from this chat", "[COUNT]"),
    Command("replay_last", "Resubmit the most recent requests", "[COUNT]"),
    Command("replay_last_long", "Resubmit the most recent long requests", "[COUNT]"),
    Command("replay_messages", "Resubmit requests by message id", "ID ..."),
    Command("files", "Open the file browser in private chat"),
    Command("help", "How to use this bot", "[all]", menu=True),
    Command("ping", "Check that the listener is online"),
    # The agent: the same words for every provider.
    Command("models", "Choose a model and reasoning level", menu=True),
    Command("switch", "Change provider; starts a fresh conversation",
            f"{PROVIDERS} [MODEL] [LEVEL]", menu=True),
    Command("start_agent", "Start the stopped agent", "[MODEL] [LEVEL]", menu=True),
    Command("restart_agent", "Start a fresh conversation", "[MODEL] [LEVEL]", menu=True),
    Command("kill_agent", "Stop the agent; keep Telegram online", menu=True),
    Command("agent_status", "Agent identity, session and sign-in details in private"),
    # Accounts, in the owner's private chat. No provider means the running one.
    Command("usage", "Check live usage limits", "[codex|claude]", menu=True),
    Command("auth", "Sign-in status, sign in or sign out",
            "[codex|claude] [status|login|logout|cancel]", menu=True),
    Command("claude_code", "Submit the one-time code from Claude sign-in", "CODE"),
    Command("codex_reset", "Check and redeem an available Codex usage reset", menu=True),
    Command("confirm", "Confirm a pending Codex usage reset"),
    # Controls that exist only in a Codex chat.
    Command("model", "Change model in the current Codex chat", "NAME [LEVEL]"),
    Command("reasoning", "Change effort in the current Codex chat", "LEVEL"),
    Command("resume_goal", "Resume a paused Codex goal"),
    Command("codex", "Send a slash command to the Codex chat", "/COMMAND"),
    # Other spellings that keep working.
    Command("start", "", same_as="/help"),
    Command("agent", "", same_as="sending plain text"),
    Command("claude", "", same_as="/switch claude"),
    Command("dsh", "", same_as="/switch dsh"),
    Command("claude_usage", "", same_as="/usage claude"),
    Command("codex_usage", "", same_as="/usage codex"),
    Command("claude_auth", "", same_as="/auth claude"),
    Command("codex_auth", "", same_as="/auth codex"),
    Command("reauth", "", same_as="/auth login"),
)
COMMAND_NAMES = frozenset(command.name for command in COMMANDS)
MENU = [(command.name, command.summary) for command in COMMANDS if command.menu]
BUTTONS = {
    "inline_keyboard": [
        [
            {"text": "Refresh", "callback_data": "relay:status"},
            {"text": "Queue", "callback_data": "relay:queue"},
            {"text": "Model", "callback_data": provider_commands.PICK},
            {"text": "Help", "callback_data": "relay:help"},
        ]
    ]
}


def mini_app_url(env: dict | None) -> str:
    path = (env or {}).get("TELEAGENT_MINI_APP_URL_FILE")
    if not path:
        return ""
    try:
        url = Path(path).read_text(encoding="utf-8").strip()
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname
                and not parsed.username and not parsed.password
                and not any(c.isspace() or ord(c) < 32 for c in url)):
            return url
    except (OSError, ValueError):
        pass
    return ""


def command_menu(env: dict | None = None) -> list[dict]:
    commands = list(MENU)
    if mini_app_url(env):
        commands.append(("files", "Open the file browser in private chat"))
    return [{"command": name, "description": description}
            for name, description in commands]


def help_text() -> str:
    return (
        "Send a task as text, code, a document, photo or voice note.\n"
        "One bot = one agent and one conversation.\n\n"
        "/status — working, idle, stopped or recovering\n"
        "/queue — waiting messages and delivery problems\n"
        "/cancel [number] — remove a waiting message\n"
        "/interrupt NEW TASK — stop this turn and submit NEW TASK\n"
        "/timed HOURS MESSAGE — send a task later\n"
        "/recent_messages and /replay_last recover earlier requests.\n\n"
        "The same commands work for every provider "
        "(codex, claude, dsh, agy):\n"
        "/switch PROVIDER [MODEL] [LEVEL] — change provider, fresh conversation\n"
        "/usage [PROVIDER] — live usage limits, in private\n"
        "/auth [PROVIDER] [status|login|logout|cancel] — sign-in, in private\n"
        "/models — model choices; /reasoning LEVEL changes effort\n"
        "/kill_agent stops the agent; /start_agent starts it.\n"
        "/restart_agent starts a fresh conversation.\n"
        "Without a provider, /usage and /auth apply to the running agent.\n"
        "/help all lists every command.\n\n"
        "In groups: @mention this bot or reply to it. Replies stay in the "
        "originating chat/topic. Other chats wait while it is working. "
        "Only the owner can change or stop the agent."
    )


def all_commands_text() -> str:
    """Every command the listener accepts, generated from `COMMANDS`."""
    lines = ["Every command:"]
    for command in COMMANDS:
        if command.same_as:
            continue
        syntax = f"/{command.name} {command.args}".rstrip()
        lines.append(f"{syntax} — {command.summary}")
    lines.append("")
    lines.append("Other spellings that still work:")
    lines += [f"/{command.name} = {command.same_as}"
              for command in COMMANDS if command.same_as]
    return "\n".join(lines)


def help_reply(payload: str) -> str:
    return all_commands_text() if payload.strip().lower() == "all" else help_text()


def unknown_command_text(command: str) -> str:
    """Point a mistyped command at the nearest real one."""
    close = difflib.get_close_matches(
        command.lstrip("/"), sorted(COMMAND_NAMES), n=1, cutoff=0.6
    )
    hint = f"Did you mean /{close[0]}? " if close else ""
    return (
        f"Unknown command. {hint}Send normal text for the agent, "
        "or use /help all to list every command."
    )


def models_text() -> str:
    return (
        "Start any model with /switch PROVIDER [MODEL] [LEVEL]. "
        "It begins a fresh conversation.\n\n"
        "codex — sol / latest (GPT-6.1 Sol, default), astra (GPT-6 Astra), "
        "gpt-5.6-sol, luna (GPT-5.6 Luna), spark (GPT-5.3 Codex Spark), "
        "ds-flash / ds-pro (DeepSeek V4 through Codex)\n"
        "claude — sonnet (default), opus, haiku; default level high\n"
        "dsh — flash (default), pro; levels off, low, high, max (default)\n"
        "agy — g-flash, argon, sonnet, opus, oss (Antigravity-hosted)\n\n"
        "Examples: /switch claude opus max, /switch codex astra xhigh, "
        "/switch agy g-flash\n\n"
        "Inside a Codex chat, /model NAME [LEVEL] and /reasoning LEVEL change "
        "the model or effort and keep the conversation while it stays on the "
        "same Codex home.\n"
        "/start_agent and /restart_agent also take MODEL [LEVEL]; there "
        "Claude models are written cc-sonnet, cc-opus, cc-haiku."
    )


def picker_keyboard(rows: provider_commands.Buttons) -> dict:
    return {"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def _handle_picker(update, callback, action, args, token, owner_chat, owner_user,
                   bot_username, member_cache) -> bool:
    """Tap-to-select provider, model and effort.

    Intermediate taps redraw the same message. The last tap becomes an ordinary
    command message (`/switch`, or `/auth` to sign in), so it takes the same
    authorization, queueing and lifecycle path as a typed command; this
    function then returns False.
    """
    message = dict(callback.get("message") or {})
    message["from"] = callback.get("from") or {}
    message.pop("entities", None)
    mention = f"@{bot_username}" if bot_username else ""
    probe = {"update_id": update.get("update_id"), "message": {**message, "text": "/models" + mention}}
    chat_id, _is_group, owner = routing.resolve_source_chat(
        probe, owner_chat, owner_user, bot_username, token, member_cache)
    try:
        transport.telegram_api(token, "answerCallbackQuery", {"callback_query_id": callback.get("id")})
    except RuntimeError:
        pass  # An expired tap acknowledgement must not discard the choice.
    if chat_id is None or not owner:
        return True
    step = provider_commands.picker([part for part in action.split(":")[1:] if part])
    if isinstance(step, str):
        message.pop("reply_markup", None)
        command, _, rest = step.partition(" ")
        message.update(text=f"{command}{mention} {rest}", date=int(time.time()))
        update.pop("callback_query", None)
        update["message"] = message
        return False
    text, rows = step
    try:
        transport.telegram_api(token, "editMessageText", {
            "chat_id": chat_id, "message_id": message.get("message_id"), "text": text,
            "reply_markup": json.dumps(picker_keyboard(rows))})
    except RuntimeError:
        # The original message can no longer be edited; ask in a new one.
        replies.send(args, token, chat_id, text, message_thread_id=message.get("message_thread_id"),
                     reply_markup=picker_keyboard(rows))
    return True


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
    # Keep the queue actionable; old failures remain in the audit state.
    recent_failures = []
    for item in failures:
        stamp = (item.get("failed_ts") or item.get("stalled_ts")
                 or item.get("created_ts") or item.get("received_ts")
                 or item.get("queued_ts") or task_message(item).get("date"))
        try:
            if time.time() - float(stamp) <= 24 * 60 * 60:
                recent_failures.append(item)
        except (TypeError, ValueError):
            pass
    failures = recent_failures
    lines = [f"Waiting: {len(tasks)} · incoming: {len(pending)}"]
    for number, task in enumerate(tasks[:8], 1):
        message = task_message(task)
        text = " ".join(
            str(message.get("text") or message.get("caption") or "[attachment]").split()
        )[:100]
        age = status.format_uptime(
            time.time() - float(task.get("queued_ts") or time.time())
        )
        lines.append(f"{number}. {text} · {age} · /cancel {number}")
    if unconfirmed:
        lines.append(
            f"Delivery check waiting: {len(unconfirmed)} sent message(s). Check /status before resending."
        )
    if failures:
        lines.append(f"Past delivery problems (not waiting): {len(failures)}")
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
            "/cancel removes the only waiting message; /cancel 1 removes item 1; /cancel all removes this visible queue."
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
    path = Path(args.relay_queue_state_path)
    with state.locked(path):
        data = state.read_json_object(path)
        visible = [item for item in data.get("tasks", [])
                   if visible_task(item, chat_id, is_group, topic_id)]
        payload = payload.strip()
        if not payload:
            if len(visible) != 1:
                return ("No messages waiting." if not visible else
                        "More than one message is waiting. Use /queue, then /cancel 1 (the list number), or /cancel all.")
            payload = str(visible[0].get("id"))
        elif payload.isdecimal():
            number = int(payload)
            if not 1 <= number <= len(visible):
                return "No waiting message with that list number. Use /queue to check."
            payload = str(visible[number - 1].get("id"))
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
        if action.split(":")[0] == provider_commands.PICK:
            return _handle_picker(update, callback, action, args, token, owner_chat,
                                  owner_user, bot_username, member_cache)
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
    # Never send the private launch credential into a group, even for its owner.
    file_url = mini_app_url(env) if owner and not is_group else ""
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
    elif command == "/files":
        reply = ("Open the file browser below." if file_url else
                 "Open /files in the bot owner's private chat." if is_group else
                 "The file browser is not configured or its launch URL is unavailable.")
    else:
        reply = help_reply(payload)
        if file_url:
            reply += "\n\n/files opens the file browser inside Telegram."
    buttons = None
    if command == "/models" and owner:
        buttons = picker_keyboard(provider_commands.picker([])[1])
    if command in {"/status", "/help", "/start", "/queue", "/files"}:
        buttons = {"inline_keyboard": [*BUTTONS["inline_keyboard"]]}
        if file_url:
            buttons["inline_keyboard"].append([
                {"text": "Files", "web_app": {"url": file_url}}
            ])
    replies.send(
        args,
        token,
        chat_id,
        redact(reply, env or {}),
        message_thread_id=topic,
        reply_markup=buttons,
    )
    return True
