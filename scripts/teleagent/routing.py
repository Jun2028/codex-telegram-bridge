"""Routing services for the Telegram relay."""

from __future__ import annotations


import os
import re
import time
from pathlib import Path
from typing import Any

from . import settings as _settings
from . import state as _state
from . import transport as _transport


def message_mentions_bot(message: dict[str, Any], bot_username: str) -> bool:
    """Recognize exact bot addresses, captions, and replies to this bot."""
    username = str(bot_username or "").lstrip("@").casefold()
    if not username:
        return False
    replied = message.get("reply_to_message") or {}
    sender = replied.get("from") or {}
    if (
        sender.get("is_bot")
        and str(sender.get("username") or "").casefold() == username
    ):
        return True
    text = str(message.get("text") or message.get("caption") or "")
    # Boundaries prevent @mybot_backup from addressing @mybot. Telegram entity
    # offsets are UTF-16 units, so Python string offsets must not be used.
    if re.search(
        r"(?<![\w@])@" + re.escape(username) + r"(?![\w])", text, re.IGNORECASE
    ):
        return True
    first = text.split(maxsplit=1)[0] if text else ""
    if (
        first.startswith("/")
        and "@" in first
        and first.rsplit("@", 1)[1].casefold() == username
    ):
        return True
    encoded = text.encode("utf-16-le")
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if not isinstance(entity, dict) or entity.get("type") not in {
            "mention",
            "bot_command",
        }:
            continue
        try:
            start, length = int(entity["offset"]), int(entity["length"])
            if start < 0 or length <= 0:
                continue
            fragment = (
                encoded[start * 2 : (start + length) * 2].decode("utf-16-le").casefold()
            )
        except (KeyError, ValueError, TypeError, UnicodeError):
            continue
        if (
            fragment == "@" + username
            or fragment.endswith("@" + username)
            and fragment.startswith("/")
        ):
            return True
    return False


def require_group_mention() -> bool:
    """Return whether owner group messages must mention this bot to relay."""
    return os.environ.get("TELEAGENT_REQUIRE_GROUP_MENTION", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove a leading @bot username so the relayed instruction is the rest."""
    if not bot_username:
        return str(text or "").strip()
    username = str(bot_username).lstrip("@").lower()
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    marker = f"@{username}"
    if lowered.startswith(marker) and (
        len(stripped) == len(marker) or not re.match(r"[\w]", stripped[len(marker)])
    ):
        stripped = stripped[len(marker) :]
    return stripped.lstrip(" ,:;-—–").strip()


def group_owner_is_member(
    token: str,
    chat_id: str,
    owner_user_id: str,
    cache: dict[str, tuple[float, bool]],
    ttl_seconds: float = _settings.GROUP_OWNER_MEMBER_TTL_SECONDS,
) -> bool:
    """Confirm the configured owner is a member before trusting a group."""
    if not owner_user_id:
        return False
    now = time.time()
    cached = cache.get(chat_id)
    if cached is not None and now - cached[0] < ttl_seconds:
        return cached[1]
    allowed = False
    try:
        member = _transport.telegram_api(
            token,
            "getChatMember",
            {"chat_id": chat_id, "user_id": owner_user_id},
            timeout=10,
        )
        status = str((member or {}).get("status") or "")
        allowed = status in ("creator", "administrator", "member")
    except Exception:
        allowed = False
    cache[chat_id] = (now, allowed)
    return allowed


def resolve_source_chat(
    update: dict[str, Any],
    allowed_chat_id: str,
    owner_user_id: str,
    bot_username: str,
    token: str,
    group_member_cache: dict[str, tuple[float, bool]],
) -> tuple[str | None, bool, bool]:
    """Return (source_chat_id, is_group, group_owner) for an authorized update.

    Private chats must match the configured owner chat. Groups are accepted
    only when the owner sent the message, or the bot is mentioned and the
    configured owner is a member of that group.
    """
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None, False, False
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    chat_type = str(chat.get("type") or "")
    sender = message.get("from") or {}
    sender_id = str(sender.get("id", ""))
    is_group = chat_type in _settings.GROUP_CHAT_TYPES
    if is_group:
        addressed = strip_bot_mention(str(message.get("text") or ""), bot_username)
        first = addressed.split(maxsplit=1)[0] if addressed else ""
        if (
            first.startswith("/")
            and "@" in first
            and first.rsplit("@", 1)[1].casefold()
            != bot_username.lstrip("@").casefold()
        ):
            return None, True, False
        group_owner = bool(owner_user_id and sender_id == owner_user_id)
        if group_owner and require_group_mention():
            if not message_mentions_bot(message, bot_username):
                return None, True, group_owner
        if group_owner or (
            bot_username
            and message_mentions_bot(message, bot_username)
            and group_owner_is_member(
                token,
                chat_id,
                owner_user_id,
                group_member_cache,
            )
        ):
            return chat_id, True, group_owner
        return None, True, group_owner
    # Telegram always includes ``type``; accepting an omitted value keeps
    # direct/local callers and older persisted test fixtures compatible while
    # explicit group/supergroup chats still take the restricted branch above.
    if chat_type in ("", "private") and chat_id == allowed_chat_id:
        return chat_id, False, True
    return None, False, False


def read_reply_route_state(state_path: Path) -> tuple[str | None, bool]:
    """Return the compatibility latest route, if still fresh.

    Per-message Codex delivery must use :func:`reply_route_for_id`; this
    top-level value remains for older callers and diagnostics only. A stale or
    missing route resolves to ``(None, False)``.
    """
    state = _state.read_json_object(state_path)
    try:
        updated_ts = float(state.get("ts") or 0)
    except (TypeError, ValueError):
        updated_ts = 0.0
    if (
        not updated_ts
        or time.time() - updated_ts > _settings.REPLY_ROUTE_MAX_AGE_SECONDS
    ):
        return None, False
    chat_id = state.get("chat_id")
    return (str(chat_id) if chat_id else None), bool(state.get("is_group"))


def reply_route_for_id(
    state_path: Path,
    route_id: str | None,
) -> tuple[str, bool] | None:
    """Look up the destination bound to one injected Telegram prompt.

    The legacy top-level ``chat_id`` is deliberately not used here. It is a
    single mutable value and was the source of cross-chat leakage when a
    second Telegram message arrived during an active Codex turn.
    """
    normalized = str(route_id or "").strip()
    if not normalized:
        return None
    state = _state.read_json_object(state_path)
    routes = state.get("routes")
    if not isinstance(routes, list):
        return None
    now = time.time()
    for raw_route in reversed(routes):
        if not isinstance(raw_route, dict):
            continue
        if str(raw_route.get("route_id") or "") != normalized:
            continue
        chat_id = str(raw_route.get("chat_id") or "").strip()
        if not chat_id:
            return None
        try:
            route_ts = float(raw_route.get("ts") or 0)
        except (TypeError, ValueError):
            route_ts = 0.0
        if route_ts and now - route_ts > _settings.REPLY_ROUTE_RECORD_MAX_AGE_SECONDS:
            return None
        return chat_id, bool(raw_route.get("is_group"))
    return None


@_state.serialized
def set_reply_route_chat_id(
    state_path: Path,
    chat_id: str,
    *,
    is_group: bool,
    route_id: str | None = None,
    message_thread_id: int | None = None,
    source_message_id: int | None = None,
) -> None:
    """Persist both a compatibility latest route and an immutable prompt route."""
    now = int(time.time())
    state = _state.read_json_object(state_path)
    normalized_route_id = str(route_id or "").strip()
    routes: list[dict[str, Any]] = []
    raw_routes = state.get("routes")
    if isinstance(raw_routes, list):
        cutoff = now - _settings.REPLY_ROUTE_RECORD_MAX_AGE_SECONDS
        for raw_route in raw_routes:
            if not isinstance(raw_route, dict):
                continue
            saved_route_id = str(raw_route.get("route_id") or "").strip()
            if not saved_route_id:
                continue
            try:
                route_ts = float(raw_route.get("ts") or 0)
            except (TypeError, ValueError):
                route_ts = 0.0
            if route_ts and route_ts < cutoff:
                continue
            if normalized_route_id and saved_route_id == normalized_route_id:
                if (
                    str(raw_route.get("chat_id")),
                    raw_route.get("message_thread_id"),
                ) != (str(chat_id), message_thread_id):
                    raise ValueError(
                        "a Telegram task route cannot change its destination"
                    )
                continue
            routes.append(
                {
                    "message_thread_id": raw_route.get("message_thread_id"),
                    "source_message_id": raw_route.get("source_message_id"),
                    "route_id": saved_route_id,
                    "chat_id": str(raw_route.get("chat_id") or ""),
                    "is_group": bool(raw_route.get("is_group")),
                    "ts": int(route_ts or now),
                }
            )
    if normalized_route_id:
        routes.append(
            {
                "message_thread_id": message_thread_id,
                "source_message_id": source_message_id,
                "route_id": normalized_route_id,
                "chat_id": str(chat_id),
                "is_group": bool(is_group),
                "ts": now,
            }
        )
    if len(routes) > _settings.REPLY_ROUTE_RECORD_MAX_COUNT:
        routes = routes[-_settings.REPLY_ROUTE_RECORD_MAX_COUNT :]
    state.update(
        {
            "version": 2,
            "chat_id": str(chat_id),
            "is_group": bool(is_group),
            "route_id": normalized_route_id,
            "active_route_id": normalized_route_id,
            "ts": now,
            "updated_ts": now,
            "routes": routes,
        }
    )
    _state.write_json_object(state_path, state)


@_state.serialized
def clear_reply_route_chat_id(
    state_path: Path,
    chat_id: str | None = None,
    *,
    route_id: str | None = None,
) -> None:
    state = _state.read_json_object(state_path)
    normalized_route_id = str(route_id or "").strip()
    if chat_id is not None and str(state.get("chat_id") or "") != str(chat_id):
        # A route-specific completion must still be allowed to retire its own
        # immutable record even when a newer chat has become the compatibility
        # top-level route.
        if not normalized_route_id:
            return
    if normalized_route_id:
        routes = state.get("routes")
        if isinstance(routes, list):
            state["routes"] = [
                item
                for item in routes
                if not (
                    isinstance(item, dict)
                    and str(item.get("route_id") or "") == normalized_route_id
                )
            ]
        if str(state.get("active_route_id") or "") != normalized_route_id:
            state.update({"updated_ts": int(time.time())})
            _state.write_json_object(state_path, state)
            return
    state.update(
        {
            "chat_id": "",
            "is_group": False,
            "route_id": "",
            "active_route_id": "",
            "cleared_ts": int(time.time()),
            "updated_ts": int(time.time()),
        }
    )
    _state.write_json_object(state_path, state)


def reply_route_details(state_path: Path, route_id: str) -> dict[str, Any] | None:
    """Resolve one issued task route, including its forum topic and source."""
    if reply_route_for_id(state_path, route_id) is None:
        return None
    for route in reversed(_state.read_json_object(state_path).get("routes", [])):
        if isinstance(route, dict) and str(route.get("route_id")) == route_id:
            return route
    return None
